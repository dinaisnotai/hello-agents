"""Generate full before/after evidence for the bounded quality repair loop."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.planner_agent import PlannerAgent  # noqa: E402
from app.agents.trip_planner_agent import MultiAgentTripPlanner  # noqa: E402
from app.models.agent_outputs import WeatherQueryResult  # noqa: E402
from app.models.schemas import (  # noqa: E402
    Attraction,
    DayPlan,
    Hotel,
    Location,
    RouteSegment,
    TripPlan,
    TripRequest,
)
from app.services.planning_observability import build_planning_trace  # noqa: E402
from app.services.spatial_planner import (  # noqa: E402
    estimate_leg,
    haversine_meters,
)


class EstimatedRouteEvaluator:
    def build_day_routes(self, day, city):
        if not day.attractions:
            return []
        nodes = []
        if day.hotel and day.hotel.location:
            nodes.append((day.hotel.name, day.hotel.location))
        nodes.extend((item.name, item.location) for item in day.attractions)
        if day.hotel and day.hotel.location:
            nodes.append((day.hotel.name, day.hotel.location))
        segments = []
        for (origin_name, origin), (destination_name, destination) in zip(
            nodes, nodes[1:]
        ):
            leg = estimate_leg(origin, destination, day.transportation)
            segments.append(
                RouteSegment(
                    day_index=day.day_index,
                    origin=origin_name,
                    destination=destination_name,
                    route_type=leg.mode,
                    distance_meters=haversine_meters(origin, destination),
                    duration_minutes=leg.travel_minutes,
                    walking_distance_meters=leg.walking_meters,
                )
            )
        return segments


class SequenceEvaluatorAgent:
    def __init__(self, responses):
        self.responses = list(responses)

    def run(self, input_text):
        if len(self.responses) > 1:
            return json.dumps(self.responses.pop(0), ensure_ascii=False)
        return json.dumps(self.responses[0], ensure_ascii=False)


def request(days):
    return TripRequest(
        city="北京",
        start_date="2026-10-10",
        end_date=f"2026-10-{9 + days:02d}",
        travel_days=days,
        transportation="public transit",
        accommodation="comfortable hotel",
        pace="balanced",
    )


def attraction(name, index, category="historic", duration=150):
    return Attraction(
        name=name,
        poi_id=f"demo-{index}",
        visit_key=f"provider:demo-{index}",
        category=category,
        categories=[category],
        score=90 - index,
        visit_duration=duration,
        suggested_duration_minutes=duration,
        location=Location(
            longitude=116.40 + index * 0.002,
            latitude=39.91 + index * 0.001,
        ),
    )


def planner():
    value = MultiAgentTripPlanner()
    value.route_evaluator = EstimatedRouteEvaluator()
    return value


def plan_for(value, trip_request, groups):
    hotel = Hotel(
        name="中心酒店",
        type=trip_request.accommodation,
        estimated_cost=300,
        location=Location(longitude=116.40, latitude=39.91),
    )
    result = TripPlan(
        city=trip_request.city,
        start_date=trip_request.start_date,
        end_date=trip_request.end_date,
        days=[
            DayPlan(
                date=f"2026-10-{10 + index:02d}",
                day_index=index,
                description=f"Day {index + 1}",
                transportation=trip_request.transportation,
                accommodation=trip_request.accommodation,
                hotel=deepcopy(hotel),
                attractions=list(items),
            )
            for index, items in enumerate(groups)
        ],
        overall_suggestions="Initial itinerary",
    )
    value._recalculate(result, trip_request)
    return result


def evidence(name, trip_request, before, after, candidates, source):
    before_eval = planner().completeness_gate.evaluate(
        trip_request, before, candidates
    )
    return {
        "name": name,
        "review_source": source,
        "before_evaluation": before_eval.model_dump(
            mode="json", by_alias=True
        ),
        "after_evaluation": (
            after.quality_evaluation.model_dump(
                mode="json", by_alias=True
            )
            if after.quality_evaluation
            else None
        ),
        "before_plan": before.model_dump(mode="json"),
        "after_plan": after.model_dump(mode="json"),
        "proof": {
            "plan_changed": [
                [item.name for item in day.attractions]
                for day in before.days
            ]
            != [
                [item.name for item in day.attractions]
                for day in after.days
            ],
            "hard_constraints_pass_after": after.constraint_report.passed
            and after.validation_result.valid,
            "duplicate_free_after": len(
                [
                    item.visit_key
                    for day in after.days
                    for item in day.attractions
                ]
            )
            == len(
                {
                    item.visit_key
                    for day in after.days
                    for item in day.attractions
                }
            ),
            "quality_gate_passed": after.quality_gate_passed,
            "repair_actions": [
                item.model_dump(mode="json")
                for item in after.repair_history
            ],
        },
    }


def main():
    examples = []

    value = planner()
    trip_request = request(3)
    selected = [
        attraction("故宫", 1),
        attraction("景山公园", 2, "park"),
        attraction("国家博物馆", 3, "museum"),
        attraction("什刹海", 4, "historic"),
    ]
    candidates = [
        *selected,
        attraction("天坛", 5, "historic"),
        attraction("北海公园", 6, "park"),
    ]
    before = plan_for(value, trip_request, [selected[:2], selected[2:], []])
    after = value.run_quality_loop(before, trip_request, candidates)
    examples.append(
        evidence(
            "empty_day_repaired",
            trip_request,
            before,
            after,
            candidates,
            "deterministic_completeness_gate",
        )
    )

    value = planner()
    trip_request = request(1)
    short = attraction("胡同短游", 10, "historic", 90)
    nearby = attraction("附近公园", 11, "park", 150)
    before = plan_for(value, trip_request, [[short]])
    after = value.run_quality_loop(
        before, trip_request, [short, nearby]
    )
    examples.append(
        evidence(
            "underfilled_day_repaired",
            trip_request,
            before,
            after,
            [short, nearby],
            "deterministic_completeness_gate",
        )
    )

    value = planner()
    trip_request = request(1)
    museums = [
        attraction("专题馆A", 20, "museum", 90),
        attraction("专题馆B", 21, "museum", 90),
        attraction("专题馆C", 22, "museum", 90),
    ]
    park = attraction("城市公园", 23, "park", 90)
    before = plan_for(value, trip_request, [museums])
    before.observability_trace = build_planning_trace(
        trip_request,
        before,
        candidates=[*museums, park],
    )
    failed = {
        "pass": False,
        "overall_score": 5.5,
        "issues": [
            {
                "issue_type": "poor_diversity",
                "severity": "high",
                "day": 1,
                "evidence": "Day 1 contains three same-category specialist museums",
                "repair_strategy": "REPLACE_LOW_VALUE_CATEGORY",
                "source": "llm",
            }
        ],
        "source": "llm",
    }
    passed = {
        "pass": True,
        "overall_score": 9.0,
        "issues": [],
        "source": "llm",
    }
    reviewer = PlannerAgent(None, value)
    reviewer.agent = SequenceEvaluatorAgent([failed, passed])
    after = reviewer.review_plan(
        trip_request,
        before,
        WeatherQueryResult(),
        [],
        available_attractions=[*museums, park],
        hotel_candidates=[before.days[0].hotel],
    )
    examples.append(
        evidence(
            "llm_reviewer_issue_repaired",
            trip_request,
            before,
            after,
            [*museums, park],
            "structured_llm_experience_evaluator",
        )
    )

    value = planner()
    trip_request = request(2)
    only_candidates = [
        attraction("唯一活动A", 30, "historic", 90),
        attraction("唯一活动B", 31, "park", 90),
    ]
    before = plan_for(
        value,
        trip_request,
        [only_candidates, []],
    )
    after = value.run_quality_loop(
        before,
        trip_request,
        only_candidates,
    )
    examples.append(
        evidence(
            "unrepairable_empty_day_degraded",
            trip_request,
            before,
            after,
            only_candidates,
            "deterministic_completeness_gate",
        )
    )

    output = ROOT / "quality_repair_examples.json"
    output.write_text(
        json.dumps(examples, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(examples)} complete comparisons to {output}")


if __name__ == "__main__":
    main()
