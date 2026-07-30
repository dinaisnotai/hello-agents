"""Run three realistic request payloads through the production quality loop."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.trip_planner_agent import MultiAgentTripPlanner  # noqa: E402
from app.models.schemas import (  # noqa: E402
    Attraction,
    DayPlan,
    Hotel,
    Location,
    RouteSegment,
    TripPlan,
    TripPlanResponse,
    TripRequest,
    WeatherInfo,
)
from app.services.spatial_planner import estimate_leg, haversine_meters  # noqa: E402


class EstimatedRouteEvaluator:
    def build_day_routes(self, day, city):
        nodes = []
        if day.hotel and day.hotel.location:
            nodes.append((day.hotel.name, day.hotel.location))
        nodes.extend((item.name, item.location) for item in day.attractions)
        if day.hotel and day.hotel.location:
            nodes.append((day.hotel.name, day.hotel.location))
        return [
            RouteSegment(
                day_index=day.day_index,
                origin=origin_name,
                destination=destination_name,
                route_type=(
                    leg := estimate_leg(origin, destination, day.transportation)
                ).mode,
                distance_meters=haversine_meters(origin, destination),
                duration_minutes=leg.travel_minutes,
                walking_distance_meters=leg.walking_meters,
            )
            for (origin_name, origin), (destination_name, destination)
            in zip(nodes, nodes[1:])
        ]


def poi(name, index, category, longitude, latitude):
    return Attraction(
        name=name,
        poi_id=f"request-demo-{index}",
        visit_key=f"provider:request-demo-{index}",
        category=category,
        categories=[category],
        visit_duration=150,
        suggested_duration_minutes=150,
        popularity=8,
        score=90 - index,
        location=Location(longitude=longitude, latitude=latitude),
    )


def initial_plan(request, attractions, weather):
    hotel = Hotel(
        name=f"{request.city}中心酒店",
        type=request.accommodation,
        estimated_cost=360,
        location=Location(
            longitude=attractions[0].location.longitude if attractions else 116.4,
            latitude=attractions[0].location.latitude if attractions else 39.9,
        ),
    )
    plan = TripPlan(
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        days=[
            DayPlan(
                date=request.start_date,
                day_index=0,
                description="根据用户请求生成的首选行程",
                transportation=request.transportation,
                accommodation=request.accommodation,
                hotel=deepcopy(hotel),
                attractions=deepcopy(attractions),
            )
        ],
        weather_info=weather,
        overall_suggestions="按实时开放信息和天气执行。",
    )
    return plan


def run_case(name, request, selected, candidates, weather):
    planner = MultiAgentTripPlanner()
    planner.route_evaluator = EstimatedRouteEvaluator()
    before = initial_plan(request, selected, weather)
    planner._recalculate(before, request)
    issues_before = planner.completeness_gate.evaluate(
        request, before, candidates
    )
    after = planner.run_quality_loop(before, request, candidates)
    response = TripPlanResponse(
        success=after.quality_gate_passed,
        message=(
            "旅行计划已生成，并保留未解决的非阻断提示"
            if after.quality_gate_passed and after.unresolved_quality_issues
            else "旅行计划生成成功"
            if after.quality_gate_passed
            else "已返回包含完整 TripPlan 的 degraded best-effort 结果"
        ),
        data=after,
    )
    return {
        "name": name,
        "request": request.model_dump(mode="json"),
        "issues_before": issues_before.model_dump(mode="json", by_alias=True),
        "repair_trace": [
            {
                "repair_action": item.selected_action,
                "action_result": item.action_result,
                "accepted": item.accepted,
                "rejected_reason": item.rejection_reason,
                "issues_before": [
                    issue.model_dump(mode="json")
                    for issue in item.issues_before
                ],
                "issues_after": [
                    issue.model_dump(mode="json")
                    for issue in item.issues_after
                ],
            }
            for item in after.repair_history
        ],
        "issues_after": (
            after.quality_evaluation.model_dump(mode="json", by_alias=True)
            if after.quality_evaluation
            else None
        ),
        "final_gate_decision": {
            "pass": after.quality_gate_passed,
            "blocking_count": len(after.unresolved_blocking_issues),
            "warning_count": len(after.unresolved_non_blocking_issues),
            "degraded_reason": after.degraded_reason,
            "best_effort": after.best_effort,
        },
        "complete_user_response": response.model_dump(
            mode="json", by_alias=True
        ),
    }


def main():
    beijing_request = TripRequest(
        city="北京",
        start_date="2026-08-08",
        end_date="2026-08-08",
        travel_days=1,
        transportation="public transit",
        accommodation="comfortable hotel",
        preferences=["历史文化", "城市公园"],
    )
    palace = poi("故宫博物院", 1, "historic", 116.397, 39.916)
    park = poi("景山公园", 2, "park", 116.396, 39.925)
    museum = poi("中国国家博物馆", 3, "museum", 116.407, 39.904)

    hangzhou_request = TripRequest(
        city="杭州",
        start_date="2026-08-09",
        end_date="2026-08-09",
        travel_days=1,
        transportation="public transit",
        accommodation="economy hotel",
        preferences=["自然风光"],
        pace="relaxed",
    )
    west_lake = poi("西湖苏堤", 11, "park", 120.145, 30.247)
    tea = poi("龙井村", 12, "natural", 120.105, 30.224)

    shanghai_request = TripRequest(
        city="上海",
        start_date="2026-08-10",
        end_date="2026-08-10",
        travel_days=1,
        transportation="public transit",
        accommodation="economy hotel",
        must_visit=["外滩"],
    )

    traces = [
        run_case(
            "beijing_thunderstorm_with_indoor_backup",
            beijing_request,
            [palace, park],
            [palace, park, museum],
            [WeatherInfo(date="2026-08-08", day_weather="雷阵雨")],
        ),
        run_case(
            "hangzhou_heat_without_indoor_candidate",
            hangzhou_request,
            [west_lake, tea],
            [west_lake, tea],
            [WeatherInfo(date="2026-08-09", day_weather="高温")],
        ),
        run_case(
            "shanghai_candidate_outage_empty_full_day",
            shanghai_request,
            [],
            [],
            [WeatherInfo(date="2026-08-10", day_weather="多云")],
        ),
    ]
    output = ROOT / "quality_gate_real_traces.json"
    output.write_text(
        json.dumps(traces, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(traces)} complete request traces to {output}")


if __name__ == "__main__":
    main()
