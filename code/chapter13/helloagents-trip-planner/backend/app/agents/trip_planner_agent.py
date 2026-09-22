"""Constraint-aware multi-role trip planner."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence, Set
from uuid import uuid4
import logging
from ..config import settings
from ..models.schemas import (
    Attraction,
    CandidateScoreDebug,
    Budget,
    ConstraintItem,
    ConstraintReport,
    DayPlan,
    EvidenceSource,
    Hotel,
    Location,
    Meal,
    POIInfo,
    PlanningTraceItem,
    PlanReviewScores,
    ReplanRequest,
    RouteSegment,
    ScheduleBlock,
    TripPlan,
    TripRequest,
    WeatherInfo,
)
from ..models.quality import (
    ExperienceEvaluation,
    ExperienceIssue,
    CommitDecisionCode,
    PlanChange,
    PlanVersionMetadata,
    RepairAttempt,
    RepairAttemptStatus,
    RepairIteration,
    RepairStrategy,
)
from ..constraints.extractor import ConstraintExtractor
from ..constraints.schema import (
    ConstraintSet,
    ConstraintType,
    ValidationResult,
)
from ..constraints.validators import ConstraintValidationEngine
from ..services.amap_service import AmapService, get_amap_service
from ..services.attraction_scorer import AttractionScorer
from ..services.accommodation_selector import AccommodationSelector
from ..services.candidate_acceptance_policy import CandidateAcceptancePolicy
from ..services.user_warning_service import curate_user_warnings
from ..services.place_name_service import normalize_place_name, place_names_match
from ..services.planning_observability import (
    build_planning_trace,
    emit_planning_trace,
    refresh_planning_trace,
)
from ..services.poi_identity_resolver import POIIdentityResolver
from ..services.itinerary_quality import ItineraryCompletenessGate
from ..services.repair_controller import RepairController
from ..services.travel_knowledge_service import get_travel_knowledge_service
from ..services.plan_mutation_sandbox import (
    CommitDecision,
    PlanMutationSandbox,
)
from ..services.poi_category_service import POI_CATEGORY_LABELS, classify_poi
from ..services.poi_metadata_service import (
    PREFERENCE_CATEGORY_MAP,
    build_preference_profile,
    enrich_attraction,
    enrich_poi,
    get_city_classic_names,
    get_city_core_names,
    preference_matches_categories,
)
from ..services.rag_service import TravelGuideRAG, get_travel_guide_rag
from ..services.spatial_planner import (
    SpatialItineraryPlanner,
    estimate_leg,
    get_pace_profile,
    haversine_meters,
)

logger = logging.getLogger("uvicorn.error")

@dataclass
class _POICandidate:
    """A POI plus the planning metadata that raw map results do not contain."""

    poi: POIInfo
    matched_preferences: Set[str] = field(default_factory=set)
    categories: Set[str] = field(default_factory=set)
    is_must_visit: bool = False
    score: float = 0
    recall_sources: Set[str] = field(default_factory=set)
    score_breakdown: dict[str, float] = field(default_factory=dict)


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
        self.identity_resolver = POIIdentityResolver()

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
        keywords = request.preferences or [
            "经典景点",
            "城市地标",
            "历史名胜",
            "自然景观",
        ]
        for keyword in keywords[:4]:
            for poi in self._safe_search(keyword, request.city):
                poi = enrich_poi(poi, request.city)
                candidates.append(
                    _POICandidate(
                        poi=poi,
                        matched_preferences={keyword},
                        categories=classify_poi(poi.name, poi.type),
                        recall_sources={f"search:{keyword}"},
                    )
                )

        if build_preference_profile(request).prefer_classic:
            # First-time visitors need explicit core recall; relying on generic
            # interest queries can over-represent nearby niche venues.
            for classic_name in get_city_core_names(request.city):
                for poi in self._safe_search(classic_name, request.city):
                    poi = enrich_poi(poi, request.city)
                    if place_names_match(classic_name, poi.name):
                        candidates.append(
                            _POICandidate(
                                poi=poi,
                                categories=classify_poi(poi.name, poi.type),
                                recall_sources={"classic_baseline"},
                            )
                        )
                        break

        # Must-visits get a dedicated recall pass. A search result is only
        # promoted when its name actually matches what the user requested.
        for must_visit in request.must_visit:
            matches = [
                enrich_poi(poi, request.city)
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
                        recall_sources={"must_visit"},
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
            and not self._is_meal_poi(candidate.poi)
        ]

    @staticmethod
    def _is_meal_poi(poi: POIInfo) -> bool:
        text = f"{poi.name} {poi.type}".lower()
        return any(
            term in text
            for term in (
                "餐厅",
                "餐馆",
                "饭店",
                "美食",
                "小吃",
                "咖啡",
                "restaurant",
                "food",
            )
        )

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
            existing.recall_sources.update(candidate.recall_sources)
            if self._poi_quality(candidate.poi) > self._poi_quality(existing.poi):
                existing.poi = candidate.poi
        return unique

    def _score_candidates(
        self, candidates: List[_POICandidate], request: TripRequest
    ) -> List[_POICandidate]:
        """按 基础质量 + 经典加成 + 偏好加分 - 小众扣分 四维打分。"""

        classic_names = get_city_classic_names(request.city)

        for candidate in candidates:
            meta = candidate.poi

            # --- 是否在人工精选的经典名单中？ ---
            is_curated = any(
                curated_name in meta.name
                for curated_name in classic_names
            ) and meta.first_visit_priority >= 7
            # --- 是否是非经典的小众博物馆？ ---
            is_unknown_museum = (
                "博物馆" in meta.name and not is_curated
            )
            # --- 用户偏好是否命中该POI的分类？ ---
            # 合并两份来源：metadata.categories（精选名单里的人工分类）
            # 和 classify_poi 结果（高德 type → 标准分类），任一命中即算匹配
            metadata_cats = (
                set(meta.categories) if meta.categories else set()
            )
            all_categories = metadata_cats | candidate.categories
            pref_matched = preference_matches_categories(
                request.preferences, all_categories
            )

            # 1. 基础质量分（0-60）：纯靠 popularity
            base_quality = meta.popularity * 6

            # 2. 经典加成（0 或 30）
            classic_bonus = 30 if is_curated else 0

            # 3. 偏好加分（0 或 10）
            pref_bonus = 10 if pref_matched else 0

            # 4. 小众博物馆扣分（0 或 -40）
            niche_penalty = -40 if is_unknown_museum else 0

            score = base_quality + classic_bonus + pref_bonus + niche_penalty
            score = max(0, min(100, score))

            if candidate.is_must_visit:
                score = max(score, 100)

            candidate.score = score
            candidate.score_breakdown = {
                "base_quality": base_quality,
                "classic_bonus": classic_bonus,
                "pref_bonus": pref_bonus,
                "niche_penalty": niche_penalty,
                "is_curated": is_curated,
            }

        logger.info("========== 候选景点打分明细 ==========")
        for c in candidates:
            logger.info(
                "景点: %s | is_must_visit=%s | score=%.1f | curated=%s | "
                "base=%s classic=%s pref=%s niche=%s",
                c.poi.name,
                c.is_must_visit,
                c.score,
                c.score_breakdown.get("is_curated", False),
                c.score_breakdown["base_quality"],
                c.score_breakdown["classic_bonus"],
                c.score_breakdown["pref_bonus"],
                c.score_breakdown["niche_penalty"],
            )
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
            coordinates=poi.coordinates
            or [poi.location.latitude, poi.location.longitude],
            visit_duration=self._duration_for(request.pace),
            suggested_duration_minutes=poi.duration_minutes,
            description=description,
            category=poi.type,
            categories=poi.categories,
            area=poi.area,
            popularity=poi.popularity,
            first_visit_priority=poi.first_visit_priority,
            crowd_level=poi.crowd_level,
            rating=poi.rating,
            poi_id=poi.id,
            parent_poi_id=poi.parent_poi_id,
            visit_key=poi.visit_key,
            ticket_price=poi.ticket_price,
            score=candidate.score,
            recall_sources=sorted(candidate.recall_sources),
            score_breakdown=candidate.score_breakdown,
            opening_hours=poi.opening_hours,
            opening_time=poi.opening_time,
            closing_time=poi.closing_time,
            latest_entry_time=poi.latest_entry_time,
            hours_source=poi.hours_source,
        )

    def collect_hotels(self, request: TripRequest) -> List[Hotel]:
        keyword = f"{request.hotel_area or request.city} {request.accommodation} 酒店"
        accommodation = request.accommodation.lower()
        if "经济" in accommodation or any(
            term in accommodation for term in ("budget", "economy", "hostel")
        ):
            nightly_cost = 350
        elif "舒适" in accommodation or any(
            term in accommodation for term in ("comfortable", "comfort")
        ):
            nightly_cost = 650
        else:
            nightly_cost = 1000
        if request.budget_limit is not None:
            daily_budget = request.budget_limit / max(1, request.travel_days)
            nightly_cost = min(
                nightly_cost,
                max(150, int(daily_budget * 0.5)),
            )
        pois = self._safe_search(keyword, request.city)
        # A keyword search can return nearby attractions, malls, or ordinary
        # restaurants. Those are not accommodation evidence and must never
        # become a numbered "recommended hotel" or a route anchor.
        lodging_pois = [poi for poi in pois if self._is_lodging_poi(poi)]
        if not lodging_pois:
            return [
                Hotel(
                    name=f"{request.hotel_area or request.city}待确认酒店",
                    type=request.accommodation,
                    estimated_cost=nightly_cost,
                    distance="未获取可验证的住宿 POI；请在出发前确认酒店",
                )
            ]
        return [
            Hotel(
                name=poi.name,
                address=poi.address,
                location=poi.location,
                price_range=f"{nightly_cost}-{nightly_cost + 200}元/晚",
                rating=str(poi.rating or 4.5),
                distance="待行程联合评估",
                type=request.accommodation,
                estimated_cost=nightly_cost,
            )
            for poi in lodging_pois[:5]
        ]

    @staticmethod
    def _is_lodging_poi(poi: POIInfo) -> bool:
        """Require provider evidence that a POI is an accommodation venue."""

        text = f"{poi.name} {poi.type}".lower()
        lodging_terms = (
            "住宿", "酒店", "宾馆", "旅馆", "民宿", "客栈", "度假村",
            "hotel", "hostel", "motel", "resort", "inn", "accommodation",
        )
        return any(term in text for term in lodging_terms)

    def collect_hotel(self, request: TripRequest) -> Hotel:
        """Backward-compatible single-hotel facade."""

        return self.collect_hotels(request)[0]

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
        return self.identity_resolver.same_visit_entity(left, right)

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

        route_nodes = [
            (item.name, item.address, item.location) for item in attractions
        ]
        if attractions and day.hotel and day.hotel.location:
            hotel_node = (day.hotel.name, day.hotel.address, day.hotel.location)
            route_nodes = [hotel_node, *route_nodes, hotel_node]

        for origin, destination in zip(route_nodes, route_nodes[1:]):
            origin_name, origin_address, origin_location = origin
            destination_name, destination_address, destination_location = destination
            leg_estimate = estimate_leg(
                origin_location, destination_location, day.transportation
            )
            route_type = leg_estimate.mode
            if (
                route_type == "walking"
                and day.max_walking_leg_minutes is not None
                and leg_estimate.travel_minutes
                > day.max_walking_leg_minutes
            ):
                route_type = "transit"
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
                    severity="blocker" if not passed else "info",
                    message="步行强度可接受" if passed else "某天步行距离偏长，建议替换相邻景点或改用打车",
                )
            )

        all_names = [attr.name for day in plan.days for attr in day.attractions]
        empty_days = [
            day.day_index + 1
            for day in plan.days
            if not day.attractions
            and not self._reasonable_arrival_or_departure_empty_day(
                day.day_index,
                len(plan.days),
                request,
            )
        ]
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

        count_violations = []
        remote_solo_days = []
        for day in plan.days:
            count = len(day.attractions)
            is_remote_solo = (
                count == 1
                and day.daily_travel_minutes
                > SpatialItineraryPlanner.remote_travel_threshold_minutes
            )
            if is_remote_solo:
                remote_solo_days.append(day.day_index + 1)
            elif count and not 2 <= count <= 5:
                count_violations.append(f"Day{day.day_index + 1}={count}个")
        count_passed = not count_violations
        items.append(
            ConstraintItem(
                name="每日景点数量",
                passed=count_passed,
                actual=(
                    "每天2-5个景点"
                    if count_passed and not remote_solo_days
                    else (
                        f"远距离单景点日：{', '.join(map(str, remote_solo_days))}"
                        if count_passed
                        else "; ".join(count_violations)
                    )
                ),
                expected="通常每天2-5个景点；往返交通超过120分钟的远距离景点可单独成日",
                severity=(
                    "warning"
                    if remote_solo_days or not count_passed
                    else "info"
                ),
                message=(
                    "每日景点数量合理"
                    if count_passed and not remote_solo_days
                    else "远距离景点交通成本较高，建议单独安排一天"
                    if count_passed
                    else "普通游览日景点过少，需要继续从当前区域按时间预算填充"
                ),
            )
        )

        low_utilization_days = [
            day
            for day in plan.days
            if day.attractions
            and day.day_utilization_score < 70
            and not (
                len(day.attractions) == 1
                and day.daily_travel_minutes
                > SpatialItineraryPlanner.remote_travel_threshold_minutes
            )
        ]
        items.append(
            ConstraintItem(
                name="每日行程利用率",
                passed=True,
                actual=(
                    "所有普通游览日达到70%"
                    if not low_utilization_days
                    else "; ".join(
                        f"Day{day.day_index + 1}={day.day_utilization_score:.0f}%"
                        for day in low_utilization_days
                    )
                ),
                expected="普通游览日目标利用率70%-90%",
                severity="warning" if low_utilization_days else "info",
                message=(
                    "每日时间利用充分"
                    if not low_utilization_days
                    else "部分日期仍有空余时间，可继续添加同区域景点"
                ),
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

        area_violations = []
        for day in plan.days:
            areas = sorted({item.area for item in day.attractions if item.area})
            if len(areas) > 2:
                area_violations.append(
                    f"Day{day.day_index + 1}({', '.join(areas)})"
                )
        area_passed = not area_violations
        items.append(
            ConstraintItem(
                name="每日区域聚合",
                passed=area_passed,
                actual=(
                    "每天不超过2个区域"
                    if area_passed
                    else "; ".join(area_violations)
                ),
                expected="一天集中在1个主要区域，最多跨2个相邻区域",
                severity="warning" if not area_passed else "info",
                message=(
                    "路线区域集中"
                    if area_passed
                    else "当天区域跳跃过多，需要移动或删除低优先级景点"
                ),
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
            passed=all(
                item.passed
                or item.name
                in {
                    "每日景点数量",
                    "每日区域聚合",
                    "每日行程利用率",
                }
                for item in items
            ),
            score=round(passed_count / len(items), 3),
            items=items,
        )

    @staticmethod
    def _reasonable_arrival_or_departure_empty_day(
        day_index: int,
        day_count: int,
        request: TripRequest,
    ) -> bool:
        start = _clock_minutes(request.daily_start_time, 9 * 60)
        end = _clock_minutes(request.daily_end_time, 20 * 60)
        if day_index == 0 and request.arrival_time:
            start = max(start, _clock_minutes(request.arrival_time, start))
        if day_index == day_count - 1 and request.departure_time:
            end = min(end, _clock_minutes(request.departure_time, end))
        has_actual_boundary = (
            day_index == 0
            and bool(request.arrival_time)
            or day_index == day_count - 1
            and bool(request.departure_time)
        )
        return has_actual_boundary and max(0, end - start) < 180


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

        distance_score = self._distance_score(plan, request)
        time_score = self._time_score(plan, request)
        experience_score = self._experience_score(plan, request)
        area_score = self._area_score(plan)
        preference_score = self._preference_score(plan, request)
        diversity_score = self._diversity_score(plan)
        budget_score = self._budget_score(plan)
        route_score = round(
            experience_score * 0.3
            + distance_score * 0.25
            + time_score * 0.2
            + area_score * 0.1
            + diversity_score * 0.1
            + budget_score * 0.05
        )
        review_warnings: List[str] = []
        for day in plan.days:
            if day.daily_distance_km > 35:
                review_warnings.append(
                    f"Day{day.day_index + 1}移动距离较长（{day.daily_distance_km:.1f}km）"
                )
            areas = {item.area for item in day.attractions if item.area}
            if len(areas) > 2:
                review_warnings.append(
                    f"Day{day.day_index + 1}跨越{len(areas)}个区域"
                )
            if self._day_ratio(day, request) > 1:
                review_warnings.append(
                    f"Day{day.day_index + 1}超过每日可用时间"
                )
            if (
                day.day_utilization_score < 70
                and not (
                    len(day.attractions) == 1
                    and day.daily_travel_minutes
                    > SpatialItineraryPlanner.remote_travel_threshold_minutes
                )
            ):
                review_warnings.append(
                    f"Day{day.day_index + 1}行程利用率仅"
                    f"{day.day_utilization_score:.0f}%"
                )
            if (
                len(day.attractions) == 1
                and day.daily_travel_minutes
                > SpatialItineraryPlanner.remote_travel_threshold_minutes
            ):
                review_warnings.append(
                    f"Day{day.day_index + 1}为远距离景点日，"
                    "附近没有可在时间预算内加入的候选景点"
                )
        plan.review_scores = PlanReviewScores(
            score=route_score,
            route_score=route_score,
            distance_score=distance_score,
            time_score=time_score,
            experience_score=experience_score,
            preference_score=preference_score,
            diversity_score=diversity_score,
            budget_score=budget_score,
            score_breakdown={
                "preference": preference_score,
                "distance": distance_score,
                "diversity": diversity_score,
                "budget": budget_score,
                "time": time_score,
                "experience": experience_score,
            },
            warnings=sorted(set(review_warnings)),
        )
        warnings.extend(review_warnings)
        plan.risk_warnings = sorted(set(warnings))
        return plan

    @staticmethod
    def _day_ratio(day: DayPlan, request: TripRequest) -> float:
        actual = (
            day.daily_elapsed_minutes
            if request.daily_start_time and request.daily_end_time
            else day.daily_duration_minutes
        )
        return actual / max(1, daily_time_budget_minutes(request))

    def _time_score(self, plan: TripPlan, request: TripRequest) -> int:
        worst_ratio = max(
            (self._day_ratio(day, request) for day in plan.days),
            default=0,
        )
        if worst_ratio <= 0.85:
            return 100
        return max(0, round(100 - (worst_ratio - 0.85) * 154))

    @staticmethod
    def _distance_score(plan: TripPlan, request: TripRequest) -> int:
        if not plan.days:
            return 100
        max_distance = max((day.daily_distance_km for day in plan.days), default=0)
        max_walking = max(
            (day.daily_walking_distance_km for day in plan.days), default=0
        )
        # A long but coherent suburban day is a warning, not automatically a
        # bad route. Area jumping and walking overruns are penalized separately.
        distance_score = max(
            0, round(100 - max(0, max_distance - 12) * 0.5)
        )
        if request.max_daily_walk_km is not None and request.max_daily_walk_km >= 0:
            walking_overrun = max(0, max_walking - request.max_daily_walk_km)
            distance_score -= round(
                min(40, walking_overrun * 10)
            )
        return max(0, distance_score)

    @staticmethod
    def _experience_score(plan: TripPlan, request: TripRequest) -> int:
        attractions = [
            attraction for day in plan.days for attraction in day.attractions
        ]
        if not attractions:
            return 0
        profile = build_preference_profile(request)
        values = [
            attraction.score
            if attraction.score > 0
            else attraction.popularity * 7
            + (attraction.first_visit_priority * 3 if profile.first_visit else 0)
            for attraction in attractions
        ]
        return max(0, min(100, round(sum(values) / len(values))))

    @staticmethod
    def _preference_score(plan: TripPlan, request: TripRequest) -> int:
        attractions = [item for day in plan.days for item in day.attractions]
        if not attractions:
            return 0
        if not request.preferences:
            return 100
        values = [
            item.score_breakdown.get("preference_score", 0)
            for item in attractions
        ]
        return max(0, min(100, round(sum(values) / len(values) / 35 * 100)))

    @staticmethod
    def _diversity_score(plan: TripPlan) -> int:
        repeats = 0
        for day in plan.days:
            counts = {}
            for item in day.attractions:
                category = AttractionScorer.category_bucket(item)
                counts[category] = counts.get(category, 0) + 1
            repeats += sum(max(0, count - 1) for count in counts.values())
        return max(0, 100 - repeats * 15)

    @staticmethod
    def _budget_score(plan: TripPlan) -> int:
        attractions = [item for day in plan.days for item in day.attractions]
        if not attractions:
            return 100
        penalty = sum(
            -item.score_breakdown.get("budget_penalty", 0)
            for item in attractions
        ) / len(attractions)
        return max(0, min(100, round(100 - penalty * 4)))

    @staticmethod
    def _area_score(plan: TripPlan) -> int:
        penalties = []
        for day in plan.days:
            area_count = len({item.area for item in day.attractions if item.area})
            penalties.append(max(0, area_count - 1) * 15)
        return max(0, 100 - max(penalties, default=0))


class MultiAgentTripPlanner:
    """Coordinates deterministic planner roles and optional local RAG evidence."""

    max_planning_iterations = 3
    max_quality_repair_iterations = 3

    def _identity(self) -> POIIdentityResolver:
        resolver = getattr(self, "identity_resolver", None)
        if resolver is None:
            resolver = POIIdentityResolver()
            self.identity_resolver = resolver
        return resolver

    def _candidate_policy(self) -> CandidateAcceptancePolicy:
        policy = getattr(self, "candidate_acceptance_policy", None)
        if policy is None:
            policy = CandidateAcceptancePolicy()
            self.candidate_acceptance_policy = policy
        return policy

    @staticmethod
    def _daily_time_budget(request: TripRequest) -> int:
        return daily_time_budget_minutes(request)

    @staticmethod
    def _city_planning_reference(city: str) -> Location:
        """Neutral geographic reference, never presented as a hotel."""

        centers = {
            "北京": (116.397128, 39.916527),
            "上海": (121.473701, 31.230416),
            "杭州": (120.155070, 30.274084),
            "成都": (104.066541, 30.572269),
            "广州": (113.264385, 23.129112),
            "深圳": (114.057868, 22.543099),
        }
        longitude, latitude = centers.get(city, (116.397128, 39.916527))
        return Location(longitude=longitude, latitude=latitude)

    def __init__(self):
        self.amap_service = get_amap_service()
        self.rag = get_travel_guide_rag()
        self.poi_collector = POICollector(self.amap_service)
        self.identity_resolver = POIIdentityResolver()
        self.accommodation_selector = AccommodationSelector()
        self.candidate_acceptance_policy = CandidateAcceptancePolicy()
        self.attraction_scorer = AttractionScorer()
        self.spatial_planner = SpatialItineraryPlanner()
        self.route_evaluator = RouteEvaluator(self.amap_service)
        self.budget_estimator = BudgetEstimator()
        self.constraint_checker = ConstraintChecker()
        self.constraint_extractor = ConstraintExtractor()
        self.constraint_validator = ConstraintValidationEngine()
        self.reviewer = PlannerReviewer()
        self.completeness_gate = ItineraryCompletenessGate(
            self.identity_resolver
        )
        self.repair_controller = RepairController(self)
        self.travel_knowledge = get_travel_knowledge_service()

    def plan_trip(
        self,
        request: TripRequest,
        *,
        emit_observability: bool = True,
    ) -> TripPlan:
        attractions = self.poi_collector.collect_attractions(request)
        hotel_candidates = self.poi_collector.collect_hotels(request)
        hotel = self.accommodation_selector.select(
            request, hotel_candidates, attractions
        )
        weather = self._weather_for_dates(request)
        query = self.build_rag_query(request)
        try:
            evidence = self.rag.search(
                request.city, query, top_k=5,
                metadata=self.build_rag_metadata(request),
            )
        except TypeError:
            # Keep compatibility with injected legacy RAG doubles and older
            # implementations while the metadata-aware boundary rolls out.
            evidence = self.rag.search(request.city, query, top_k=5)

        plan = self.build_plan_from_inputs(
            request=request,
            attractions=attractions,
            hotel=hotel,
            weather=weather,
            evidence=evidence,
            hotel_candidates=hotel_candidates,
        )
        refresh_planning_trace(plan, run_type="deterministic")
        if emit_observability:
            emit_planning_trace(plan.observability_trace)
        return plan

    def build_plan_from_inputs(
        self,
        request: TripRequest,
        attractions: List[Attraction],
        hotel: Hotel,
        weather: List[WeatherInfo],
        evidence: List[EvidenceSource],
        constraint_set: ConstraintSet | None = None,
        hotel_candidates: Sequence[Hotel] = (),
    ) -> TripPlan:
        """Build a plan from specialist outputs without searching again."""

        constraint_set = constraint_set or self.constraint_extractor.extract(
            request
        )
        effective_request = self._request_for_constraints(
            request, constraint_set
        )
        profile = build_preference_profile(effective_request)
        attractions = self.ensure_candidate_coverage(
            effective_request, attractions
        )
        attractions = [
            enrich_attraction(
                deepcopy(attraction), effective_request, profile
            )
            for attraction in attractions
        ]
        if settings.enable_travel_knowledge:
            attractions = [
                self.travel_knowledge.enrich_attraction(item, effective_request)
                for item in attractions
            ]
        attractions = self._identity().deduplicate(
            attractions,
            must_visit=effective_request.must_visit,
        )
        # A missing verified hotel must not become a fake hotel route anchor.
        # Use an internal city reference only for candidate ranking and
        # clustering so a remote landmark does not win simply because no
        # origin was known. The DayPlan still carries the unlocated
        # "待确认酒店" and route metrics do not claim hotel legs.
        planning_reference = hotel.location or self._city_planning_reference(
            effective_request.city
        )
        attractions = self.attraction_scorer.score_candidates(
            attractions,
            effective_request,
            planning_reference,
            daily_time_budget_minutes(effective_request),
            weather_risks=[
                f"{item.day_weather} {item.night_weather}" for item in weather
            ],
        )
        scored_candidates = list(attractions)
        logger.info("========== 路线候选评分（最终） ==========")
        for item in sorted(attractions, key=lambda item: (-item.score, item.name))[:12]:
            logger.info(
                "景点: %s | score=%.1f | base=%.1f | distance=%.1f | diversity=%.1f",
                item.name,
                item.score,
                item.score_breakdown.get("selection_base_score", 0),
                item.score_breakdown.get("distance_penalty", 0),
                item.score_breakdown.get("diversity_penalty", 0),
            )
        attractions = self._filter_by_planning_constraints(
            attractions, effective_request, constraint_set
        )
        self._apply_conservative_opening_hours(attractions)
        days = self._assign_days(
            effective_request,
            attractions,
            hotel,
            constraint_set,
            planning_reference=planning_reference,
        )
        plan = TripPlan(
            city=effective_request.city,
            start_date=effective_request.start_date,
            end_date=effective_request.end_date,
            days=days,
            weather_info=weather,
            overall_suggestions=self._build_suggestions(request, evidence),
            evidence_sources=evidence,
            risk_warnings=[],
            normalized_constraints=constraint_set.constraints,
            candidate_debug=[
                CandidateScoreDebug(
                    name=item.name,
                    category=AttractionScorer.category_bucket(item),
                    tags=item.tags,
                    score=item.score,
                    score_breakdown=item.score_breakdown,
                )
                for item in sorted(
                    attractions, key=lambda item: (-item.score, item.name)
                )[:20]
            ],
        )
        plan = self._repair_until_stable(
            plan, effective_request, attractions
        )
        plan = self.reviewer.review(plan, effective_request)
        if plan.review_scores.route_score < 70 or self._has_underfilled_day(
            plan
        ):
            score_before = plan.review_scores.route_score / 100
            self._repair_low_soft_score(
                plan, effective_request, attractions
            )
            self._recalculate(plan, effective_request)
            plan = self.reviewer.review(plan, effective_request)
            plan.planning_trace.append(
                PlanningTraceItem(
                    iteration=len(plan.planning_trace),
                    role="SoftReview",
                    action="replan_low_quality_or_underfilled_day",
                    reason=(
                        "Re-ran deterministic daily filling because route "
                        "quality was below 70 or a normal day was under 70% utilized"
                    ),
                    score_before=score_before,
                    score_after=plan.review_scores.route_score / 100,
                )
            )
        plan = self.run_quality_loop(
            plan,
            effective_request,
            attractions,
            hotel_candidates=hotel_candidates or [hotel],
        )
        curate_user_warnings(plan)
        plan.observability_trace = build_planning_trace(
            effective_request,
            plan,
            candidates=scored_candidates,
            eligible_candidates=attractions,
            run_type="deterministic_build",
        )
        self._log_finalized_itinerary(plan)
        return plan

    @staticmethod
    def _log_finalized_itinerary(plan: TripPlan) -> None:
        logger.info(
            "========== finalize 后日程选点 ==========%s",
            [
                {
                    "day": day.day_index + 1,
                    "pois": [
                        {
                            "name": item.name,
                            "visit_key": item.visit_key,
                            "role": item.selection_role,
                            "score": round(item.score, 2),
                        }
                        for item in day.attractions
                    ],
                }
                for day in plan.days
            ],
        )

    def run_quality_loop(
        self,
        plan: TripPlan,
        request: TripRequest,
        available_attractions: Sequence[Attraction],
        *,
        hotel_candidates: Sequence[Hotel] = (),
        external_evaluator: Callable[[TripPlan], ExperienceEvaluation | None] | None = None,
        initial_external_evaluation: ExperienceEvaluation | None = None,
    ) -> TripPlan:
        """Bounded repair using one sandboxed candidate/commit path."""
        gate = getattr(self, "completeness_gate", None) or ItineraryCompletenessGate(self._identity())
        self.completeness_gate = gate
        controller = getattr(self, "repair_controller", None) or RepairController(self)
        self.repair_controller = controller
        current = plan.model_copy(deep=True)
        self._recalculate(current, request)
        for day in current.days:
            if not day.primary_plan:
                day.primary_plan = deepcopy(day.attractions)

        def evaluate(candidate: TripPlan) -> ExperienceEvaluation:
            external = external_evaluator(candidate) if external_evaluator else None
            return gate.merge(gate.evaluate(request, candidate, available_attractions), external)

        evaluation = gate.merge(
            gate.evaluate(request, current, available_attractions),
            initial_external_evaluation or (
                external_evaluator(current) if external_evaluator else None
            ),
        )
        attempted = {item.issue_fingerprint or item.trigger_issue.fingerprint for item in current.repair_history}
        sandbox = PlanMutationSandbox(
            recalculate=self._recalculate,
            evaluate=evaluate,
            hard_snapshot=lambda candidate: {
                "hard_pass": self._hard_constraints_pass(candidate),
                "hard_keys": sorted(self._hard_violation_keys(candidate)),
            },
            pipeline_mode="deterministic",
            run_id=(current.observability_trace.run_id if current.observability_trace else ""),
        )
        for iteration in range(1, self.max_quality_repair_iterations + 1):
            trigger = self._next_quality_issue(evaluation, attempted)
            if trigger is None:
                break
            attempted.add(trigger.fingerprint)
            proposal_box = [None]
            scope = RepairController.mutation_scope(trigger.repair_strategy, trigger.day)

            def mutate(candidate: TripPlan):
                proposal = controller.propose(
                    candidate, request, available_attractions,
                    hotel_candidates, trigger,
                )
                proposal_box[0] = proposal
                return proposal.plan if proposal is not None else False

            def after_recalculate(candidate: TripPlan) -> None:
                self.reviewer.review(candidate, request)

            def additional_gate(before, candidate, before_eval, after_eval):
                proposal = proposal_box[0]
                fields = self._repair_modified_fields(before, candidate)
                if self._scope_violations(fields, proposal.mutation_scope or scope):
                    return CommitDecision(CommitDecisionCode.INVALID_CANDIDATE, "MUTATION_SCOPE_VIOLATION")
                if any(item.severity in {"high", "critical"} and item.fingerprint not in {x.fingerprint for x in before_eval.issues if x.severity in {"high", "critical"}} for item in after_eval.issues):
                    return CommitDecision(CommitDecisionCode.QUALITY_REGRESSION, "NEW_HIGH_ISSUE")
                if not trigger.is_blocking:
                    for old, new in zip(before.days, candidate.days):
                        if new.daily_travel_minutes > old.daily_travel_minutes + (0 if trigger.repair_strategy == RepairStrategy.ADD_WEATHER_BACKUP else 15):
                            return CommitDecision(CommitDecisionCode.QUALITY_REGRESSION, "TRANSPORT_REGRESSION")
                        if new.daily_walking_distance_km > old.daily_walking_distance_km + (0.01 if trigger.repair_strategy == RepairStrategy.ADD_WEATHER_BACKUP else 0.5):
                            return CommitDecision(CommitDecisionCode.QUALITY_REGRESSION, "WALKING_REGRESSION")
                return None

            outcome = sandbox.execute(
                base_plan=current, current_plan=current, request=request,
                issue=trigger, strategy=trigger.repair_strategy, mutate=mutate,
                before_evaluation=evaluation,
                after_recalculate=after_recalculate,
                additional_gate=additional_gate,
            )
            proposal = proposal_box[0]
            candidate = outcome.candidate or current
            after_evaluation = outcome.after_evaluation or evaluation
            fields = self._repair_modified_fields(current, candidate)
            accepted = outcome.decision.accepted
            reason = outcome.decision.reason or ("" if accepted else outcome.decision.code.value)
            if not self._hard_constraints_pass(candidate):
                reason = "Repair rejected because full hard-constraint validation failed"
            elif reason == "MUTATION_SCOPE_VIOLATION":
                reason = "Mutation scope violation"
            scope_rejected = reason == "Mutation scope violation"
            record = RepairIteration(
                iteration=iteration, trigger_issue=trigger, issue_fingerprint=trigger.fingerprint,
                selected_action=(proposal.action if proposal else "no_legal_repair_available"),
                action_result=(
                    "accepted; plan revalidated" if accepted else
                    "rolled_back_before_recalculation" if scope_rejected else
                    "rejected transactionally; previous best plan retained"
                ),
                accepted=accepted, rejection_reason=reason,
                changes=self._quality_plan_changes(current, candidate),
                constraint_pass_before=self._hard_constraints_pass(current),
                constraint_pass_after=self._hard_constraints_pass(candidate) if outcome.candidate else self._hard_constraints_pass(current),
                quality_score_before=evaluation.overall_score,
                quality_score_after=after_evaluation.overall_score,
                issues_before=evaluation.issues, issues_after=after_evaluation.issues,
                mutation_scope=list(proposal.mutation_scope if proposal else scope),
                actual_modified_fields=fields,
                itinerary_before=self._itinerary_snapshot(current),
                itinerary_after=self._itinerary_snapshot(candidate),
                constraint_delta={
                    "added": sorted(self._hard_violation_keys(candidate) - self._hard_violation_keys(current)),
                    "removed": sorted(self._hard_violation_keys(current) - self._hard_violation_keys(candidate)),
                },
                issue_delta=self._issue_delta(evaluation, after_evaluation),
                rollback_reason=reason if not accepted else "",
            )
            if accepted:
                candidate.repair_history = [*current.repair_history, record]
                candidate.repair_attempts = [*current.repair_attempts, outcome.attempt]
                for day in candidate.days:
                    if f"days[{day.day_index}].attractions" in fields:
                        day.primary_plan = deepcopy(day.attractions)
                current, evaluation = candidate, after_evaluation
            else:
                current.repair_history.append(record)
                current.repair_attempts.append(outcome.attempt)
                if proposal is None:
                    continue
                break
        return self._finalize_quality_loop(current, request, available_attractions, evaluation, external_evaluator)

    def _finalize_quality_loop(
        self,
        current: TripPlan,
        request: TripRequest,
        available_attractions: Sequence[Attraction],
        evaluation: ExperienceEvaluation,
        external_evaluator: Callable[[TripPlan], ExperienceEvaluation | None] | None,
    ) -> TripPlan:
        """Apply final best-effort/degraded semantics after sandbox attempts."""
        gate = self.completeness_gate
        final_evaluation = gate.merge(
            gate.evaluate(request, current, available_attractions),
            external_evaluator(current) if external_evaluator else None,
        )
        hard_pass = self._hard_constraints_pass(current)
        current.quality_evaluation = final_evaluation
        unresolved = []
        for issue in final_evaluation.issues:
            if issue.resolution_status in {"resolved", "mitigated"}:
                continue
            issue.resolution_status = "unresolved_blocking" if issue.is_blocking else "unresolved_non_blocking"
            unresolved.append(issue)
        blocking = [item for item in unresolved if item.is_blocking]
        warnings = [item for item in unresolved if not item.is_blocking]
        current.quality_gate_passed = hard_pass and not blocking
        current.unresolved_quality_issues = unresolved
        current.unresolved_blocking_issues = blocking
        current.unresolved_non_blocking_issues = warnings
        current.best_effort = bool(unresolved)
        current.suggested_alternatives = sorted(set([
            *current.suggested_alternatives,
            *[day.weather_warning for day in current.days if day.weather_warning],
            *[f"可选改进：{item.evidence}" for item in warnings],
            *[f"需处理后执行：{item.evidence}" for item in blocking],
        ]))
        if current.quality_gate_passed:
            current.failure_reason = None if hard_pass else current.failure_reason
            current.degraded_reason = None
            current.risk_warnings = sorted(set([*current.risk_warnings, *[item.evidence for item in warnings]]))
        else:
            current.failure_reason = current.failure_reason or "Quality gate unresolved after bounded repair"
            current.degraded_reason = current.failure_reason
        curate_user_warnings(current)
        return current

    def _run_quality_loop_legacy(
        self,
        plan: TripPlan,
        request: TripRequest,
        available_attractions: Sequence[Attraction],
        *,
        hotel_candidates: Sequence[Hotel] = (),
        external_evaluator: Callable[
            [TripPlan], ExperienceEvaluation | None
        ]
        | None = None,
        initial_external_evaluation: ExperienceEvaluation | None = None,
    ) -> TripPlan:
        """Run a bounded gate → directed repair → full revalidation loop."""

        gate = getattr(self, "completeness_gate", None)
        if gate is None:
            gate = ItineraryCompletenessGate(self._identity())
            self.completeness_gate = gate
        controller = getattr(self, "repair_controller", None)
        if controller is None:
            controller = RepairController(self)
            self.repair_controller = controller

        current = deepcopy(plan)
        self._recalculate(current, request)
        for day in current.days:
            if not day.primary_plan:
                day.primary_plan = deepcopy(day.attractions)
        external = initial_external_evaluation
        if external is None and external_evaluator is not None:
            external = external_evaluator(current)
        evaluation = gate.merge(
            gate.evaluate(request, current, available_attractions),
            external,
        )

        attempted = {
            item.issue_fingerprint or item.trigger_issue.fingerprint
            for item in current.repair_history
        }
        for iteration in range(1, self.max_quality_repair_iterations + 1):
            trigger = self._next_quality_issue(evaluation, attempted)
            if trigger is None:
                break
            attempted.add(trigger.fingerprint)
            before = deepcopy(current)
            proposal = controller.propose(
                current,
                request,
                available_attractions,
                hotel_candidates,
                trigger,
            )
            if proposal is None:
                scope = RepairController.mutation_scope(
                    trigger.repair_strategy,
                    trigger.day,
                )
                current.repair_history.append(
                    RepairIteration(
                        iteration=iteration,
                        trigger_issue=trigger,
                        issue_fingerprint=trigger.fingerprint,
                        selected_action="no_legal_repair_available",
                        action_result="No supported mutation was available",
                        accepted=False,
                        rejection_reason=(
                            "No repair could be produced from the existing "
                            "candidate pool and deterministic planner actions"
                        ),
                        changes=[],
                        constraint_pass_before=current.constraint_report.passed,
                        constraint_pass_after=current.constraint_report.passed,
                        quality_score_before=evaluation.overall_score,
                        quality_score_after=evaluation.overall_score,
                        issues_before=evaluation.issues,
                        issues_after=evaluation.issues,
                        mutation_scope=list(scope),
                        actual_modified_fields=[],
                        itinerary_before=self._itinerary_snapshot(current),
                        itinerary_after=self._itinerary_snapshot(current),
                        constraint_delta={"added": [], "removed": []},
                        issue_delta={"added": [], "removed": [], "remaining": [
                            item.fingerprint for item in evaluation.issues
                        ]},
                        rollback_reason="no_legal_repair_available",
                    )
                )
                continue

            candidate = proposal.plan
            proposal_scope = (
                proposal.mutation_scope
                or RepairController.mutation_scope(
                    trigger.repair_strategy,
                    trigger.day,
                )
            )
            proposal_fields = self._repair_modified_fields(before, candidate)
            scope_violations = self._scope_violations(
                proposal_fields,
                proposal_scope,
            )
            if scope_violations:
                reason = (
                    "Mutation scope violation for "
                    f"{trigger.repair_strategy.value}: "
                    + ", ".join(scope_violations)
                )
                current.repair_history.append(
                    RepairIteration(
                        iteration=iteration,
                        trigger_issue=trigger,
                        issue_fingerprint=trigger.fingerprint,
                        selected_action=proposal.action,
                        action_result=(
                            "rolled_back_before_recalculation"
                        ),
                        accepted=False,
                        rejection_reason=reason,
                        rollback_reason=reason,
                        changes=self._quality_plan_changes(before, candidate),
                        constraint_pass_before=self._hard_constraints_pass(before),
                        constraint_pass_after=self._hard_constraints_pass(before),
                        quality_score_before=evaluation.overall_score,
                        quality_score_after=evaluation.overall_score,
                        issues_before=evaluation.issues,
                        issues_after=evaluation.issues,
                        mutation_scope=list(proposal_scope),
                        actual_modified_fields=proposal_fields,
                        itinerary_before=self._itinerary_snapshot(before),
                        itinerary_after=self._itinerary_snapshot(candidate),
                        constraint_delta={"added": [], "removed": []},
                        issue_delta={"added": [], "removed": [], "remaining": [
                            item.fingerprint for item in evaluation.issues
                        ]},
                    )
                )
                break
            proposal_evaluation = gate.evaluate(
                request,
                candidate,
                available_attractions,
            )
            duplicate_introduced = any(
                item.issue_type == "duplicate_visit"
                for item in proposal_evaluation.issues
            )
            self._recalculate(candidate, request)
            candidate = self.reviewer.review(candidate, request)
            candidate_external = (
                external_evaluator(candidate)
                if external_evaluator is not None
                else None
            )
            after_evaluation = gate.merge(
                gate.evaluate(
                    request,
                    candidate,
                    available_attractions,
                ),
                candidate_external,
            )
            changes = self._quality_plan_changes(before, candidate)
            duplicate_after = any(
                item.issue_type == "duplicate_visit" and item.is_blocking
                for item in after_evaluation.issues
            )
            hard_pass_after = self._hard_constraints_pass(candidate)
            improved = self._quality_rank(after_evaluation) > self._quality_rank(
                evaluation
            )
            before_blocking = {
                item.fingerprint
                for item in evaluation.issues
                if item.is_blocking
            }
            after_blocking = {
                item.fingerprint
                for item in after_evaluation.issues
                if item.is_blocking
            }
            new_blocking = after_blocking - before_blocking
            before_hard = self._hard_violation_keys(before)
            after_hard = self._hard_violation_keys(candidate)
            new_hard = after_hard - before_hard
            before_high = {
                item.fingerprint
                for item in evaluation.issues
                if item.severity in {"high", "critical"}
            }
            after_high = {
                item.fingerprint
                for item in after_evaluation.issues
                if item.severity in {"high", "critical"}
            }
            new_high = after_high - before_high
            significant_transport_regression = [
                day_after.day_index + 1
                for day_before, day_after in zip(before.days, candidate.days)
                if day_before.attractions
                and (
                    day_after.daily_travel_minutes
                    - day_before.daily_travel_minutes
                    > max(60, day_before.daily_travel_minutes * 0.25)
                )
            ]
            soft_metric_regressions = []
            if not trigger.is_blocking:
                strict_non_mutating = (
                    trigger.repair_strategy
                    == RepairStrategy.ADD_WEATHER_BACKUP
                )
                for day_before, day_after in zip(before.days, candidate.days):
                    if (
                        day_after.daily_walking_distance_km
                        > day_before.daily_walking_distance_km
                        + (0.01 if strict_non_mutating else 0.5)
                    ):
                        soft_metric_regressions.append(
                            f"Day {day_after.day_index + 1} walking increased"
                        )
                    if (
                        day_after.daily_travel_minutes
                        > day_before.daily_travel_minutes
                        + (0 if strict_non_mutating else 15)
                    ):
                        soft_metric_regressions.append(
                            f"Day {day_after.day_index + 1} transport increased"
                        )
            utilization_regression = False
            if (
                trigger.repair_strategy
                not in {
                    RepairStrategy.RECLUSTER_ROUTE,
                    RepairStrategy.RESELECT_HOTEL,
                }
                and trigger.day
                and trigger.day <= len(candidate.days)
            ):
                utilization_regression = (
                    candidate.days[trigger.day - 1].day_utilization_score
                    + 0.1
                    < before.days[trigger.day - 1].day_utilization_score
                )
            target_improved = self._target_issue_improved(
                trigger,
                after_evaluation,
            )
            accepted = (
                hard_pass_after
                and not duplicate_introduced
                and not duplicate_after
                and not new_blocking
                and not new_hard
                and not new_high
                and not significant_transport_regression
                and not utilization_regression
                and not soft_metric_regressions
                and target_improved
                and improved
            )
            rejection_reason = ""
            if not hard_pass_after:
                rejection_reason = (
                    "Repair rejected because full hard-constraint validation failed"
                )
            elif duplicate_introduced or duplicate_after:
                rejection_reason = (
                    "Repair rejected because it attempted to violate the "
                    "duplicate invariant"
                )
            elif new_blocking:
                rejection_reason = (
                    "Repair rejected because it introduced new blocking "
                    "issues: "
                    + ", ".join(
                        fingerprint
                        for fingerprint in sorted(new_blocking)
                    )
                )
            elif new_hard:
                rejection_reason = (
                    "Repair introduced hard constraint violations: "
                    + ", ".join(sorted(new_hard))
                )
            elif new_high:
                rejection_reason = (
                    "Repair introduced new high/critical issues: "
                    + ", ".join(sorted(new_high))
                )
            elif significant_transport_regression:
                rejection_reason = (
                    "Repair significantly increased transport on Day "
                    + ", Day ".join(
                        str(day) for day in significant_transport_regression
                    )
                )
            elif utilization_regression:
                rejection_reason = (
                    f"Repair reduced target Day {trigger.day} utilization"
                )
            elif soft_metric_regressions:
                rejection_reason = (
                    "Soft-issue repair degraded primary metrics: "
                    + "; ".join(soft_metric_regressions)
                )
            elif not target_improved:
                rejection_reason = (
                    "Repair did not improve its target issue"
                )
            elif not improved:
                rejection_reason = (
                    "Repair did not improve overall quality"
                )

            record = RepairIteration(
                iteration=iteration,
                trigger_issue=trigger,
                issue_fingerprint=trigger.fingerprint,
                selected_action=proposal.action,
                action_result=(
                    "accepted; plan revalidated"
                    if accepted
                    else "rejected transactionally; previous best plan retained"
                ),
                accepted=accepted,
                rejection_reason=rejection_reason,
                changes=changes,
                constraint_pass_before=before.constraint_report.passed,
                constraint_pass_after=hard_pass_after,
                quality_score_before=evaluation.overall_score,
                quality_score_after=after_evaluation.overall_score,
                issues_before=evaluation.issues,
                issues_after=after_evaluation.issues,
                mutation_scope=list(proposal_scope),
                actual_modified_fields=proposal_fields,
                itinerary_before=self._itinerary_snapshot(before),
                itinerary_after=self._itinerary_snapshot(candidate),
                constraint_delta={
                    "added": sorted(after_hard - before_hard),
                    "removed": sorted(before_hard - after_hard),
                },
                issue_delta=self._issue_delta(
                    evaluation,
                    after_evaluation,
                ),
                rollback_reason=rejection_reason if not accepted else "",
            )
            # The handler only touched ``candidate`` (RepairController deep
            # copies its input).  Record the same candidate transaction in a
            # versioned envelope before swapping the formal current plan.
            attempt = RepairAttempt(
                attempt_id=uuid4().hex,
                base_plan_version=current.plan_version.version,
                issue_fingerprint=trigger.fingerprint,
                issue_type=trigger.issue_type,
                repair_strategy=trigger.repair_strategy,
                status=(
                    RepairAttemptStatus.COMMITTED
                    if accepted
                    else RepairAttemptStatus.ROLLED_BACK
                ),
                validation_before={
                    "hard_pass": self._hard_constraints_pass(before),
                    "hard_keys": sorted(self._hard_violation_keys(before)),
                },
                validation_after={
                    "hard_pass": hard_pass_after,
                    "hard_keys": sorted(after_hard),
                },
                quality_before={"score": evaluation.overall_score},
                quality_after={"score": after_evaluation.overall_score},
                quality_delta=round(
                    after_evaluation.overall_score - evaluation.overall_score, 4
                ),
                plan_diff=PlanMutationSandbox.diff(before, candidate),
                rollback_reason=rejection_reason if not accepted else "",
                committed_plan_version=(
                    current.plan_version.version + 1 if accepted else None
                ),
            )
            if accepted:
                for day in candidate.days:
                    if f"days[{day.day_index}].attractions" in proposal_fields:
                        day.primary_plan = deepcopy(day.attractions)
                candidate.repair_history = [*current.repair_history, record]
                candidate.repair_attempts = [*current.repair_attempts, attempt]
                candidate.plan_version = PlanVersionMetadata(
                    version=current.plan_version.version + 1,
                    parent_version=current.plan_version.version,
                    mutation_source="repair_controller",
                    mutation_action=trigger.repair_strategy.value,
                    mutation_reason=trigger.issue_type,
                    attempt_id=attempt.attempt_id,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                candidate.planning_trace.append(
                    PlanningTraceItem(
                        iteration=iteration,
                        role="QualityRepairController",
                        action=proposal.action,
                        reason=trigger.evidence,
                        score_before=evaluation.overall_score / 10,
                        score_after=after_evaluation.overall_score / 10,
                    )
                )
                current = candidate
                evaluation = after_evaluation
            else:
                current.repair_history.append(record)
                current.repair_attempts.append(attempt)
                # The fingerprint prevents this failed action from being retried.
                break

        final_external = (
            external_evaluator(current)
            if external_evaluator is not None
            else external
        )
        final_evaluation = gate.merge(
            gate.evaluate(request, current, available_attractions),
            final_external,
        )
        hard_pass = self._hard_constraints_pass(current)
        current.quality_evaluation = final_evaluation
        unresolved = []
        for issue in final_evaluation.issues:
            if issue.resolution_status in {"resolved", "mitigated"}:
                continue
            issue.resolution_status = (
                "unresolved_blocking"
                if issue.is_blocking
                else "unresolved_non_blocking"
            )
            unresolved.append(issue)
        blocking = [issue for issue in unresolved if issue.is_blocking]
        warnings = [issue for issue in unresolved if not issue.is_blocking]
        current.quality_gate_passed = hard_pass and not blocking
        current.unresolved_quality_issues = unresolved
        current.unresolved_blocking_issues = blocking
        current.unresolved_non_blocking_issues = warnings
        current.best_effort = bool(unresolved)
        for day in current.days:
            if not day.primary_plan:
                day.primary_plan = deepcopy(day.attractions)
        current.suggested_alternatives = sorted(
            set(
                [
                    *current.suggested_alternatives,
                    *[
                        day.weather_warning
                        for day in current.days
                        if day.weather_warning
                    ],
                    *[f"可选改进：{item.evidence}" for item in warnings],
                    *[
                        f"需处理后执行：{item.evidence}；建议动作 {item.repair_strategy.value}"
                        for item in blocking
                    ],
                ]
            )
        )
        if current.quality_gate_passed:
            if current.failure_reason and hard_pass:
                current.failure_reason = None
            current.degraded_reason = None
            current.risk_warnings = sorted(
                set([*current.risk_warnings, *[item.evidence for item in warnings]])
            )
        else:
            unresolved_text = "; ".join(
                f"{item.issue_type}"
                + (f"(Day {item.day})" if item.day else "")
                + f": {item.evidence}"
                for item in blocking
            )
            current.failure_reason = (
                "Quality gate unresolved after bounded repair: "
                + (unresolved_text or "hard constraint validation failed")
            )
            current.degraded_reason = current.failure_reason
            current.risk_warnings = sorted(
                set(
                    [
                        *current.risk_warnings,
                        "质量门未通过，当前结果为 degraded，不应视为完整可执行方案。",
                    ]
                )
            )
        curate_user_warnings(current)
        return current

    @staticmethod
    def _hard_constraints_pass(plan: TripPlan) -> bool:
        return plan.validation_result.valid and not any(
            not item.passed
            and item.severity.lower() in {"blocker", "hard", "critical"}
            for item in plan.constraint_report.items
        )

    @staticmethod
    def _next_quality_issue(
        evaluation: ExperienceEvaluation,
        attempted: set[str],
    ) -> ExperienceIssue | None:
        severity_rank = {"critical": 3, "high": 2, "warning": 1, "info": 0}
        priority = {
            "duplicate_visit": 0,
            "empty_day": 1,
            "missing_must_visit": 2,
            "attraction_closed": 3,
            "safety_risk": 4,
            "impossible_schedule": 5,
            "budget_violation": 6,
            "time_violation": 7,
            "constraint_failure": 8,
        }
        eligible = [
            issue
            for issue in evaluation.issues
            if issue.fingerprint not in attempted
            and issue.resolution_status not in {"resolved", "mitigated"}
            and issue.severity != "info"
        ]
        return min(
            eligible,
            key=lambda issue: (
                not issue.is_blocking,
                -severity_rank[issue.severity],
                priority.get(issue.issue_type, 50),
                issue.day or 0,
                issue.fingerprint,
            ),
            default=None,
        )

    @staticmethod
    def _quality_rank(evaluation: ExperienceEvaluation) -> tuple:
        blocking = sum(item.is_blocking for item in evaluation.issues)
        unresolved = sum(
            item.resolution_status not in {"resolved", "mitigated"}
            for item in evaluation.issues
        )
        return (-blocking, -unresolved, evaluation.overall_score)

    @staticmethod
    def _quality_plan_changes(
        before: TripPlan,
        after: TripPlan,
    ) -> List[PlanChange]:
        changes = []
        for before_day, after_day in zip(before.days, after.days):
            before_pois = [item.name for item in before_day.attractions]
            after_pois = [item.name for item in after_day.attractions]
            before_hotel = before_day.hotel.name if before_day.hotel else None
            after_hotel = after_day.hotel.name if after_day.hotel else None
            before_backup = [item.name for item in before_day.weather_backup]
            after_backup = [item.name for item in after_day.weather_backup]
            warning_changed = (
                before_day.weather_warning != after_day.weather_warning
            )
            if (
                before_pois != after_pois
                or before_hotel != after_hotel
                or before_backup != after_backup
                or warning_changed
            ):
                changes.append(
                    PlanChange(
                        day=before_day.day_index + 1,
                        before_pois=before_pois,
                        after_pois=after_pois,
                        before_hotel=before_hotel,
                        after_hotel=after_hotel,
                        before_weather_backup=before_backup,
                        after_weather_backup=after_backup,
                        details=(
                            after_day.weather_warning
                            if warning_changed
                            else ""
                        ),
                        changed_fields=[
                            field
                            for field, changed in {
                                "attractions": before_pois != after_pois,
                                "hotel": before_hotel != after_hotel,
                                "weather_backup": before_backup != after_backup,
                                "weather_warning": warning_changed,
                            }.items()
                            if changed
                        ],
                    )
                )
        return changes

    @staticmethod
    def _repair_modified_fields(
        before: TripPlan,
        after: TripPlan,
    ) -> List[str]:
        fields: List[str] = []
        if len(before.days) != len(after.days):
            fields.append("days")
        if before.overall_suggestions != after.overall_suggestions:
            fields.append("overall_suggestions")
        if before.weather_info != after.weather_info:
            fields.append("weather_info")
        for index, (old, new) in enumerate(zip(before.days, after.days)):
            prefix = f"days[{index}]"
            old_pois = [item.visit_key or item.name for item in old.attractions]
            new_pois = [item.visit_key or item.name for item in new.attractions]
            if old_pois != new_pois:
                fields.append(f"{prefix}.attractions")
            old_primary = [
                item.visit_key or item.name for item in old.primary_plan
            ]
            new_primary = [
                item.visit_key or item.name for item in new.primary_plan
            ]
            if old_primary != new_primary:
                fields.append(f"{prefix}.primary_plan")
            old_backup = [
                item.visit_key or item.name for item in old.weather_backup
            ]
            new_backup = [
                item.visit_key or item.name for item in new.weather_backup
            ]
            if old_backup != new_backup:
                fields.append(f"{prefix}.weather_backup")
            if old.weather_warning != new.weather_warning:
                fields.append(f"{prefix}.weather_warning")
            if (
                old.hotel.model_dump(mode="json") if old.hotel else None
            ) != (
                new.hotel.model_dump(mode="json") if new.hotel else None
            ):
                fields.append(f"{prefix}.hotel")
            if old.accommodation != new.accommodation:
                fields.append(f"{prefix}.accommodation")
            if old.schedule_blocks != new.schedule_blocks:
                fields.append(f"{prefix}.schedule_blocks")
        return fields

    @staticmethod
    def _scope_violations(
        changed_fields: Sequence[str],
        mutation_scope: Sequence[str],
    ) -> List[str]:
        def allowed(path: str) -> bool:
            for pattern in mutation_scope:
                if "[*]" in pattern:
                    start, end = pattern.split("[*]", 1)
                    if path.startswith(start + "[") and path.endswith(end):
                        return True
                if "[one]" in pattern:
                    start, end = pattern.split("[one]", 1)
                    if path.startswith(start + "[") and path.endswith(end):
                        return True
                if path == pattern:
                    return True
            return False

        violations = [path for path in changed_fields if not allowed(path)]
        for pattern in mutation_scope:
            if "[one]" not in pattern:
                continue
            start, end = pattern.split("[one]", 1)
            matches = [
                path
                for path in changed_fields
                if path.startswith(start + "[") and path.endswith(end)
            ]
            day_prefixes = {path.split("].", 1)[0] for path in matches}
            if len(day_prefixes) > 1:
                violations.extend(matches)
        return list(dict.fromkeys(violations))

    @staticmethod
    def _itinerary_snapshot(plan: TripPlan) -> List[dict]:
        return [
            {
                "day": day.day_index + 1,
                "pois": [item.name for item in day.attractions],
                "hotel": day.hotel.name if day.hotel else None,
                "travel_minutes": day.daily_travel_minutes,
                "walking_km": round(day.daily_walking_distance_km, 3),
                "utilization": round(day.day_utilization_score, 1),
                "weather_backup": [
                    item.name for item in day.weather_backup
                ],
                "weather_warning": day.weather_warning,
            }
            for day in plan.days
        ]

    @staticmethod
    def _hard_violation_keys(plan: TripPlan) -> set[str]:
        result = {
            (
                f"validation:{item.type}:{item.day}:{item.poi_name}:"
                f"{item.message}"
            )
            for item in plan.validation_result.violations
            if item.severity == "hard"
        }
        result.update(
            f"legacy:{item.name}:{item.message}"
            for item in plan.constraint_report.items
            if not item.passed
            and item.severity.lower() in {"blocker", "hard", "critical"}
        )
        return result

    @staticmethod
    def _issue_delta(
        before: ExperienceEvaluation,
        after: ExperienceEvaluation,
    ) -> dict:
        old = {item.fingerprint: item for item in before.issues}
        new = {item.fingerprint: item for item in after.issues}
        return {
            "added": [
                new[key].model_dump(mode="json")
                for key in sorted(new.keys() - old.keys())
            ],
            "removed": [
                old[key].model_dump(mode="json")
                for key in sorted(old.keys() - new.keys())
            ],
            "remaining": sorted(new.keys() & old.keys()),
        }

    @staticmethod
    def _target_issue_improved(
        trigger: ExperienceIssue,
        after: ExperienceEvaluation,
    ) -> bool:
        severity_rank = {"info": 0, "warning": 1, "high": 2, "critical": 3}
        matches = [
            item
            for item in after.issues
            if item.issue_type == trigger.issue_type
            and item.day == trigger.day
        ]
        if not matches:
            return True
        return all(
            item.resolution_status in {"mitigated", "resolved"}
            or severity_rank[item.severity] < severity_rank[trigger.severity]
            for item in matches
        )

    def ensure_candidate_coverage(
        self,
        request: TripRequest,
        specialist_candidates: List[Attraction],
    ) -> List[Attraction]:
        """Merge deterministic recall when LLM candidates are thin or biased.

        Candidate count alone is not coverage: six museums are still a poor
        pool for a neutral three-day Beijing trip. A classic baseline marker
        proves that the deterministic landmark recall has already run.
        """

        canonical_specialists = self._identity().deduplicate(
            specialist_candidates,
            must_visit=request.must_visit,
        )
        unique = {
            item.visit_key: deepcopy(item)
            for item in canonical_specialists
        }
        minimum = min(30, max(request.travel_days * 2, request.travel_days))
        profile = build_preference_profile(request)
        broad_classic_theme = preference_matches_categories(
            request.preferences, {"historic", "culture"}
        )
        has_classic_baseline = any(
            "classic_baseline" in item.recall_sources
            for item in unique.values()
        )
        needs_supplement = (
            len(unique) < minimum
            or (
                (profile.prefer_classic or broad_classic_theme)
                and not has_classic_baseline
            )
        )
        if not needs_supplement:
            return list(unique.values())

        recall_request = (
            request.model_copy(update={"prefer_classic": True})
            if broad_classic_theme and not profile.prefer_classic
            else request
        )
        for recalled in self.poi_collector.collect_attractions(recall_request):
            self._identity().assign_visit_keys([recalled])
            key = recalled.visit_key
            existing = unique.get(key)
            if existing is None:
                unique[key] = recalled
                continue
            existing.recall_sources = sorted(
                set([*existing.recall_sources, *recalled.recall_sources])
            )
        return sorted(
            unique.values(),
            key=lambda item: (
                -item.score,
                normalize_place_name(item.name),
                item.poi_id or "",
            ),
        )

    @staticmethod
    def _request_for_constraints(
        request: TripRequest, constraint_set: ConstraintSet
    ) -> TripRequest:
        updates = {}
        daily_load = constraint_set.first(ConstraintType.DAILY_LOAD)
        if daily_load and daily_load.value == "low":
            updates["pace"] = "relaxed"
        end_time = constraint_set.first(ConstraintType.DAILY_END_TIME)
        if end_time and not request.daily_end_time:
            updates["daily_end_time"] = str(end_time.value)
        walking_distance = constraint_set.first(
            ConstraintType.WALKING_DISTANCE
        )
        if (
            walking_distance
            and request.max_daily_walk_km is None
        ):
            updates["max_daily_walk_km"] = float(walking_distance.value)
        return request.model_copy(update=updates) if updates else request

    @staticmethod
    def _filter_by_planning_constraints(
        attractions: List[Attraction],
        request: TripRequest,
        constraint_set: ConstraintSet,
    ) -> List[Attraction]:
        avoid_high = any(
            item.value == "high_intensity"
            for item in constraint_set.get(
                ConstraintType.AVOID_ACTIVITY
            )
        )
        require_accessible = bool(
            constraint_set.get(ConstraintType.ACCESSIBLE_ROUTE)
        )
        result = []
        for attraction in attractions:
            required = any(
                place_names_match(name, attraction.name)
                for name in request.must_visit
            )
            if (
                avoid_high
                and attraction.intensity_level == "high"
                and not required
            ):
                continue
            if (
                require_accessible
                and attraction.accessible is not True
                and not required
            ):
                continue
            result.append(attraction)
        return result

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
        # Replanning must not mutate a plan held by a conversation/checkpoint.
        # It starts a new formal lineage version before optional repairs create
        # further versions through the sandbox gate.
        source_plan = replan_request.plan
        plan = source_plan.model_copy(deep=True)
        plan.plan_version = PlanVersionMetadata(
            version=source_plan.plan_version.version + 1,
            parent_version=source_plan.plan_version.version,
            mutation_source="replan",
            mutation_action="replan",
            mutation_reason=replan_request.notes or "user_replan",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
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
        plan = self.reviewer.review(plan, request)
        selected_candidates = [
            item for day in plan.days for item in day.attractions
        ]
        plan = self.run_quality_loop(
            plan,
            request,
            selected_candidates,
            hotel_candidates=[
                day.hotel for day in plan.days if day.hotel is not None
            ],
        )
        if plan.observability_trace is None:
            selected = [
                item
                for day in plan.days
                for item in day.attractions
            ]
            plan.observability_trace = build_planning_trace(
                request,
                plan,
                candidates=selected,
                eligible_candidates=selected,
                run_type="replan",
            )
        refresh_planning_trace(plan, run_type="replan")
        emit_planning_trace(plan.observability_trace)
        return plan

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
        removed_duplicates = self._identity().deduplicate_plan(
            plan,
            must_visit=request.must_visit,
        )
        if removed_duplicates:
            plan.risk_warnings = sorted(
                set(
                    [
                        *plan.risk_warnings,
                        "已合并重复游览实体："
                        + "、".join(sorted(set(removed_duplicates))),
                    ]
                )
            )
        for day in plan.days:
            self._annotate_available_window(day, len(plan.days), request)
            self._recalculate_day(day, request)
        plan.route_segments = [segment for day in plan.days for segment in day.route_segments]
        plan.budget = self.budget_estimator.estimate(plan.days, request)
        legacy_report = self.constraint_checker.check(plan, request)
        constraint_set = (
            ConstraintSet(constraints=plan.normalized_constraints)
            if plan.normalized_constraints
            else self.constraint_extractor.extract(request)
            if hasattr(self, "constraint_extractor")
            else ConstraintExtractor().extract(request)
        )
        validator = (
            self.constraint_validator
            if hasattr(self, "constraint_validator")
            else ConstraintValidationEngine()
        )
        validation_result = validator.validate(plan, constraint_set)
        plan.normalized_constraints = constraint_set.constraints
        plan.validation_result = validation_result
        generic_items = [
            ConstraintItem(
                name=f"Constraint:{violation.type}",
                passed=False,
                actual=str(violation.actual),
                expected=str(violation.expected),
                severity=(
                    "blocker"
                    if violation.severity == "hard"
                    else "warning"
                ),
                message=violation.message,
            )
            for violation in validation_result.violations
        ]
        plan.constraint_report = ConstraintReport(
            passed=legacy_report.passed and validation_result.valid,
            score=round(
                min(legacy_report.score, validation_result.score), 3
            ),
            items=[*legacy_report.items, *generic_items],
        )

    @staticmethod
    def _annotate_available_window(
        day: DayPlan,
        day_count: int,
        request: TripRequest,
    ) -> None:
        start = _clock_minutes(request.daily_start_time, 9 * 60)
        end = _clock_minutes(request.daily_end_time, 20 * 60)
        reason = None
        if day.day_index == 0 and request.arrival_time:
            start = max(start, _clock_minutes(request.arrival_time, start))
            reason = "arrival"
        if day.day_index == day_count - 1 and request.departure_time:
            end = min(end, _clock_minutes(request.departure_time, end))
            reason = "departure"
        day.available_minutes = max(0, end - start)
        day.partial_day_reason = reason

    def _recalculate_day(self, day: DayPlan, request: TripRequest) -> None:
        day.route_segments = self.route_evaluator.build_day_routes(day, request.city)
        self._schedule_day_opening_hours(day, request)
        self._update_day_metrics(
            day, request.pace, daily_time_budget_minutes(request)
        )

    def _apply_next_repair(
        self,
        plan: TripPlan,
        request: TripRequest,
        available_attractions: List[Attraction],
    ) -> Optional[tuple[str, str]]:
        hard_violations = [
            item
            for item in plan.validation_result.violations
            if item.severity == "hard"
        ]
        activity_violation = next(
            (
                item
                for item in hard_violations
                if item.type in {"ACTIVITY_LEVEL", "ACCESSIBILITY"}
                and item.poi_name
            ),
            None,
        )
        if activity_violation is not None:
            for day in plan.days:
                candidate = next(
                    (
                        item
                        for item in day.attractions
                        if item.name == activity_violation.poi_name
                        and not any(
                            place_names_match(required, item.name)
                            for required in request.must_visit
                        )
                    ),
                    None,
                )
                if candidate is not None:
                    day.attractions.remove(candidate)
                    return (
                        "remove_constraint_violating_activity",
                        activity_violation.message,
                    )

        rest_violation = next(
            (
                item
                for item in hard_violations
                if item.type == "REST_WINDOW" and item.day
            ),
            None,
        )
        if rest_violation is not None:
            rest_constraint = next(
                (
                    item
                    for item in plan.normalized_constraints
                    if item.type == ConstraintType.TIME_WINDOW
                    and item.reason == "rest"
                ),
                None,
            )
            if rest_constraint:
                day = plan.days[rest_violation.day - 1]
                day.schedule_blocks = self._schedule_blocks_for_constraints(
                    ConstraintSet(constraints=[rest_constraint])
                )
                return (
                    "insert_required_rest_window",
                    rest_violation.message,
                )

        load_or_walk_violation = next(
            (
                item
                for item in hard_violations
                if item.type
                in {
                    "DAILY_LOAD",
                    "WALKING_LIMIT",
                    "WALKING_DURATION",
                }
                and item.day
            ),
            None,
        )
        if load_or_walk_violation is not None:
            day = plan.days[load_or_walk_violation.day - 1]
            optional = [
                item
                for item in day.attractions
                if not any(
                    place_names_match(required, item.name)
                    for required in request.must_visit
                )
            ]
            if optional:
                removed = min(
                    optional,
                    key=lambda item: self._removal_priority(
                        day, item, request
                    ),
                )
                day.attractions.remove(removed)
                return (
                    "reduce_load_from_violation",
                    (
                        f"{load_or_walk_violation.message}; "
                        f"removed {removed.name}"
                    ),
                )

        transport_violation = next(
            (
                item
                for item in hard_violations
                if item.type == "TRANSPORT_MODE" and item.day
            ),
            None,
        )
        if transport_violation is not None:
            day = plan.days[transport_violation.day - 1]
            day.transportation = request.transportation
            return (
                "restore_required_transportation",
                transport_violation.message,
            )

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
                if self._try_add_attraction(
                    target_day, attraction, request, available_attractions
                ):
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

        area_jump_days = [
            day
            for day in plan.days
            if len({item.area for item in day.attractions if item.area}) > 2
        ]
        if area_jump_days:
            donor_day = max(
                area_jump_days,
                key=lambda day: len(
                    {item.area for item in day.attractions if item.area}
                ),
            )
            optional = sorted(
                (
                    item
                    for item in donor_day.attractions
                    if not any(
                        place_names_match(name, item.name)
                        for name in request.must_visit
                    )
                ),
                key=lambda item: self._removal_priority(
                    donor_day, item, request
                ),
            )
            for attraction in optional:
                compatible_days = [
                    day
                    for day in plan.days
                    if day is not donor_day
                    and attraction.area
                    and attraction.area
                    in {item.area for item in day.attractions}
                ]
                for target_day in sorted(
                    compatible_days, key=lambda day: day.day_index
                ):
                    if self._try_move_attraction(
                        donor_day, target_day, attraction, request
                    ):
                        return (
                            "move_area_outlier",
                            f"Moved {attraction.name} to Day{target_day.day_index + 1} "
                            f"to keep Day{donor_day.day_index + 1} geographically coherent",
                        )
            if optional:
                attraction = optional[0]
                donor_day.attractions.remove(attraction)
                return (
                    "remove_area_outlier",
                    f"Removed low-priority area outlier {attraction.name}",
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
                    key=lambda item: (
                        *self._removal_priority(item[0], item[1], request),
                        -item[1].ticket_price,
                    ),
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
                key=lambda attraction: self._removal_priority(
                    donor_day, attraction, request
                ),
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

    def _removal_priority(
        self, day: DayPlan, attraction: Attraction, request: TripRequest
    ) -> tuple:
        """Low-value, remote, time-expensive optional POIs are removed first."""

        anchors = [
            item.location
            for item in day.attractions
            if item is not attraction
        ]
        if day.hotel and day.hotel.location:
            anchors.append(day.hotel.location)
        distance = min(
            (
                haversine_meters(attraction.location, anchor)
                for anchor in anchors
            ),
            default=0,
        )
        profile = build_preference_profile(request)
        preference_niche = (
            attraction.selection_role == "niche_attraction"
            and attraction.score_breakdown.get("preference_match", 0) > 0
        )
        if profile.deep_exploration:
            portfolio_protection = 2 if preference_niche else 0
        else:
            portfolio_protection = (
                2
                if attraction.selection_role == "core_landmark"
                else 1
                if attraction.selection_role == "major_attraction"
                else 0
            )
        return (
            portfolio_protection,
            attraction.popularity,
            attraction.first_visit_priority,
            attraction.score,
            -distance,
            -attraction.visit_duration,
            attraction.name,
        )

    @staticmethod
    def _has_underfilled_day(plan: TripPlan) -> bool:
        return any(
            day.attractions
            and (
                day.day_utilization_score < 70
                or len(day.attractions) < 2
            )
            and not (
                len(day.attractions) == 1
                and day.daily_travel_minutes
                > SpatialItineraryPlanner.remote_travel_threshold_minutes
            )
            for day in plan.days
        )

    def _repair_low_soft_score(
        self,
        plan: TripPlan,
        request: TripRequest,
        available_attractions: List[Attraction],
    ) -> None:
        """One bounded deterministic replan when soft quality is below 70."""

        self._identity().assign_visit_keys(available_attractions)
        planned_keys = self._identity().assigned_visit_keys(plan)
        for day in sorted(plan.days, key=lambda item: item.day_index):
            if (
                (
                    day.day_utilization_score >= 70
                    and len(day.attractions) >= 2
                )
                or len(day.attractions)
                >= SpatialItineraryPlanner.max_attractions_per_day
                or (
                    len(day.attractions) == 1
                    and day.daily_travel_minutes
                    > SpatialItineraryPlanner.remote_travel_threshold_minutes
                )
            ):
                continue
            day_areas = {item.area for item in day.attractions if item.area}
            candidates = [
                item
                for item in available_attractions
                if item.visit_key not in planned_keys
                and (
                    not day_areas
                    or item.area in day_areas
                    or len(day_areas) < 2
                )
            ]
            candidates.sort(
                key=lambda item: (
                    0 if item.area in day_areas else 1,
                    self.spatial_planner._next_poi_rank(
                        day.attractions,
                        item,
                        day.hotel.location
                        if day.hotel and day.hotel.location
                        else None,
                        day.transportation,
                        daily_time_budget_minutes(request),
                    ),
                )
            )
            for candidate in candidates:
                if self._try_add_attraction(
                    day, candidate, request, available_attractions
                ):
                    planned_keys.add(candidate.visit_key)
                    self._recalculate_day(day, request)
                    if day.day_utilization_score >= 70:
                        break

        for day in plan.days:
            day.attractions = self.spatial_planner._beam_search_order(
                day.attractions,
                request.must_visit,
                day.hotel.location if day.hotel else None,
                day.transportation,
                daily_time_budget_minutes(request),
            )
        worst_days = sorted(
            plan.days,
            key=lambda day: (
                self.reviewer._day_ratio(day, request),
                day.daily_distance_km,
            ),
            reverse=True,
        )
        for day in worst_days:
            optional = [
                item
                for item in day.attractions
                if not any(
                    place_names_match(name, item.name)
                    for name in request.must_visit
                )
            ]
            if len(day.attractions) > 1 and optional and (
                self.reviewer._day_ratio(day, request) > 1
                or day.daily_distance_km > 35
            ):
                day.attractions.remove(
                    min(
                        optional,
                        key=lambda item: self._removal_priority(
                            day, item, request
                        ),
                    )
                )
                break

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
        candidate_pool: Sequence[Attraction] = (),
    ) -> bool:
        decision = self._candidate_policy().evaluate(
            attraction,
            pool=candidate_pool or [attraction],
            selected=target_day.attractions,
            request=request,
            entry_point="planner_repair_or_fill",
        )
        if not decision.accepted:
            return False
        candidate = deepcopy(target_day)
        candidate.attractions.append(deepcopy(attraction))
        candidate.attractions = self.spatial_planner._nearest_neighbor_order(
            candidate.attractions,
            request.must_visit,
            candidate.hotel.location if candidate.hotel else None,
        )
        self._recalculate_day(candidate, request)
        if not self._day_is_feasible(candidate, request):
            attraction.selection_trace.append(
                {
                    "entry_point": "planner_repair_or_fill",
                    "accepted": False,
                    "reason": "route_or_time_constraint",
                    "route_increment_minutes": (
                        candidate.daily_travel_minutes
                        - target_day.daily_travel_minutes
                    ),
                }
            )
            return False
        if (
            request.max_daily_walk_km is not None
            and candidate.daily_walking_distance_km > request.max_daily_walk_km
        ):
            attraction.selection_trace.append(
                {
                    "entry_point": "planner_repair_or_fill",
                    "accepted": False,
                    "reason": "walking_constraint",
                    "route_increment_minutes": (
                        candidate.daily_travel_minutes
                        - target_day.daily_travel_minutes
                    ),
                }
            )
            return False
        daily_budget = (
            request.budget_limit / max(1, request.travel_days)
            if request.budget_limit is not None
            else None
        )
        if daily_budget is not None and (
            target_day.daily_cost + attraction.ticket_price > daily_budget
        ):
            attraction.selection_trace.append(
                {
                    "entry_point": "planner_repair_or_fill",
                    "accepted": False,
                    "reason": "daily_budget_constraint",
                }
            )
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

    def _assign_days(
        self,
        request: TripRequest,
        attractions: List[Attraction],
        hotel: Hotel,
        constraint_set: ConstraintSet | None = None,
        planning_reference: Location | None = None,
    ) -> List[DayPlan]:
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
        if not hasattr(self, "spatial_planner"):
            self.spatial_planner = SpatialItineraryPlanner()
        daily_groups = self.spatial_planner.plan(
            attractions=attractions,
            travel_days=request.travel_days,
            pace=request.pace,
            must_visit=request.must_visit,
            hotel_location=planning_reference or hotel.location,
            transportation=request.transportation,
            daily_time_budget_minutes=daily_time_budget_minutes(request),
        )
        logger.info(
            "========== 空间分配中间结果（constraint/repair/finalize 前） ==========%s",
            [
                [f"{item.name}({item.score:.1f})" for item in group]
                for group in daily_groups
            ],
        )
        days: List[DayPlan] = []
        constraint_set = constraint_set or ConstraintExtractor().extract(
            request
        )
        walking_duration = constraint_set.first(
            ConstraintType.WALKING_DURATION
        )

        for day_index in range(request.travel_days):
            current_date = start_date + timedelta(days=day_index)
            day_attractions = daily_groups[day_index]
            areas = "、".join(
                sorted({item.area for item in day_attractions if item.area})
            )
            days.append(
                DayPlan(
                    date=current_date.strftime("%Y-%m-%d"),
                    day_index=day_index,
                    description=(
                        f"第{day_index + 1}天集中游览{areas or request.city + '核心区域'}，"
                        f"节奏为{request.pace}。"
                    ),
                    transportation=request.transportation,
                    accommodation=request.accommodation,
                    hotel=hotel,
                    attractions=day_attractions,
                    meals=self._build_meals(request, day_index),
                    schedule_blocks=self._schedule_blocks_for_constraints(
                        constraint_set
                    ),
                    max_walking_leg_minutes=(
                        int(walking_duration.value)
                        if walking_duration is not None
                        else None
                    ),
                )
            )
        return days

    @staticmethod
    def _schedule_blocks_for_constraints(
        constraint_set: ConstraintSet,
    ) -> List[ScheduleBlock]:
        blocks = []
        for constraint in constraint_set.get(ConstraintType.TIME_WINDOW):
            duration = int(constraint.value or 0)
            start = _clock_minutes(constraint.start, 12 * 60)
            window_end = _clock_minutes(constraint.end, start + duration)
            end = min(window_end, start + duration)
            blocks.append(
                ScheduleBlock(
                    type=(
                        "rest"
                        if constraint.reason == "rest"
                        else "buffer"
                    ),
                    start_time=_format_clock(start),
                    end_time=_format_clock(end),
                    reason=constraint.reason,
                )
            )
        return blocks

    def _schedule_day_opening_hours(self, day: DayPlan, request: TripRequest) -> None:
        """Calculate visit windows after routes are known and mark hard violations."""

        current = _clock_minutes(request.daily_start_time, 9 * 60)
        start_minutes = current
        day.planned_start_time = _format_clock(current)
        incoming_by_destination = {segment.destination: segment for segment in day.route_segments}
        pending_blocks = sorted(
            day.schedule_blocks,
            key=lambda block: _clock_minutes(block.start_time, 24 * 60),
        )
        for attraction in day.attractions:
            incoming = incoming_by_destination.get(attraction.name)
            arrival = current + (incoming.duration_minutes if incoming else 0)
            if incoming is not None:
                incoming.planned_departure_time = _format_clock(current)
                incoming.planned_arrival_time = _format_clock(arrival)
            opening = _clock_minutes(attraction.opening_time, 0)
            start = max(arrival, opening) if attraction.opening_time else arrival
            departure = start + attraction.visit_duration
            for block in list(pending_blocks):
                block_start = _clock_minutes(block.start_time, 0)
                block_end = _clock_minutes(block.end_time, block_start)
                block_duration = max(0, block_end - block_start)
                if start <= block_start < departure:
                    # Long scenic visits may contain a seated lunch/rest stop.
                    departure += block_duration
                    pending_blocks.remove(block)
                    break
                if block_start <= start < block_end:
                    start = block_end
                    departure = start + attraction.visit_duration
                    pending_blocks.remove(block)
                    break
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
        for block in pending_blocks:
            block_start = _clock_minutes(block.start_time, 0)
            block_end = _clock_minutes(block.end_time, block_start)
            if current <= block_end:
                current = max(current, block_end)
        # Meals are placed flexibly between visits, but still consume real
        # availability and therefore must move the computed return time.
        if (
            day.attractions
            and day.meals
            and not any(
                block.reason == "rest"
                for block in day.schedule_blocks
            )
        ):
            current += min(60, get_pace_profile(request.pace).daily_buffer_minutes)
        if day.route_segments and day.attractions:
            return_segment = day.route_segments[-1]
            if return_segment.origin == day.attractions[-1].name:
                return_segment.planned_departure_time = _format_clock(current)
                current += return_segment.duration_minutes
                return_segment.planned_arrival_time = _format_clock(current)
        day.planned_end_time = _format_clock(current)
        day.daily_elapsed_minutes = current - start_minutes

    def _update_day_metrics(
        self,
        day: DayPlan,
        pace: str,
        available_time_minutes: Optional[int] = None,
    ) -> None:
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
        # Meal time is explicitly exposed and is already included in the
        # profile buffer, preserving the single-counted duration invariant.
        day.daily_meal_minutes = (
            min(60, day.daily_buffer_minutes) if day.meals else 0
        )
        day.daily_duration_minutes = (
            day.daily_visit_minutes
            + day.daily_travel_minutes
            + day.daily_buffer_minutes
        )
        utilized_minutes = (
            day.daily_elapsed_minutes
            if day.daily_elapsed_minutes > 0
            else day.daily_duration_minutes
        )
        day.day_utilization_score = round(
            utilized_minutes
            / max(
                1,
                available_time_minutes
                or profile.daily_time_budget_minutes,
            )
            * 100,
            1,
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

    def build_rag_metadata(self, request: TripRequest) -> dict[str, list[str]]:
        """Build only deterministic filters; uncertain free text stays query text."""
        metadata: dict[str, list[str]] = {}
        categories: set[str] = set()
        for preference in [*request.preferences, *request.soft_preferences]:
            for label, mapped in PREFERENCE_CATEGORY_MAP.items():
                if label.lower() in preference.lower():
                    categories.update(mapped)
        if categories:
            metadata["categories"] = sorted(categories)
        poi_keys: list[str] = []
        for name in request.must_visit:
            item = self.travel_knowledge.repository.resolve_poi(request.city, name=name)
            if item is not None:
                poi_keys.append(item.poi_key)
        if poi_keys:
            metadata["poi_key"] = sorted(set(poi_keys))
        scenarios = self.travel_knowledge.scenario_tags(request)
        if scenarios:
            metadata["scenario_tags"] = scenarios
        return metadata


_multi_agent_planner: Optional[MultiAgentTripPlanner] = None


def get_trip_planner_agent() -> MultiAgentTripPlanner:
    global _multi_agent_planner
    if _multi_agent_planner is None:
        _multi_agent_planner = MultiAgentTripPlanner()
    return _multi_agent_planner
