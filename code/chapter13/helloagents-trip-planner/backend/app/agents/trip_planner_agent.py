"""Constraint-aware multi-role trip planner."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from ..models.schemas import (
    Attraction,
    Budget,
    ConstraintItem,
    ConstraintReport,
    DayPlan,
    Hotel,
    Meal,
    ReplanRequest,
    RouteSegment,
    TripPlan,
    TripRequest,
    WeatherInfo,
)
from ..services.amap_service import AmapService, get_amap_service
from ..services.place_name_service import place_names_match
from ..services.rag_service import TravelGuideRAG, get_travel_guide_rag


class POICollector:
    def __init__(self, amap_service: AmapService):
        self.amap_service = amap_service

    def collect_attractions(self, request: TripRequest) -> List[Attraction]:
        keywords = request.preferences or ["景点", "博物馆", "公园"]
        candidates: Dict[str, Attraction] = {}

        for keyword in keywords[:4]:
            for poi in self.amap_service.search_poi(keyword, request.city):
                if self._is_avoided(poi.type, request.avoid_categories):
                    continue
                candidates[poi.name] = Attraction(
                    name=poi.name,
                    address=poi.address,
                    location=poi.location,
                    visit_duration=self._duration_for(request.pace),
                    description=f"{poi.name}适合{keyword}主题旅行。",
                    category=poi.type,
                    rating=poi.rating,
                    poi_id=poi.id,
                    ticket_price=poi.ticket_price,
                    score=self._score_poi(poi.name, poi.type, request),
                )

        for must_visit in request.must_visit:
            matched_key = next(
                (name for name in candidates if place_names_match(must_visit, name)),
                None,
            )
            if matched_key is not None:
                candidates[matched_key].score = max(candidates[matched_key].score, 100)
            else:
                poi = self.amap_service.search_poi(must_visit, request.city)[0]
                candidates[poi.name] = Attraction(
                    name=poi.name,
                    address=poi.address,
                    location=poi.location,
                    visit_duration=self._duration_for(request.pace),
                    description=f"{poi.name}是用户指定的必去地点。",
                    category=poi.type,
                    rating=poi.rating,
                    poi_id=poi.id,
                    ticket_price=poi.ticket_price,
                    score=100,
                )

        return sorted(candidates.values(), key=lambda item: item.score, reverse=True)[:30]

    def collect_hotel(self, request: TripRequest) -> Hotel:
        keyword = f"{request.hotel_area or request.city} {request.accommodation} 酒店"
        pois = self.amap_service.search_poi(keyword, request.city)
        poi = pois[0]
        nightly_cost = 350 if "经济" in request.accommodation else 650 if "舒适" in request.accommodation else 1000
        return Hotel(
            name=poi.name if "酒店" in poi.name else f"{request.hotel_area or request.city}推荐酒店",
            address=poi.address,
            location=poi.location,
            price_range=f"{nightly_cost}-{nightly_cost + 200}元/晚",
            rating=str(poi.rating or 4.5),
            distance="靠近核心游览区域",
            type=request.accommodation,
            estimated_cost=nightly_cost,
        )

    def _score_poi(self, name: str, category: str, request: TripRequest) -> float:
        score = 50.0
        score += 40 if any(place_names_match(must, name) for must in request.must_visit) else 0
        score += 8 if any(pref in category or pref in name for pref in request.preferences) else 0
        score -= 30 if self._is_avoided(category, request.avoid_categories) else 0
        return score

    def _duration_for(self, pace: str) -> int:
        return {"relaxed": 150, "balanced": 120, "packed": 90}.get(pace, 120)

    def _is_avoided(self, category: str, avoid_categories: List[str]) -> bool:
        return any(item and item in category for item in avoid_categories)


class RouteEvaluator:
    def __init__(self, amap_service: AmapService):
        self.amap_service = amap_service

    def build_day_routes(self, day: DayPlan, city: str) -> List[RouteSegment]:
        segments: List[RouteSegment] = []
        attractions = day.attractions
        if day.transportation == "步行":
            route_type = "walking"
        elif day.transportation == "公共交通":
            route_type = "transit"
        else:
            route_type = "driving"
        for index in range(len(attractions) - 1):
            origin = attractions[index]
            destination = attractions[index + 1]
            route = self.amap_service.route_between_pois(
                origin_name=origin.name,
                origin_address=origin.address,
                origin=origin.location,
                destination_name=destination.name,
                destination_address=destination.address,
                destination=destination.location,
                city=city,
                route_type=route_type,
            )
            segments.append(
                RouteSegment(
                    day_index=day.day_index,
                    origin=origin.name,
                    destination=destination.name,
                    route_type=route.route_type,
                    distance_meters=route.distance,
                    duration_minutes=max(1, int(route.duration / 60)),
                    description=route.description,
                )
            )
        return segments


class BudgetEstimator:
    def estimate(self, days: List[DayPlan], request: TripRequest) -> Budget:
        total_attractions = sum(attr.ticket_price for day in days for attr in day.attractions)
        total_hotels = sum((day.hotel.estimated_cost if day.hotel else 0) for day in days)
        total_meals = sum(meal.estimated_cost for day in days for meal in day.meals)
        total_transportation = sum(30 + int(day.daily_distance_km * 3) for day in days)
        total = total_attractions + total_hotels + total_meals + total_transportation
        remaining = request.budget_limit - total if request.budget_limit is not None else None
        return Budget(
            total_attractions=total_attractions,
            total_hotels=total_hotels,
            total_meals=total_meals,
            total_transportation=total_transportation,
            total=total,
            budget_limit=request.budget_limit,
            remaining=remaining,
        )


class ConstraintChecker:
    def check(self, plan: TripPlan, request: TripRequest) -> ConstraintReport:
        items: List[ConstraintItem] = []

        if request.budget_limit is not None and plan.budget:
            passed = plan.budget.total <= request.budget_limit
            items.append(
                ConstraintItem(
                    name="预算上限",
                    passed=passed,
                    actual=f"{plan.budget.total}元",
                    expected=f"不超过{request.budget_limit}元",
                    severity="blocker" if not passed else "info",
                    message="预算满足要求" if passed else "预算超出，需要减少酒店/门票/餐饮成本",
                )
            )

        if request.max_daily_walk_km is not None:
            max_walk = max((day.daily_distance_km for day in plan.days), default=0)
            passed = max_walk <= request.max_daily_walk_km
            items.append(
                ConstraintItem(
                    name="每日步行距离",
                    passed=passed,
                    actual=f"{max_walk:.1f}km",
                    expected=f"不超过{request.max_daily_walk_km:.1f}km",
                    severity="warning" if not passed else "info",
                    message="步行强度可接受" if passed else "某天步行距离偏长，建议替换相邻景点或改用打车",
                )
            )

        all_names = [attr.name for day in plan.days for attr in day.attractions]
        for must_visit in request.must_visit:
            passed = any(place_names_match(must_visit, name) for name in all_names)
            items.append(
                ConstraintItem(
                    name=f"必去景点：{must_visit}",
                    passed=passed,
                    actual="已安排" if passed else "未安排",
                    expected="必须出现在行程中",
                    severity="blocker" if not passed else "info",
                    message="必去景点已覆盖" if passed else "需要在重规划中优先加入",
                )
            )

        if request.dietary_restrictions:
            items.append(
                ConstraintItem(
                    name="饮食限制",
                    passed=True,
                    actual="; ".join(request.dietary_restrictions),
                    expected="餐饮描述中规避相关限制",
                    severity="info",
                    message="餐饮推荐已标记饮食限制，实际点餐前仍建议二次确认",
                )
            )

        if not items:
            items.append(ConstraintItem(name="基础完整性", passed=True, message="行程结构完整"))

        passed_count = sum(1 for item in items if item.passed)
        return ConstraintReport(
            passed=all(item.passed for item in items if item.severity == "blocker"),
            score=round(passed_count / len(items), 3),
            items=items,
        )


class PlannerReviewer:
    def review(self, plan: TripPlan, request: TripRequest) -> TripPlan:
        warnings = list(plan.risk_warnings)
        if plan.constraint_report.score < 1:
            warnings.append("Reviewer发现部分约束未完全满足，已在约束报告中标出，可通过减少景点或提升预算继续重规划。")

        bad_weather = [
            weather
            for weather in plan.weather_info
            if any(word in weather.day_weather for word in ["雨", "雪", "雷", "台风"])
        ]
        if bad_weather:
            warnings.append("天气存在降雨/恶劣风险，建议把室外景点和博物馆类景点互换。")

        plan.risk_warnings = sorted(set(warnings))
        return plan


class MultiAgentTripPlanner:
    """Coordinates deterministic planner roles and optional local RAG evidence."""

    def __init__(self):
        self.amap_service = get_amap_service()
        self.rag = get_travel_guide_rag()
        self.poi_collector = POICollector(self.amap_service)
        self.route_evaluator = RouteEvaluator(self.amap_service)
        self.budget_estimator = BudgetEstimator()
        self.constraint_checker = ConstraintChecker()
        self.reviewer = PlannerReviewer()

    def plan_trip(self, request: TripRequest) -> TripPlan:
        attractions = self.poi_collector.collect_attractions(request)
        hotel = self.poi_collector.collect_hotel(request)
        weather = self._weather_for_dates(request)
        evidence = self.rag.search(request.city, self._rag_query(request), top_k=5)

        days = self._assign_days(request, attractions, hotel)
        for day in days:
            day.route_segments = self.route_evaluator.build_day_routes(day, request.city)
            day.daily_distance_km = round(sum(seg.distance_meters for seg in day.route_segments) / 1000, 2)
            day.daily_cost = self._daily_cost(day)

        route_segments = [segment for day in days for segment in day.route_segments]
        budget = self.budget_estimator.estimate(days, request)
        plan = TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=days,
            weather_info=weather,
            overall_suggestions=self._build_suggestions(request, evidence),
            budget=budget,
            route_segments=route_segments,
            evidence_sources=evidence,
            risk_warnings=[],
        )
        plan.constraint_report = self.constraint_checker.check(plan, request)
        return self.reviewer.review(plan, request)

    def replan(self, replan_request: ReplanRequest) -> TripPlan:
        plan = replan_request.plan
        request = replan_request.request or TripRequest(
            city=plan.city,
            start_date=plan.start_date,
            end_date=plan.end_date,
            travel_days=len(plan.days),
            transportation=plan.days[0].transportation if plan.days else "公共交通",
            accommodation=plan.days[0].accommodation if plan.days else "经济型酒店",
        )
        for day in plan.days:
            day.route_segments = self.route_evaluator.build_day_routes(day, request.city)
            day.daily_distance_km = round(sum(seg.distance_meters for seg in day.route_segments) / 1000, 2)
            day.daily_cost = self._daily_cost(day)
        plan.route_segments = [segment for day in plan.days for segment in day.route_segments]
        plan.budget = self.budget_estimator.estimate(plan.days, request)
        plan.constraint_report = self.constraint_checker.check(plan, request)
        if replan_request.notes:
            plan.risk_warnings.append(f"重规划备注：{replan_request.notes}")
        return self.reviewer.review(plan, request)

    def _assign_days(self, request: TripRequest, attractions: List[Attraction], hotel: Hotel) -> List[DayPlan]:
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
        per_day = {"relaxed": 2, "balanced": 3, "packed": 3}.get(request.pace, 3)
        selected = attractions[: max(request.travel_days * per_day, len(request.must_visit))]
        days: List[DayPlan] = []

        for day_index in range(request.travel_days):
            current_date = start_date + timedelta(days=day_index)
            day_attractions = selected[day_index * per_day : (day_index + 1) * per_day]
            if not day_attractions and selected:
                day_attractions = selected[-2:]
            days.append(
                DayPlan(
                    date=current_date.strftime("%Y-%m-%d"),
                    day_index=day_index,
                    description=f"第{day_index + 1}天围绕{request.city}核心区域游览，节奏为{request.pace}。",
                    transportation=request.transportation,
                    accommodation=request.accommodation,
                    hotel=hotel,
                    attractions=day_attractions,
                    meals=self._build_meals(request, day_index),
                )
            )
        return days

    def _build_meals(self, request: TripRequest, day_index: int) -> List[Meal]:
        restriction = f"，注意{';'.join(request.dietary_restrictions)}" if request.dietary_restrictions else ""
        return [
            Meal(type="breakfast", name=f"{request.city}本地早餐", description=f"以清淡快捷为主{restriction}", estimated_cost=25),
            Meal(type="lunch", name=f"{request.city}特色午餐", description=f"靠近上午景点，减少绕路{restriction}", estimated_cost=60),
            Meal(type="dinner", name=f"{request.city}晚餐推荐", description=f"安排在住宿或交通便利区域{restriction}", estimated_cost=80),
        ]

    def _weather_for_dates(self, request: TripRequest) -> List[WeatherInfo]:
        weather = self.amap_service.get_weather(request.city)
        if weather:
            return weather[: request.travel_days]

        start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
        return [
            WeatherInfo(
                date=(start_date + timedelta(days=index)).strftime("%Y-%m-%d"),
                day_weather="未知",
                night_weather="未知",
                day_temp=0,
                night_temp=0,
                wind_direction="",
                wind_power="",
            )
            for index in range(request.travel_days)
        ]

    def _daily_cost(self, day: DayPlan) -> int:
        return (
            sum(attr.ticket_price for attr in day.attractions)
            + sum(meal.estimated_cost for meal in day.meals)
            + (day.hotel.estimated_cost if day.hotel else 0)
            + 30
            + int(day.daily_distance_km * 3)
        )

    def _build_suggestions(self, request: TripRequest, evidence: List) -> str:
        base = f"已按{request.pace}节奏为{request.city}生成行程，优先考虑{', '.join(request.preferences) or '综合体验'}。"
        if request.budget_limit:
            base += f" 总预算目标为{request.budget_limit}元。"
        if evidence:
            base += " 规划参考了本地攻略证据，结果页可查看来源片段。"
        return base

    def _rag_query(self, request: TripRequest) -> str:
        return " ".join(
            [
                request.city,
                " ".join(request.preferences),
                " ".join(request.must_visit),
                request.free_text_input or "",
                request.pace,
            ]
        )


_multi_agent_planner: Optional[MultiAgentTripPlanner] = None


def get_trip_planner_agent() -> MultiAgentTripPlanner:
    global _multi_agent_planner
    if _multi_agent_planner is None:
        _multi_agent_planner = MultiAgentTripPlanner()
    return _multi_agent_planner
