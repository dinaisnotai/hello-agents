"""Reproduce the reported Beijing candidate mismatch and emit the fixed chain."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.trip_planner_agent import MultiAgentTripPlanner  # noqa: E402
from app.models.schemas import Attraction, DayPlan, Hotel, Location, TripPlan, TripRequest  # noqa: E402
from app.services.planning_observability import build_planning_trace  # noqa: E402


def poi(name, score, role, lon, lat, *, poi_id=None, parent="", category="historic"):
    return Attraction(
        name=name, poi_id=poi_id or name, parent_poi_id=parent,
        visit_key=f"provider:{parent or poi_id or name}", score=score,
        selection_role=role, is_core_landmark=role == "core_landmark",
        category=category, categories=[category], popularity=8,
        first_visit_priority=9 if role == "core_landmark" else 7,
        visit_duration=120, suggested_duration_minutes=120,
        location=Location(longitude=lon, latitude=lat),
    )


def summarize(trace):
    return [
        {
            "name": item.name,
            "visit_key": item.visit_key,
            "raw_candidate_score": item.final_score,
            "landmark_tier": item.landmark_tier,
            "selection_role": item.selection_role,
            "final_selected": item.final_selected,
            "final_reason": item.selection_reason,
            "decision_trace": item.selection_trace,
            "rejection_rule": item.rejection_reason,
        }
        for item in trace.candidate_pois
    ]


def main():
    request = TripRequest(
        city="北京", start_date="2026-10-10", end_date="2026-10-12",
        travel_days=3, transportation="public transit",
        accommodation="comfortable hotel", first_visit=True,
        preferences=["历史"], max_daily_walk_km=8,
    )
    palace = poi("故宫博物院", 93.8, "core_landmark", 116.397, 39.918, poi_id="palace")
    wenhua = poi("故宫博物院-文华殿", 93.8, "core_landmark", 116.402, 39.916, poi_id="wenhua", parent="palace")
    national = poi("中国国家博物馆", 93.6, "core_landmark", 116.407, 39.904, poi_id="national", category="museum")
    temple = poi("天坛公园", 90.5, "core_landmark", 116.407, 39.882, poi_id="temple", category="park")
    summer = poi("颐和园", 87.5, "core_landmark", 116.273, 39.999, poi_id="summer", category="park")
    zhengyang = poi("正阳门箭楼", 55.0, "major_attraction", 116.397, 39.899, poi_id="zhengyang")
    guozijian = poi("国子监", 50.1, "major_attraction", 116.407, 39.946, poi_id="guozijian")
    police = poi("北京警察博物馆", 23.6, "niche_attraction", 116.418, 39.899, poi_id="police", category="museum")
    raw = [wenhua, palace, national, temple, summer, zhengyang, guozijian, police]

    # This is the ambiguous pre-finalize assignment from the reported run.
    legacy_assignment = [
        ["故宫博物院-文华殿", "天坛公园"],
        ["正阳门箭楼", "颐和园"],
        ["中国国家博物馆", "天坛公园"],
    ]
    planner = MultiAgentTripPlanner()
    canonical = planner._identity().deduplicate(raw, must_visit=request.must_visit)
    hotel = Hotel(name="北京待确认酒店", type=request.accommodation)
    plan = TripPlan(
        city=request.city, start_date=request.start_date, end_date=request.end_date,
        overall_suggestions="决策链演示",
        days=[
            DayPlan(date="2026-10-10", day_index=0, description="d1", transportation=request.transportation, accommodation=request.accommodation, hotel=deepcopy(hotel), attractions=[deepcopy(palace), deepcopy(temple)]),
            DayPlan(date="2026-10-11", day_index=1, description="d2", transportation=request.transportation, accommodation=request.accommodation, hotel=deepcopy(hotel), attractions=[deepcopy(zhengyang), deepcopy(summer)]),
            DayPlan(date="2026-10-12", day_index=2, description="d3", transportation=request.transportation, accommodation=request.accommodation, hotel=deepcopy(hotel), attractions=[deepcopy(national), deepcopy(temple)]),
        ],
    )
    planner._recalculate(plan, request)  # removes the duplicate canonical visit
    planner._repair_low_soft_score(plan, request, canonical)
    planner._recalculate(plan, request)
    # Explicitly record the underfilled-day admission check that previously
    # allowed the low-score niche POI merely because it was nearby.
    police_candidate = next(item for item in canonical if item.name == "北京警察博物馆")
    planner._candidate_policy().evaluate(
        police_candidate,
        pool=canonical,
        selected=[item for day in plan.days for item in day.attractions],
        request=request,
        entry_point="underfilled_day_candidate_check",
    )
    trace = build_planning_trace(request, plan, candidates=canonical, eligible_candidates=canonical)

    output = {
        "reported_pre_finalize_assignment": legacy_assignment,
        "canonical_candidate_pool": [item.name for item in canonical],
        "final_itinerary": [
            {"day": day.day_index + 1, "pois": [item.name for item in day.attractions],
             "travel_minutes": day.daily_travel_minutes,
             "walking_km": day.daily_walking_distance_km,
             "utilization": day.day_utilization_score}
            for day in plan.days
        ],
        "portfolio_metrics": trace.portfolio_metrics.model_dump(mode="json"),
        "candidate_decision_chain": summarize(trace),
    }
    path = ROOT / "candidate_selection_chain_real_case.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote candidate decision chain to {path}")


if __name__ == "__main__":
    main()
