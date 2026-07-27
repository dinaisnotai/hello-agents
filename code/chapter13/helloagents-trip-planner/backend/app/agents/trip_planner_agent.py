"""Constraint-aware multi-role trip planner."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Set

from ..models.schemas import (
    Attraction,
    Budget,
    ConstraintItem,
    ConstraintReport,
    DayPlan,
    EvidenceSource,
    Hotel,
    Meal,
    POIInfo,
    PlanningTraceItem,
    ReplanRequest,
    RouteSegment,
    TripPlan,
    TripRequest,
    WeatherInfo,
)
from ..services.amap_service import AmapService, get_amap_service
from ..services.place_name_service import normalize_place_name, place_names_match
from ..services.poi_category_service import POI_CATEGORY_LABELS, classify_poi
from ..services.rag_service import TravelGuideRAG, get_travel_guide_rag
from ..services.spatial_planner import SpatialItineraryPlanner, get_pace_profile


@dataclass
class _POICandidate:
    """A POI plus the planning metadata that raw map results do not contain."""

    poi: POIInfo
    matched_preferences: Set[str] = field(default_factory=set)
    categories: Set[str] = field(default_factory=set)
    is_must_visit: bool = False
    score: float = 0


def _clock_minutes(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        parsed = datetime.strptime(value, "%H:%M")
        return parsed.hour * 60 + parsed.minute
    except ValueError:
        return default


def _format_clock(minutes: int) -> str:
    minutes = max(0, minutes)
    day_offset, clock_minutes = divmod(minutes, 24 * 60)
    clock = f"{clock_minutes // 60:02d}:{clock_minutes % 60:02d}"
    return clock if day_offset == 0 else f"次日{clock}"


def daily_time_budget_minutes(request: TripRequest) -> int:
    """Use an explicit user availability window before falling back to pace."""

    start = _clock_minutes(request.daily_start_time, -1)
    end = _clock_minutes(request.daily_end_time, -1)
    if start >= 0 and end > start:
        return end - start
    return get_pace_profile(request.pace).daily_time_budget_minutes


class POICollector:
    def __init__(self, amap_service: AmapService):
        self.amap_service = amap_service

    def collect_attractions(self, request: TripRequest) -> List[Attraction]:
        recalled = self._recall_candidates(request)
        filtered = self._filter_candidates(recalled, request)
        deduplicated = self._deduplicate_candidates(filtered)
        scored = self._score_candidates(deduplicated, request)
        selected = self._select_candidates(scored, limit=30)
        return [self._to_attraction(candidate, request) for candidate in selected]

    def _recall_candidates(self, request: TripRequest) -> List[_POICandidate]:
        """Recall broadly, while keeping the source of every result explicit."""

        candidates: List[_POICandidate] = []
        keywords = request.preferences or ["景点", "博物馆", "公园"]
        for keyword in keywords[:4]:
            for poi in self._safe_search(keyword, request.city):
                candidates.append(
                    _POICandidate(
                        poi=poi,
                        matched_preferences={keyword},
                        categories=classify_poi(poi.name, poi.type),
                    )
                )

        # Must-visits get a dedicated recall pass. A search result is only
        # promoted when its name actually matches what the user requested.
        for must_visit in request.must_visit:
            matches = [
                poi
                for poi in self._safe_search(must_visit, request.city)
                if place_names_match(must_visit, poi.name)
            ]
            if matches:
                poi = max(matches, key=self._poi_quality)
                candidates.append(
                    _POICandidate(
                        poi=poi,
                        categories=classify_poi(poi.name, poi.type),
                        is_must_visit=True,
                    )
                )

        # A must-visit may already have been found by an interest query even if
        # its dedicated search failed, so mark those results as well.
        for candidate in candidates:
            if any(place_names_match(name, candidate.poi.name) for name in request.must_visit):
                candidate.is_must_visit = True
        return candidates

    def _filter_candidates(
        self, candidates: List[_POICandidate], request: TripRequest
    ) -> List[_POICandidate]:
        """Apply hard exclusions before any ranking happens."""

        return [
            candidate
            for candidate in candidates
            if not candidate.categories.intersection(request.avoid_categories)
        ]

    def _deduplicate_candidates(self, candidates: List[_POICandidate]) -> List[_POICandidate]:
        """Merge duplicate IDs and aliases without losing must-visit metadata."""

        unique: List[_POICandidate] = []
        for candidate in candidates:
            existing = next(
                (item for item in unique if self._same_poi(item.poi, candidate.poi)),
                None,
            )
            if existing is None:
                unique.append(candidate)
                continue

            existing.is_must_visit = existing.is_must_visit or candidate.is_must_visit
            existing.matched_preferences.update(candidate.matched_preferences)
            existing.categories.update(candidate.categories)
            if self._poi_quality(candidate.poi) > self._poi_quality(existing.poi):
                existing.poi = candidate.poi
        return unique

    def _score_candidates(
        self, candidates: List[_POICandidate], request: TripRequest
    ) -> List[_POICandidate]:
        """Score only after invalid and duplicate tool results are gone."""

        for candidate in candidates:
            candidate.score = self._score_poi(candidate.poi.name, candidate.poi.type or "", request)
            if candidate.is_must_visit:
                candidate.score = max(candidate.score, 100)
        return candidates

    def _select_candidates(
        self, candidates: List[_POICandidate], limit: int
    ) -> List[_POICandidate]:
        """Reserve capacity for must-visits; truncate only optional candidates."""

        must_visits = sorted(
            (item for item in candidates if item.is_must_visit),
            key=self._candidate_sort_key,
        )
        optional = sorted(
            (item for item in candidates if not item.is_must_visit),
            key=self._candidate_sort_key,
        )
        optional_limit = max(0, limit - len(must_visits))
        return must_visits + optional[:optional_limit]

    def _to_attraction(self, candidate: _POICandidate, request: TripRequest) -> Attraction:
        poi = candidate.poi
        if candidate.is_must_visit:
            description = f"{poi.name}是用户指定的必去地点。"
        else:
            themes = "、".join(sorted(candidate.matched_preferences)) or "综合"
            description = f"{poi.name}适合{themes}主题旅行。"
        return Attraction(
            name=poi.name,
            address=poi.address,
            location=poi.location,
            visit_duration=self._duration_for(request.pace),
            description=description,
            category=poi.type,
            rating=poi.rating,
            poi_id=poi.id,
            ticket_price=poi.ticket_price,
            score=candidate.score,
            opening_hours=poi.opening_hours,
            opening_time=poi.opening_time,
            closing_time=poi.closing_time,
            latest_entry_time=poi.latest_entry_time,
            hours_source=poi.hours_source,
        )

    def collect_hotel(self, request: TripRequest) -> Hotel:
        keyword = f"{request.hotel_area or request.city} {request.accommodation} 酒店"
        nightly_cost = 350 if "经济" in request.accommodation else 650 if "舒适" in request.accommodation else 1000
        pois = self._safe_search(keyword, request.city)
        if not pois:
            return Hotel(
                name=f"{request.hotel_area or request.city}待确认酒店",
                type=request.accommodation,
                estimated_cost=nightly_cost,
            )
        poi = pois[0]
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
        return score

    def _duration_for(self, pace: str) -> int:
        return get_pace_profile(pace).visit_duration_minutes

    def _safe_search(self, keyword: str, city: str) -> List[POIInfo]:
        try:
            return self.amap_service.search_poi(keyword, city) or []
        except Exception:
            return []

    def _same_poi(self, left: POIInfo, right: POIInfo) -> bool:
        if left.id and right.id and left.id == right.id:
            return True
        return place_names_match(left.name, right.name)

    def _poi_quality(self, poi: POIInfo) -> tuple:
        return (
            bool(poi.id),
            poi.rating or 0,
            len(normalize_place_name(poi.name)),
            normalize_place_name(poi.name),
            poi.id,
            poi.location.longitude,
            poi.location.latitude,
        )

    def _candidate_sort_key(self, candidate: _POICandidate) -> tuple:
        poi = candidate.poi
        return (
            -candidate.score,
            normalize_place_name(poi.name),
            poi.id,
            poi.location.longitude,
            poi.location.latitude,
        )


class RouteEvaluator:
    def __init__(self, amap_service: AmapService):
        self.amap_service = amap_service

    def build_day_routes(self, day: DayPlan, city: str) -> List[RouteSegment]:
        segments: List[RouteSegment] = []
        attractions = day.attractions
        if day.transportation == "步行":
            route_type = "walking"
        elif any(label in day.transportation for label in ("公共", "公交", "地铁")):
            route_type = "transit"
        else:
            route_type = "driving"

        route_nodes = [
            (item.name, item.address, item.location) for item in attractions
        ]
        if attractions and day.hotel and day.hotel.location:
            hotel_node = (day.hotel.name, day.hotel.address, day.hotel.location)
            route_nodes = [hotel_node, *route_nodes, hotel_node]

        for origin, destination in zip(route_nodes, route_nodes[1:]):
            origin_name, origin_address, origin_location = origin
            destination_name, destination_address, destination_location = destination
            route = self.amap_service.route_between_pois(
                origin_name=origin_name,
                origin_address=origin_address,
                origin=origin_location,
                destination_name=destination_name,
                destination_address=destination_address,
                destination=destination_location,
                city=city,
                route_type=route_type,
            )
            segments.append(
                RouteSegment(
                    day_index=day.day_index,
                    origin=origin_name,
                    destination=destination_name,
                    route_type=route.route_type,
                    distance_meters=route.distance,
                    duration_minutes=max(1, int(route.duration / 60)),
                    walking_distance_meters=route.walking_distance,
                    walking_duration_minutes=(
                        max(1, round(route.walking_duration / 60))
                        if route.walking_duration
                        else 0
                    ),
                    transit_duration_minutes=(
                        max(1, round(route.transit_duration / 60))
                        if route.transit_duration
                        else 0
                    ),
                    steps=route.steps,
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
            max_walk = max(
                (day.daily_walking_distance_km for day in plan.days),
                default=0,
            )
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
        empty_days = [day.day_index + 1 for day in plan.days if not day.attractions]
        if len(plan.days) > 1:
            days_covered = not empty_days
            items.append(
                ConstraintItem(
                    name="每日行程覆盖",
                    passed=days_covered,
                    actual=(
                        "每天均有游览安排"
                        if days_covered
                        else f"第{', '.join(map(str, empty_days))}天空白"
                    ),
                    expected="多日旅行每天至少安排一个可执行游览点",
                    severity="blocker" if not days_covered else "info",
                    message=(
                        "每天均有有效行程"
                        if days_covered
                        else "存在空白日，需要从未使用候选中补充可执行景点"
                    ),
                )
            )

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

        all_attractions = [attraction for day in plan.days for attraction in day.attractions]
        for avoided_category in request.avoid_categories:
            violations = [
                attraction.name
                for attraction in all_attractions
                if avoided_category
                in classify_poi(attraction.name, attraction.category or "")
            ]
            passed = not violations
            label = POI_CATEGORY_LABELS[avoided_category]
            items.append(
                ConstraintItem(
                    name=f"避开类型：{label}",
                    passed=passed,
                    actual="未安排该类型" if passed else f"发现：{', '.join(violations)}",
                    expected=f"行程中不包含{label}",
                    severity="blocker" if not passed else "info",
                    message="避开类型已满足" if passed else "候选过滤未完全生效，需要重新规划",
                )
            )

        if request.dietary_restrictions:
            meals = [meal for day in plan.days for meal in day.meals]
            passed = bool(meals) and all(
                all(
                    restriction in f"{meal.name} {meal.description or ''}"
                    for restriction in request.dietary_restrictions
                )
                for meal in meals
            )
            items.append(
                ConstraintItem(
                    name="饮食限制",
                    passed=passed,
                    actual="; ".join(request.dietary_restrictions),
                    expected="餐饮描述中规避相关限制",
                    severity="info" if passed else "blocker",
                    message=(
                        "餐饮推荐已明确标记饮食限制"
                        if passed
                        else "至少一餐缺少明确的饮食限制标记"
                    ),
                )
            )

        closed_attractions = [
            attraction
            for day in plan.days
            for attraction in day.attractions
            if attraction.opening_hours_status == "closed"
        ]
        if closed_attractions:
            details = "; ".join(
                f"{item.name} {item.planned_arrival_time}-{item.planned_departure_time} "
                f"(closes {item.closing_time})"
                for item in closed_attractions
            )
            items.append(
                ConstraintItem(
                    name="Opening-hours feasibility",
                    passed=False,
                    actual=details,
                    expected="Each visit must finish before closing time and enter before last entry",
                    severity="blocker",
                    message="At least one attraction is scheduled outside its published opening hours",
                )
            )

        unknown_hour_attractions = [
            attraction.name
            for day in plan.days
            for attraction in day.attractions
            if attraction.opening_hours_status == "unknown"
        ]
        if unknown_hour_attractions:
            items.append(
                ConstraintItem(
                    name="Opening-hours data coverage",
                    passed=True,
                    actual=", ".join(unknown_hour_attractions),
                    expected="Verify these attractions with the official source before departure",
                    severity="warning",
                    message="Some attractions have no reliable opening-hours data; they require confirmation",
                )
            )

        time_budget = daily_time_budget_minutes(request)
        use_actual_window = bool(request.daily_start_time and request.daily_end_time)
        longest_day = max(
            (
                day.daily_elapsed_minutes
                if use_actual_window
                else day.daily_duration_minutes
                for day in plan.days
            ),
            default=0,
        )
        time_passed = longest_day <= time_budget
        items.append(
            ConstraintItem(
                name="每日返程时间" if use_actual_window else "每日行程时长",
                passed=time_passed,
                actual=f"最长{longest_day}分钟",
                expected=f"不超过{time_budget}分钟",
                severity=(
                    "blocker"
                    if not time_passed and use_actual_window
                    else "warning"
                    if not time_passed
                    else "info"
                ),
                message=(
                    "每天均可在用户指定的最晚时间前返回"
                    if time_passed and use_actual_window
                    else "每日游览、交通和缓冲时间在节奏预算内"
                    if time_passed
                    else "真实路线显示当天无法在用户指定的最晚时间前返回"
                    if use_actual_window
                    else "长耗时或远郊活动已单独安排，但当天仍可能较累"
                ),
            )
        )

        if not items:
            items.append(ConstraintItem(name="基础完整性", passed=True, message="行程结构完整"))

        passed_count = sum(1 for item in items if item.passed)
        return ConstraintReport(
            passed=all(item.passed for item in items),
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

    max_planning_iterations = 5

    def __init__(self):
        self.amap_service = get_amap_service()
        self.rag = get_travel_guide_rag()
        self.poi_collector = POICollector(self.amap_service)
        self.spatial_planner = SpatialItineraryPlanner()
        self.route_evaluator = RouteEvaluator(self.amap_service)
        self.budget_estimator = BudgetEstimator()
        self.constraint_checker = ConstraintChecker()
        self.reviewer = PlannerReviewer()

    def plan_trip(self, request: TripRequest) -> TripPlan:
        attractions = self.poi_collector.collect_attractions(request)
        hotel = self.poi_collector.collect_hotel(request)
        weather = self._weather_for_dates(request)
        evidence = self.rag.search(request.city, self.build_rag_query(request), top_k=5)

        return self.build_plan_from_inputs(
            request=request,
            attractions=attractions,
            hotel=hotel,
            weather=weather,
            evidence=evidence,
        )

    def build_plan_from_inputs(
        self,
        request: TripRequest,
        attractions: List[Attraction],
        hotel: Hotel,
        weather: List[WeatherInfo],
        evidence: List[EvidenceSource],
    ) -> TripPlan:
        """Build a plan from specialist outputs without searching again."""

        self._apply_conservative_opening_hours(attractions)
        days = self._assign_days(request, attractions, hotel)
        plan = TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=days,
            weather_info=weather,
            overall_suggestions=self._build_suggestions(request, evidence),
            evidence_sources=evidence,
            risk_warnings=[],
        )
        plan = self._repair_until_stable(plan, request, attractions)
        return self.reviewer.review(plan, request)

    @staticmethod
    def _apply_conservative_opening_hours(attractions: List[Attraction]) -> None:
        """Prevent obviously late visits when a POI omits its hours.

        These are conservative category policies, never represented as live
        provider data. Unknown commercial and street locations remain unknown.
        """

        for attraction in attractions:
            if attraction.opening_time and attraction.closing_time:
                continue
            text = f"{attraction.name} {attraction.category or ''}"
            if any(term in text for term in ("博物馆", "美术馆", "展览", "文化馆", "纪念馆")):
                attraction.opening_time = "09:00"
                attraction.closing_time = "18:00"
                attraction.latest_entry_time = "16:30"
                attraction.hours_source = "category_estimate"
            elif any(term in text for term in ("故宫", "寺", "宫", "遗址", "古迹")):
                attraction.opening_time = "08:00"
                attraction.closing_time = "17:30"
                attraction.latest_entry_time = "16:00"
                attraction.hours_source = "category_estimate"

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
        self._recalculate(plan, request)
        if replan_request.notes:
            plan.risk_warnings.append(f"重规划备注：{replan_request.notes}")
        return self.reviewer.review(plan, request)

    def _repair_until_stable(
        self,
        plan: TripPlan,
        request: TripRequest,
        available_attractions: List[Attraction],
    ) -> TripPlan:
        """Bound constraint repairs and keep the best plan seen so far."""

        best_plan: Optional[TripPlan] = None
        best_rank: Optional[tuple] = None

        for iteration in range(self.max_planning_iterations):
            self._recalculate(plan, request)
            report = plan.constraint_report
            score = report.score
            plan.planning_trace.append(
                PlanningTraceItem(
                    iteration=iteration,
                    role="ConstraintChecker",
                    action="check_constraints",
                    reason=self._report_summary(report),
                    score_before=score,
                    score_after=score,
                )
            )
            rank = self._plan_rank(plan, request)
            if best_rank is None or rank > best_rank:
                best_plan, best_rank = deepcopy(plan), rank

            if report.passed:
                plan.failure_reason = None
                return plan

            score_before = score
            action = self._apply_next_repair(plan, request, available_attractions)
            if action is None:
                break

            self._recalculate(plan, request)
            plan.planning_trace.append(
                PlanningTraceItem(
                    iteration=iteration,
                    role="RepairPolicy",
                    action=action[0],
                    reason=action[1],
                    score_before=score_before,
                    score_after=plan.constraint_report.score,
                )
            )
            repaired_rank = self._plan_rank(plan, request)
            if best_rank is None or repaired_rank > best_rank:
                best_plan, best_rank = deepcopy(plan), repaired_rank
            if plan.constraint_report.passed:
                plan.failure_reason = None
                return plan

        result = best_plan or plan
        self._recalculate(result, request)
        result.failure_reason = self._failure_reason(result.constraint_report)
        return result

    def _recalculate(self, plan: TripPlan, request: TripRequest) -> None:
        for day in plan.days:
            self._recalculate_day(day, request)
        plan.route_segments = [segment for day in plan.days for segment in day.route_segments]
        plan.budget = self.budget_estimator.estimate(plan.days, request)
        plan.constraint_report = self.constraint_checker.check(plan, request)

    def _recalculate_day(self, day: DayPlan, request: TripRequest) -> None:
        day.route_segments = self.route_evaluator.build_day_routes(day, request.city)
        self._schedule_day_opening_hours(day, request)
        self._update_day_metrics(day, request.pace)

    def _apply_next_repair(
        self,
        plan: TripPlan,
        request: TripRequest,
        available_attractions: List[Attraction],
    ) -> Optional[tuple[str, str]]:
        planned_names = [item.name for day in plan.days for item in day.attractions]
        missing = [
            name
            for name in request.must_visit
            if not any(place_names_match(name, planned) for planned in planned_names)
        ]
        if missing:
            candidate = next(
                (
                    item
                    for item in available_attractions
                    if place_names_match(missing[0], item.name)
                ),
                None,
            )
            if candidate is None or not plan.days:
                return None
            target_day = min(plan.days, key=lambda day: (len(day.attractions), day.day_index))
            target_day.attractions.append(deepcopy(candidate))
            target_day.attractions = self.spatial_planner._nearest_neighbor_order(
                target_day.attractions,
                request.must_visit,
                target_day.hotel.location if target_day.hotel else None,
            )
            return (
                "add_missing_must_visit",
                f"Added required attraction: {candidate.name}",
            )

        empty_days = [day for day in plan.days if not day.attractions]
        if empty_days:
            target_day = min(empty_days, key=lambda day: day.day_index)
            unused = [
                attraction
                for attraction in available_attractions
                if not any(
                    place_names_match(attraction.name, planned_name)
                    for planned_name in planned_names
                )
            ]
            for attraction in sorted(
                unused,
                key=lambda item: (-item.score, item.name, item.poi_id or ""),
            ):
                if self._try_add_attraction(target_day, attraction, request):
                    return (
                        "fill_empty_day_with_feasible_attraction",
                        f"Added {attraction.name} to day {target_day.day_index + 1} "
                        "after validating real routes, opening hours, and return time",
                    )

        closed_optional = [
            (day, attraction)
            for day in plan.days
            for attraction in day.attractions
            if attraction.opening_hours_status == "closed"
            and not any(place_names_match(name, attraction.name) for name in request.must_visit)
        ]
        if closed_optional:
            donor_day, attraction = min(
                closed_optional,
                key=lambda item: (item[1].score, -item[1].visit_duration, item[1].name),
            )
            for target_day in sorted(
                (day for day in plan.days if day is not donor_day),
                key=lambda day: (
                    len(day.attractions),
                    day.daily_elapsed_minutes,
                    day.day_index,
                ),
            ):
                if self._try_move_attraction(
                    donor_day, target_day, attraction, request
                ):
                    return (
                        "move_closed_optional_attraction",
                        f"Moved {attraction.name} from day {donor_day.day_index + 1} "
                        f"to day {target_day.day_index + 1}, where it fits opening hours and return time",
                    )

            donor_day.attractions.remove(attraction)
            return (
                "remove_closed_optional_attraction",
                f"Removed {attraction.name}; its scheduled visit is outside published opening hours",
            )

        if request.budget_limit is not None and plan.budget and plan.budget.total > request.budget_limit:
            optional = [
                (day, attraction)
                for day in plan.days
                for attraction in day.attractions
                if not any(place_names_match(name, attraction.name) for name in request.must_visit)
            ]
            if optional:
                day, attraction = min(
                    optional,
                    key=lambda item: (item[1].score, -item[1].ticket_price, item[1].name),
                )
                day.attractions.remove(attraction)
                return (
                    "remove_low_priority_attraction",
                    f"Budget exceeds the limit; removed optional attraction {attraction.name}",
                )

            hotel = next((day.hotel for day in plan.days if day.hotel), None)
            if hotel and hotel.estimated_cost > 200:
                old_cost = hotel.estimated_cost
                hotel.estimated_cost = 350 if old_cost > 350 else 200
                return (
                    "downgrade_hotel",
                    f"Budget exceeds the limit; reduced nightly hotel estimate from {old_cost} to {hotel.estimated_cost}",
                )

            meals = [meal for day in plan.days for meal in day.meals if meal.estimated_cost > 20]
            if meals:
                for meal in meals:
                    meal.estimated_cost = max(20, round(meal.estimated_cost * 0.8))
                return (
                    "reduce_adjustable_meal_costs",
                    "Budget exceeds the limit; reduced adjustable meal estimates",
                )

        overlong_days = [
            day for day in plan.days if self._day_exceeds_window(day, request)
        ]
        if overlong_days:
            donor_day = max(
                overlong_days,
                key=lambda day: (
                    day.daily_elapsed_minutes
                    if request.daily_start_time and request.daily_end_time
                    else day.daily_duration_minutes
                ),
            )
            optional = sorted(
                (
                    attraction
                    for attraction in donor_day.attractions
                    if not any(
                        place_names_match(name, attraction.name)
                        for name in request.must_visit
                    )
                ),
                key=lambda attraction: (attraction.score, -attraction.visit_duration, attraction.name),
            )

            # Evaluate both affected days with real routes. Adding only the
            # visit duration badly underestimates remote POIs such as Badaling.
            for attraction in optional:
                for target_day in sorted(
                    (day for day in plan.days if day is not donor_day),
                    key=lambda day: (
                        len(day.attractions),
                        day.daily_elapsed_minutes,
                        day.day_index,
                    ),
                ):
                    if self._try_move_attraction(
                        donor_day, target_day, attraction, request
                    ):
                        return (
                            "move_optional_attraction_to_reduce_daily_duration",
                            f"Moved optional attraction {attraction.name} from day "
                            f"{donor_day.day_index + 1} to day {target_day.day_index + 1}",
                        )

            if optional:
                attraction = optional[0]
                donor_day.attractions.remove(attraction)
                return (
                    "remove_optional_attraction_to_reduce_daily_duration",
                    f"Day {donor_day.day_index + 1} exceeds the time budget; "
                    f"removed optional attraction {attraction.name}",
                )

            # All remaining attractions are must-visits. They may still be
            # distributed across days without dropping a hard user request.
            for attraction in sorted(
                donor_day.attractions,
                key=lambda item: (-item.visit_duration, item.name),
            ):
                for target_day in sorted(
                    (day for day in plan.days if day is not donor_day),
                    key=lambda day: (
                        len(day.attractions),
                        day.daily_elapsed_minutes,
                        day.day_index,
                    ),
                ):
                    if self._try_move_attraction(
                        donor_day, target_day, attraction, request
                    ):
                        return (
                            "move_must_visit_to_reduce_daily_duration",
                            f"Moved required attraction {attraction.name} from day "
                            f"{donor_day.day_index + 1} to day {target_day.day_index + 1}",
                        )

        if request.max_daily_walk_km is not None:
            over_limit_days = [
                day for day in plan.days if day.daily_walking_distance_km > request.max_daily_walk_km
            ]
            if over_limit_days:
                day = max(over_limit_days, key=lambda item: item.daily_walking_distance_km)
                if day.transportation != "公共交通":
                    day.transportation = "公共交通"
                    return (
                        "switch_to_transit",
                        f"Walking exceeds the daily limit on day {day.day_index + 1}; switched to public transit",
                    )

        return None

    def _try_move_attraction(
        self,
        donor_day: DayPlan,
        target_day: DayPlan,
        attraction: Attraction,
        request: TripRequest,
    ) -> bool:
        """Commit a cross-day move only after real routes make both days feasible."""

        candidate_donor = deepcopy(donor_day)
        candidate_target = deepcopy(target_day)
        candidate_donor.attractions = [
            item
            for item in candidate_donor.attractions
            if item.name != attraction.name
        ]
        candidate_target.attractions.append(deepcopy(attraction))
        for candidate in (candidate_donor, candidate_target):
            candidate.attractions = self.spatial_planner._nearest_neighbor_order(
                candidate.attractions,
                request.must_visit,
                candidate.hotel.location if candidate.hotel else None,
            )
            self._recalculate_day(candidate, request)

        if not self._day_is_feasible(candidate_target, request):
            return False
        if candidate_donor.attractions and not self._day_is_feasible(
            candidate_donor, request
        ):
            return False

        donor_day.attractions = candidate_donor.attractions
        target_day.attractions = candidate_target.attractions
        return True

    def _try_add_attraction(
        self,
        target_day: DayPlan,
        attraction: Attraction,
        request: TripRequest,
    ) -> bool:
        candidate = deepcopy(target_day)
        candidate.attractions.append(deepcopy(attraction))
        candidate.attractions = self.spatial_planner._nearest_neighbor_order(
            candidate.attractions,
            request.must_visit,
            candidate.hotel.location if candidate.hotel else None,
        )
        self._recalculate_day(candidate, request)
        if not self._day_is_feasible(candidate, request):
            return False
        target_day.attractions = candidate.attractions
        return True

    def _day_is_feasible(self, day: DayPlan, request: TripRequest) -> bool:
        return (
            not any(
                attraction.opening_hours_status == "closed"
                for attraction in day.attractions
            )
            and not self._day_exceeds_window(day, request)
        )

    def _day_exceeds_window(self, day: DayPlan, request: TripRequest) -> bool:
        time_budget = daily_time_budget_minutes(request)
        if request.daily_start_time and request.daily_end_time:
            return day.daily_elapsed_minutes > time_budget
        return day.daily_duration_minutes > time_budget

    def _plan_rank(self, plan: TripPlan, request: TripRequest) -> tuple:
        failed_count = sum(not item.passed for item in plan.constraint_report.items)
        budget_overrun = max(0, -(plan.budget.remaining or 0)) if plan.budget else 0
        max_walk_overrun = (
            max(
                (
                    max(0, day.daily_walking_distance_km - request.max_daily_walk_km)
                    for day in plan.days
                ),
                default=0,
            )
            if request.max_daily_walk_km is not None
            else 0
        )
        return (-failed_count, plan.constraint_report.score, -budget_overrun, -max_walk_overrun)

    def _report_summary(self, report: ConstraintReport) -> str:
        failures = [item.message or item.name for item in report.items if not item.passed]
        return "; ".join(failures) if failures else "All constraints passed"

    def _failure_reason(self, report: ConstraintReport) -> str:
        if any(
            not item.passed and item.name in {"每日行程时长", "每日返程时间"}
            for item in report.items
        ):
            return (
                "无法在当前旅行天数、节奏与必去景点要求下满足每日时长限制；"
                "请增加旅行天数、降低行程节奏或减少必去景点。"
            )
        failures = [item.message or item.name for item in report.items if not item.passed]
        return "; ".join(failures) or "Planning stopped before all constraints could be satisfied"

    def _assign_days(self, request: TripRequest, attractions: List[Attraction], hotel: Hotel) -> List[DayPlan]:
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
        if not hasattr(self, "spatial_planner"):
            self.spatial_planner = SpatialItineraryPlanner()
        daily_groups = self.spatial_planner.plan(
            attractions=attractions,
            travel_days=request.travel_days,
            pace=request.pace,
            must_visit=request.must_visit,
            hotel_location=hotel.location,
            transportation=request.transportation,
            daily_time_budget_minutes=daily_time_budget_minutes(request),
        )
        days: List[DayPlan] = []

        for day_index in range(request.travel_days):
            current_date = start_date + timedelta(days=day_index)
            day_attractions = daily_groups[day_index]
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

    def _schedule_day_opening_hours(self, day: DayPlan, request: TripRequest) -> None:
        """Calculate visit windows after routes are known and mark hard violations."""

        current = _clock_minutes(request.daily_start_time, 9 * 60)
        start_minutes = current
        day.planned_start_time = _format_clock(current)
        incoming_by_destination = {segment.destination: segment for segment in day.route_segments}
        for attraction in day.attractions:
            incoming = incoming_by_destination.get(attraction.name)
            arrival = current + (incoming.duration_minutes if incoming else 0)
            if incoming is not None:
                incoming.planned_departure_time = _format_clock(current)
                incoming.planned_arrival_time = _format_clock(arrival)
            opening = _clock_minutes(attraction.opening_time, 0)
            start = max(arrival, opening) if attraction.opening_time else arrival
            departure = start + attraction.visit_duration
            closing = _clock_minutes(attraction.closing_time, 24 * 60)
            latest_entry = _clock_minutes(attraction.latest_entry_time, closing)
            attraction.planned_arrival_time = _format_clock(arrival)
            attraction.planned_departure_time = _format_clock(departure)
            if not attraction.opening_time or not attraction.closing_time:
                attraction.opening_hours_status = "unknown"
            elif start > latest_entry or departure > closing:
                attraction.opening_hours_status = "closed"
            else:
                attraction.opening_hours_status = "open"
            current = departure
        if day.route_segments and day.attractions:
            return_segment = day.route_segments[-1]
            if return_segment.origin == day.attractions[-1].name:
                return_segment.planned_departure_time = _format_clock(current)
                current += return_segment.duration_minutes
                return_segment.planned_arrival_time = _format_clock(current)
        day.planned_end_time = _format_clock(current)
        day.daily_elapsed_minutes = current - start_minutes

    def _update_day_metrics(self, day: DayPlan, pace: str) -> None:
        profile = get_pace_profile(pace)
        day.daily_distance_km = round(
            sum(segment.distance_meters for segment in day.route_segments) / 1000, 2
        )
        day.daily_walking_distance_km = round(
            sum(
                segment.distance_meters
                if segment.route_type == "walking"
                else segment.walking_distance_meters
                for segment in day.route_segments
            )
            / 1000,
            2,
        )
        day.daily_visit_minutes = sum(
            attraction.visit_duration for attraction in day.attractions
        )
        actual_travel_minutes = sum(
            segment.duration_minutes for segment in day.route_segments
        )
        estimated_timing = self.spatial_planner.estimate_day_timing(
            day.attractions,
            day.hotel.location if day.hotel else None,
            day.transportation,
            profile,
        )
        day.daily_travel_minutes = max(
            actual_travel_minutes, estimated_timing.travel_minutes
        )
        day.daily_buffer_minutes = (
            profile.daily_buffer_minutes if day.attractions else 0
        )
        day.daily_duration_minutes = (
            day.daily_visit_minutes
            + day.daily_travel_minutes
            + day.daily_buffer_minutes
        )
        day.daily_cost = self._daily_cost(day)

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

    def build_rag_query(self, request: TripRequest) -> str:
        """Build the retrieval query shared by legacy and Agent workflows."""

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
