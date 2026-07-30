"""Produce inspectable offline traces for portfolio and weather-repair paths.

This is intentionally a request-level demonstration rather than an assertion
fixture: it records selected and rejected candidates, the complete itinerary,
every transactional repair decision, and the response payload returned to a
client.  Run with ``python scripts/demonstrate_portfolio_and_weather_traces.py``.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.trip_planner_agent import MultiAgentTripPlanner  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.schemas import (  # noqa: E402
    TripPlan,
    TripPlanResponse,
    TripRequest,
    WeatherInfo,
)
from app.services.poi_metadata_service import (  # noqa: E402
    build_preference_profile,
    enrich_attraction,
)
from app.services.rag_service import EmbeddingError, TravelGuideRAG  # noqa: E402


class OfflineEmbedder:
    def embed(self, texts):
        raise EmbeddingError("trace demo uses deterministic keyword retrieval")


def prepare_candidates(planner: MultiAgentTripPlanner, request: TripRequest):
    """Follow the production candidate preparation path for a later repair."""

    constraint_set = planner.constraint_extractor.extract(request)
    effective = planner._request_for_constraints(request, constraint_set)
    profile = build_preference_profile(effective)
    candidates = planner.ensure_candidate_coverage(
        effective, planner.poi_collector.collect_attractions(effective)
    )
    candidates = [
        enrich_attraction(candidate, effective, profile)
        for candidate in candidates
    ]
    candidates = planner._identity().deduplicate(
        candidates, must_visit=effective.must_visit
    )
    return planner.attraction_scorer.score_candidates(
        candidates,
        effective,
        None,
        planner._daily_time_budget(effective),
    )


def itinerary(plan: TripPlan):
    return [
        {
            "day": day.day_index + 1,
            "date": day.date,
            "primary_plan": [item.name for item in day.primary_plan],
            "weather_backup": [item.name for item in day.weather_backup],
            "weather_warning": day.weather_warning,
            "travel_minutes": day.daily_travel_minutes,
            "walking_km": day.daily_walking_distance_km,
            "utilization": day.day_utilization_score,
            "hotel": day.hotel.name if day.hotel else None,
        }
        for day in plan.days
    ]


def candidate_decisions(plan: TripPlan):
    traces = plan.observability_trace.candidate_pois if plan.observability_trace else []
    return [
        {
            "name": item.name,
            "score": item.final_score,
            "score_breakdown": item.score_breakdown,
            "selection_role": item.selection_role,
            "decision": "selected" if not item.rejection_reason else "rejected",
            "reason": item.selection_reason or item.rejection_reason,
        }
        for item in sorted(traces, key=lambda item: item.final_score, reverse=True)[:16]
    ]


def repair_trace(plan: TripPlan):
    return [
        {
            "iteration": entry.iteration,
            "issues_before": [issue.model_dump(mode="json") for issue in entry.issues_before],
            "repair_strategy": entry.selected_action,
            "mutation_scope": entry.mutation_scope,
            "actual_modified_fields": entry.actual_modified_fields,
            "itinerary_before": entry.itinerary_before,
            "itinerary_after": entry.itinerary_after,
            "constraint_delta": entry.constraint_delta,
            "issue_delta": entry.issue_delta,
            "action_result": entry.action_result,
            "accepted": entry.accepted,
            "rollback_reason": entry.rollback_reason or entry.rejection_reason,
            "issues_after": [issue.model_dump(mode="json") for issue in entry.issues_after],
        }
        for entry in plan.repair_history
    ]


def response_for(plan: TripPlan):
    return TripPlanResponse(
        success=plan.quality_gate_passed,
        message=(
            "Itinerary generated with unresolved non-blocking warnings."
            if plan.best_effort and plan.quality_gate_passed
            else "Itinerary generated successfully."
            if plan.quality_gate_passed
            else "Degraded response still includes the best executable TripPlan "
            "and actionable unresolved issues."
        ),
        data=plan,
    ).model_dump(mode="json", by_alias=True)


def run_case(name: str, request: TripRequest, *, thunderstorm: bool = False):
    planner = MultiAgentTripPlanner()
    planner.rag = TravelGuideRAG(embedder=OfflineEmbedder())
    before = planner.plan_trip(request, emit_observability=False)
    candidates = prepare_candidates(planner, request)
    issues_before = planner.completeness_gate.evaluate(request, before, candidates)

    if thunderstorm:
        repaired_input = deepcopy(before)
        repaired_input.repair_history = []
        repaired_input.weather_info = [
            WeatherInfo(
                date=day.date,
                day_weather="雷阵雨" if day.day_index == 1 else "多云",
            )
            for day in repaired_input.days
        ]
        issues_before = planner.completeness_gate.evaluate(
            request, repaired_input, candidates
        )
        after = planner.run_quality_loop(repaired_input, request, candidates)
    else:
        after = before

    return {
        "name": name,
        "request": request.model_dump(mode="json"),
        "top_candidates_and_selection_process": candidate_decisions(after),
        "portfolio_metrics": (
            after.observability_trace.portfolio_metrics.model_dump(mode="json")
            if after.observability_trace
            else {}
        ),
        "issues_before": issues_before.model_dump(mode="json", by_alias=True),
        "trip_plan_before_repair": itinerary(before),
        "repair_trace": repair_trace(after),
        "trip_plan_after_repair": itinerary(after),
        "final_gate_decision": {
            "quality_gate_passed": after.quality_gate_passed,
            "degraded": not after.quality_gate_passed,
            "best_effort": after.best_effort,
            "degraded_reason": after.degraded_reason,
            "unresolved_blocking_issues": [
                issue.model_dump(mode="json")
                for issue in after.unresolved_blocking_issues
            ],
            "unresolved_non_blocking_issues": [
                issue.model_dump(mode="json")
                for issue in after.unresolved_non_blocking_issues
            ],
        },
        "complete_user_response": response_for(after),
    }


def main():
    settings.amap_api_key = ""
    settings.embedding_api_key = ""
    base = {
        "city": "北京",
        "start_date": "2026-10-01",
        "end_date": "2026-10-03",
        "travel_days": 3,
        "transportation": "public transit",
        "accommodation": "comfortable hotel",
        "pace": "balanced",
        "first_visit": True,
    }
    traces = [
        run_case(
            "beijing_first_visit_family",
            TripRequest(**base, preferences=["亲子"]),
        ),
        run_case(
            "beijing_first_visit_history",
            TripRequest(**base, preferences=["历史"]),
        ),
        run_case(
            "beijing_deep_niche_history",
            TripRequest(
                **{**base, "first_visit": False},
                preferences=["小众", "深度历史游", "避开热门", "多次到访"],
            ),
        ),
        run_case(
            "beijing_thunderstorm_outdoor_primary_with_indoor_backup",
            TripRequest(**base, preferences=[]),
            thunderstorm=True,
        ),
    ]
    output = ROOT / "portfolio_weather_real_traces.json"
    output.write_text(
        json.dumps(traces, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(traces)} complete request traces to {output}")


if __name__ == "__main__":
    main()
