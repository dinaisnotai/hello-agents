"""Deterministic, lightweight spatial planning for daily itineraries."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ..models.schemas import Attraction, Location
from .attraction_scorer import AttractionScorer
from .candidate_acceptance_policy import CandidateAcceptancePolicy
from .place_name_service import normalize_place_name, place_names_match


@dataclass(frozen=True)
class PaceProfile:
    attractions_per_day: int
    visit_duration_minutes: int
    daily_time_budget_minutes: int
    daily_buffer_minutes: int


PACE_PROFILES = {
    "relaxed": PaceProfile(2, 150, 420, 60),
    "balanced": PaceProfile(3, 120, 540, 60),
    "packed": PaceProfile(4, 90, 660, 60),
}
DEFAULT_PACE_PROFILE = PACE_PROFILES["balanced"]


def get_pace_profile(pace: str) -> PaceProfile:
    return PACE_PROFILES.get(pace, DEFAULT_PACE_PROFILE)


def closing_time_priority(attraction: Attraction) -> int:
    """Earlier published closing times should be visited earlier in the day."""

    if not attraction.closing_time:
        return 24 * 60
    try:
        hour, minute = attraction.closing_time.split(":", 1)
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return 24 * 60


def haversine_meters(left: Location, right: Location) -> float:
    """Return straight-line distance between two coordinates in meters."""

    earth_radius_meters = 6_371_000
    left_latitude = math.radians(left.latitude)
    right_latitude = math.radians(right.latitude)
    latitude_delta = math.radians(right.latitude - left.latitude)
    longitude_delta = math.radians(right.longitude - left.longitude)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(left_latitude)
        * math.cos(right_latitude)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * earth_radius_meters * math.asin(min(1.0, math.sqrt(haversine)))


@dataclass(frozen=True)
class DayTiming:
    visit_minutes: int
    travel_minutes: int
    buffer_minutes: int

    @property
    def total_minutes(self) -> int:
        return self.visit_minutes + self.travel_minutes + self.buffer_minutes


def estimate_visit_duration(name: str, category: str, pace: str) -> int:
    """Estimate POI dwell time with transparent keyword rules."""

    text = f"{name} {category or ''}"
    duration = 120
    duration_rules = (
        (("长城", "八达岭", "慕田峪", "大型景区", "主题乐园", "动物园"), 240),
        (("博物馆", "美术馆", "科技馆", "湿地", "风景区", "山"), 180),
        (("古镇", "园林", "公园", "寺", "宫", "遗址", "湖"), 120),
        (("街", "巷", "广场", "观景台"), 90),
    )
    for keywords, rule_duration in duration_rules:
        if any(keyword in text for keyword in keywords):
            duration = rule_duration
            break

    pace_adjustment = {"relaxed": 30, "balanced": 0, "packed": -30}.get(pace, 0)
    return max(60, duration + pace_adjustment)


def estimate_travel_minutes(
    origin: Location, destination: Location, transportation: str
) -> int:
    """Conservatively estimate travel time without making route API calls."""

    distance = haversine_meters(origin, destination)
    if distance < 50:
        return 0

    if transportation == "步行":
        speed_meters_per_minute, transfer_minutes = 69.0, 0
    elif any(label in transportation for label in ("公共", "公交", "地铁")):
        speed_meters_per_minute, transfer_minutes = 333.0, 15
    else:
        speed_meters_per_minute, transfer_minutes = 500.0, 10
    return math.ceil(distance / speed_meters_per_minute) + transfer_minutes


@dataclass(frozen=True)
class LegEstimate:
    mode: str
    travel_minutes: int
    walking_meters: float
    transfers: int


def estimate_leg(
    origin: Location, destination: Location, transportation: str
) -> LegEstimate:
    """Choose a sensible mode for one leg instead of copying a day-wide mode."""

    distance = haversine_meters(origin, destination)
    walking_minutes = math.ceil(distance / 69.0)
    if distance < 1_500 or transportation == "步行":
        return LegEstimate("walking", walking_minutes, distance, 0)

    if any(label in transportation for label in ("公共", "公交", "地铁", "transit")):
        # Long regional legs (for example central Beijing to Badaling) use
        # express rail/coach speeds rather than inner-city bus speed.
        transit_speed = 800.0 if distance > 30_000 else 333.0
        transit_minutes = math.ceil(distance / transit_speed) + 15
        if distance < 5_000 and walking_minutes <= transit_minutes:
            return LegEstimate("walking", walking_minutes, distance, 0)
        return LegEstimate(
            "transit",
            transit_minutes,
            min(800.0, distance * 0.15),
            1,
        )

    driving_minutes = math.ceil(distance / 500.0) + 10
    if distance < 5_000 and walking_minutes <= driving_minutes:
        return LegEstimate("walking", walking_minutes, distance, 0)
    return LegEstimate("driving", driving_minutes, 0, 0)


class SpatialItineraryPlanner:
    """Select high-priority POIs, cluster them by day, then order each day."""

    max_attractions_per_day = 5
    min_utilization_ratio = 0.70
    target_utilization_ratio = 0.90
    remote_travel_threshold_minutes = 120
    remote_nearby_radius_meters = 20_000
    attraction_scorer = AttractionScorer()

    def plan(
        self,
        attractions: Sequence[Attraction],
        travel_days: int,
        pace: str,
        must_visit: Sequence[str],
        hotel_location: Optional[Location] = None,
        transportation: str = "公共交通",
        daily_time_budget_minutes: Optional[int] = None,
    ) -> List[List[Attraction]]:
        if travel_days <= 0:
            return []

        selected = self.select_attractions(
            attractions,
            travel_days,
            pace,
            must_visit,
            # Candidate selection follows the requested pace. The hard
            # maximum is enforced later while filling each day; using it here
            # retained 15 candidates for a balanced 3-day trip and allowed
            # low-value POIs to be selected merely to fill an area.
            per_day_limit=get_pace_profile(pace).attractions_per_day,
        )
        for attraction in selected:
            if attraction.suggested_duration_minutes is not None:
                pace_adjustment = {
                    "relaxed": 30,
                    "balanced": 0,
                    "packed": -30,
                }.get(pace, 0)
                attraction.visit_duration = max(
                    60,
                    attraction.suggested_duration_minutes + pace_adjustment,
                )
            else:
                attraction.visit_duration = estimate_visit_duration(
                    attraction.name, attraction.category or "", pace
                )

        groups = self._cluster_by_day(
            selected,
            travel_days,
            must_visit,
            pace,
            hotel_location,
            transportation,
            daily_time_budget_minutes or get_pace_profile(pace).daily_time_budget_minutes,
        )
        groups = self._rebalance_groups(
            groups,
            must_visit,
            hotel_location,
            transportation,
            get_pace_profile(pace),
            daily_time_budget_minutes or get_pace_profile(pace).daily_time_budget_minutes,
        )
        ordered_groups = [
            self._beam_search_order(
                group,
                must_visit,
                hotel_location,
                transportation,
                daily_time_budget_minutes
                or get_pace_profile(pace).daily_time_budget_minutes,
            )
            for group in groups
        ]
        return ordered_groups + [[] for _ in range(travel_days - len(ordered_groups))]

    def select_attractions(
        self,
        attractions: Sequence[Attraction],
        travel_days: int,
        pace: str,
        must_visit: Sequence[str],
        per_day_limit: Optional[int] = None,
    ) -> List[Attraction]:
        """Keep must-visits, then fill the pace capacity with top-scoring POIs."""

        unique = {}
        for attraction in attractions:
            key = attraction.visit_key or normalize_place_name(attraction.name)
            existing = unique.get(key)
            if existing is None or self._priority_key(
                attraction, must_visit
            ) < self._priority_key(existing, must_visit):
                unique[key] = attraction

        ranked = sorted(unique.values(), key=lambda item: self._priority_key(item, must_visit))
        must_visit_count = sum(self._is_must_visit(item, must_visit) for item in ranked)
        pace_capacity = travel_days * (
            per_day_limit or get_pace_profile(pace).attractions_per_day
        )
        selection_limit = max(pace_capacity, must_visit_count)
        required = [
            item for item in ranked if self._is_must_visit(item, must_visit)
        ]
        optional = [
            item for item in ranked if not self._is_must_visit(item, must_visit)
        ]
        selected = list(required)
        policy = CandidateAcceptancePolicy()
        for item in selected:
            policy.evaluate(
                item,
                pool=ranked,
                selected=[],
                request=None,
                entry_point="initial_must_visit_selection",
            )
        deep_exploration = any(
            item.score_breakdown.get("deep_exploration", 0) > 0
            for item in ranked
        )
        portfolio_enabled = any(
            "base_quality" in item.score_breakdown for item in ranked
        )
        core_candidates = [
            item
            for item in optional
            if self._portfolio_role(item) == "core_landmark"
        ]
        core_quota = (
            0
            if deep_exploration
            else min(travel_days, len(core_candidates))
        )
        selected.extend(core_candidates[:core_quota])
        for item in core_candidates[:core_quota]:
            policy.evaluate(
                item, pool=ranked, selected=selected, request=None,
                entry_point="initial_core_quota",
            )
        optional = [
            item for item in optional if item not in core_candidates[:core_quota]
        ]
        remaining_capacity = max(0, selection_limit - len(selected))
        major_candidates = [
            item
            for item in optional
            if self._portfolio_role(item) == "major_attraction"
        ]
        major_quota = (
            0
            if deep_exploration
            else min(travel_days, remaining_capacity, len(major_candidates))
        )
        selected.extend(major_candidates[:major_quota])
        for item in major_candidates[:major_quota]:
            policy.evaluate(
                item, pool=ranked, selected=selected, request=None,
                entry_point="initial_major_quota",
            )
        optional = [
            item
            for item in optional
            if item not in major_candidates[:major_quota]
        ]
        if deep_exploration:
            deep_niche_candidates = [
                item
                for item in optional
                if self._portfolio_role(item) == "niche_attraction"
                and self._has_explicit_preference_match(item)
            ]
            deep_niche_candidates.sort(
                key=lambda item: self._priority_key(item, must_visit)
            )
            reserve = min(
                max(2, travel_days),
                selection_limit - len(selected),
                len(deep_niche_candidates),
            )
            selected.extend(deep_niche_candidates[:reserve])
            for item in deep_niche_candidates[:reserve]:
                policy.evaluate(
                    item, pool=ranked, selected=selected, request=None,
                    entry_point="initial_deep_niche_reserve",
                )
            optional = [
                item
                for item in optional
                if item not in deep_niche_candidates[:reserve]
            ]
        # A low-score POI must not become a destination simply because it is
        # in a separate area. Keep it only as a last-resort coverage fallback.
        quality_optional = [
            item
            for item in optional
            if item.score >= 60 or item.first_visit_priority >= 7
        ]
        low_quality_optional = [
            item for item in optional if item not in quality_optional
        ]
        minimum_pool = min(selection_limit, max(travel_days, travel_days * 2))
        optional = (
            quality_optional
            if len(required) + len(quality_optional) >= minimum_pool
            else [*quality_optional, *low_quality_optional]
        )
        bucket_counts: Dict[str, int] = {}
        for item in selected:
            bucket = self._diversity_bucket(item)
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

        # With no explicit matching preference, specialist cultural venues
        # may supplement a trip but cannot consume most of the planning pool.
        niche_cap = (
            max(1, math.ceil(selection_limit * 0.7))
            if deep_exploration
            else max(1, math.floor(selection_limit * 0.25))
        )
        deferred: List[Attraction] = []
        while optional and len(selected) < selection_limit:
            ranked_optional = sorted(
                optional,
                key=lambda item: (
                    self._priority_key(item, must_visit)[1]
                    + bucket_counts.get(self._diversity_bucket(item), 0) * 18,
                    *self._priority_key(item, must_visit)[2:],
                ),
            )
            chosen = None
            for item in ranked_optional:
                bucket = self._diversity_bucket(item)
                if (
                    deep_exploration
                    and self._portfolio_role(item) == "core_landmark"
                    and sum(
                        self._portfolio_role(existing) == "core_landmark"
                        for existing in selected
                    ) >= max(1, travel_days // 2)
                ):
                    deferred.append(item)
                    optional.remove(item)
                    continue
                if (
                    portfolio_enabled
                    and not deep_exploration
                    and bucket_counts.get(bucket, 0) >= 2
                ):
                    deferred.append(item)
                    optional.remove(item)
                    continue
                if (
                    portfolio_enabled
                    and not deep_exploration
                    and len(selected) >= minimum_pool
                    and item.score < 40
                ):
                    deferred.append(item)
                    optional.remove(item)
                    continue
                if (
                    self._portfolio_role(item) == "niche_attraction"
                    and sum(
                        self._portfolio_role(existing) == "niche_attraction"
                        for existing in selected
                    ) >= niche_cap
                    # Strong, explicit specialist requests can still use the
                    # legacy focused-candidate path. Ordinary preference
                    # boosts are 10, while this path requires 20+.
                    and item.score_breakdown.get("preference_match", 0) < 20
                ):
                    deferred.append(item)
                    optional.remove(item)
                    continue
                decision = policy.evaluate(
                    item,
                    pool=ranked,
                    selected=selected,
                    request=None,
                    entry_point="initial_portfolio_fill",
                )
                if not decision.accepted:
                    deferred.append(item)
                    optional.remove(item)
                    continue
                chosen = item
                break
            if chosen is None:
                break
            selected.append(chosen)
            chosen.score_breakdown["saturation_penalty"] = float(
                bucket_counts.get(self._diversity_bucket(chosen), 0) * 18
            )
            optional.remove(chosen)
            bucket = self._diversity_bucket(chosen)
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

        # Candidate starvation is worse than imperfect diversity. If the map
        # provider returned almost nothing else, retain enough deferred POIs
        # to make each day executable, without filling all capacity with them.
        for item in deferred:
            if len(selected) >= minimum_pool:
                break
            if (
                not deep_exploration
                and self._portfolio_role(item) == "niche_attraction"
                and sum(
                    self._portfolio_role(existing) == "niche_attraction"
                    for existing in selected
                ) >= niche_cap
                and item.score_breakdown.get("preference_match", 0) < 20
            ):
                continue
            if not policy.evaluate(
                item,
                pool=ranked,
                selected=selected,
                request=None,
                entry_point="initial_deferred_fill",
            ).accepted:
                continue
            selected.append(item)
        selected_keys = {
            item.visit_key or normalize_place_name(item.name)
            for item in selected
        }
        for item in ranked:
            role = self._portfolio_role(item)
            item.selection_role = role
            key = item.visit_key or normalize_place_name(item.name)
            if key in selected_keys:
                item.selection_reason = (
                    "must_visit"
                    if self._is_must_visit(item, must_visit)
                    else "core_landmark_quota"
                    if role == "core_landmark" and not deep_exploration
                    else "major_attraction_quota"
                    if role == "major_attraction" and not deep_exploration
                    else "preference_calibrated_portfolio_fill"
                )
            elif role in {"core_landmark", "major_attraction"}:
                item.selection_reason = (
                    "not_selected_after_feasibility_capacity_and_route_tradeoff"
                )
            elif role == "niche_attraction":
                item.selection_reason = (
                    "not_selected_due_to_niche_quota_or_saturation"
                )
            else:
                item.selection_reason = "not_selected_by_portfolio_capacity"
        return sorted(
            selected,
            key=lambda item: self._priority_key(item, must_visit),
        )

    @classmethod
    def _portfolio_role(cls, attraction: Attraction) -> str:
        if attraction.is_core_landmark:
            return "core_landmark"
        if attraction.selection_role != "complementary_attraction":
            return attraction.selection_role
        if (
            attraction.first_visit_priority >= 7
            and attraction.popularity >= 7
        ):
            return "major_attraction"
        if (
            cls._is_niche_cultural(attraction)
        ):
            return "niche_attraction"
        return "complementary_attraction"

    @staticmethod
    def _has_explicit_preference_match(attraction: Attraction) -> bool:
        return attraction.score_breakdown.get("preference_match", 0) > 0

    @classmethod
    def _is_niche_cultural(cls, attraction: Attraction) -> bool:
        return (
            cls._diversity_bucket(attraction)
            in {"museum", "cultural_venue"}
            and attraction.first_visit_priority <= 6
        )

    @staticmethod
    def _diversity_bucket(attraction: Attraction) -> str:
        categories = set(attraction.categories)
        text = f"{attraction.name} {attraction.category or ''}"
        if "museum" in categories or any(
            term in text
            for term in ("博物馆", "美术馆", "展览馆", "纪念馆", "科技馆")
        ):
            return "museum"
        if any(
            term in text
            for term in ("剧院", "剧场", "艺术中心", "演艺中心")
        ):
            return "cultural_venue"
        for category in (
            "historic",
            "natural",
            "park",
            "shopping",
            "temple",
            "amusement",
            "zoo",
        ):
            if category in categories:
                return category
        return sorted(categories)[0] if categories else "general"

    def _cluster_by_day(
        self,
        attractions: Sequence[Attraction],
        travel_days: int,
        must_visit: Sequence[str],
        pace: str,
        hotel_location: Optional[Location],
        transportation: str,
        time_budget_minutes: int,
    ) -> List[List[Attraction]]:
        if not attractions:
            return []

        known_area_count = sum(bool(item.area) for item in attractions)
        if known_area_count >= max(1, len(attractions) // 2):
            return self._cluster_by_area(
                attractions,
                travel_days,
                must_visit,
                pace,
                hotel_location,
                transportation,
                time_budget_minutes,
            )

        active_days = min(travel_days, len(attractions))
        profile = get_pace_profile(pace)

        ranked = sorted(attractions, key=lambda item: self._priority_key(item, must_visit))
        seeds = [ranked[0]]
        remaining = ranked[1:]
        while len(seeds) < active_days:
            next_seed = remaining[0]
            next_distance = -1.0
            for candidate in remaining:
                distance_to_nearest_seed = min(
                    haversine_meters(candidate.location, seed.location) for seed in seeds
                )
                if distance_to_nearest_seed > next_distance:
                    next_seed = candidate
                    next_distance = distance_to_nearest_seed
            seeds.append(next_seed)
            remaining.remove(next_seed)

        groups = [[seed] for seed in seeds]
        while remaining:
            best_pair = None
            for candidate in remaining:
                candidate_key = self._priority_key(candidate, must_visit)
                for group_index, group in enumerate(groups):
                    is_must_visit = self._is_must_visit(candidate, must_visit)
                    if len(group) >= profile.attractions_per_day and not is_must_visit:
                        continue
                    proposed_group = self._nearest_neighbor_order(
                        [*group, candidate], must_visit, hotel_location
                    )
                    timing = self.estimate_day_timing(
                        proposed_group, hotel_location, transportation, profile
                    )
                    if (
                        timing.total_minutes > time_budget_minutes
                        and not is_must_visit
                    ):
                        continue
                    distance = haversine_meters(
                        candidate.location, self._centroid(group)
                    )
                    pair_key = (candidate_key, distance, timing.total_minutes, group_index)
                    if best_pair is None or pair_key < best_pair[0]:
                        best_pair = (pair_key, candidate, group_index)

            if best_pair is None:
                break
            _, candidate, group_index = best_pair
            groups[group_index].append(candidate)
            remaining.remove(candidate)

        return groups

    def _cluster_by_area(
        self,
        attractions: Sequence[Attraction],
        travel_days: int,
        must_visit: Sequence[str],
        pace: str,
        hotel_location: Optional[Location],
        transportation: str,
        time_budget_minutes: int,
    ) -> List[List[Attraction]]:
        """Allocate explicit areas to days before optimizing within each day."""

        buckets: Dict[str, List[Attraction]] = {}
        for attraction in attractions:
            area = attraction.area or self._nearest_known_area(attraction, attractions)
            buckets.setdefault(area or "未标注区域", []).append(attraction)
        for bucket in buckets.values():
            bucket.sort(key=lambda item: self._priority_key(item, must_visit))

        area_order = sorted(
            buckets,
            key=lambda area: (
                min(self._priority_key(item, must_visit) for item in buckets[area]),
                area,
            ),
        )
        active_days = min(travel_days, len(attractions))
        groups: List[List[Attraction]] = [[] for _ in range(active_days)]
        group_areas: List[set[str]] = [set() for _ in range(active_days)]
        profile = get_pace_profile(pace)

        # Give the strongest areas their own day first.
        for index, area in enumerate(area_order[:active_days]):
            groups[index].append(buckets[area].pop(0))
            group_areas[index].add(area)

        # A city-wide core coverage decision is ineffective if the area
        # seeding step immediately displaces a second core landmark merely
        # because it shares an area with the first one.  When a selected core
        # is still unseeded, prefer it over a non-core seed.  This does not
        # add capacity or relax timing; it only gives the route optimizer a
        # feasible day in which to place the already-selected core POI.
        if any("base_quality" in item.score_breakdown for item in attractions):
            unseeded_cores = sorted(
                (
                    item
                    for area in area_order
                    for item in buckets[area]
                    if self._portfolio_role(item) == "core_landmark"
                ),
                key=lambda item: self._priority_key(item, must_visit),
            )
            replaceable_days = sorted(
                (
                    day_index
                    for day_index, group in enumerate(groups)
                    if group
                    and self._portfolio_role(group[0]) != "core_landmark"
                ),
                key=lambda day_index: self._priority_key(
                    groups[day_index][0], must_visit
                ),
                reverse=True,
            )
            for core, day_index in zip(unseeded_cores, replaceable_days):
                old_seed = groups[day_index][0]
                old_area = next(iter(group_areas[day_index]))
                core_area = core.area or self._nearest_known_area(core, attractions)
                buckets[core_area].remove(core)
                buckets.setdefault(old_area, []).append(old_seed)
                buckets[old_area].sort(
                    key=lambda item: self._priority_key(item, must_visit)
                )
                groups[day_index] = [core]
                group_areas[day_index] = {core_area}

        # When there are fewer areas than travel days, split a large area
        # across multiple days instead of leaving a day empty.
        for day_index in range(len(area_order), active_days):
            splittable = [area for area in area_order if buckets[area]]
            if not splittable:
                break
            seeded_categories = {
                self._diversity_bucket(item)
                for group in groups
                for item in group
            }
            area, seed = min(
                (
                    (area, attraction)
                    for area in splittable
                    for attraction in buckets[area]
                ),
                key=lambda item: (
                    self._diversity_bucket(item[1]) in seeded_categories,
                    self._priority_key(item[1], must_visit),
                    -len(buckets[item[0]]),
                    item[0],
                ),
            )
            groups[day_index].append(seed)
            buckets[area].remove(seed)
            group_areas[day_index].add(area)

        remaining = sorted(
            (
                (area, attraction)
                for area in area_order
                for attraction in buckets[area]
            ),
            key=lambda pair: self._priority_key(pair[1], must_visit),
        )

        # Phase 1: fill the day's primary area first. This prevents a slightly
        # higher-scoring cross-city POI from consuming the time needed for
        # nearby attractions such as Summer Palace + Old Summer Palace.
        assigned_ids = set()
        for day_index, group in enumerate(groups):
            if not group:
                continue
            primary_area = next(iter(group_areas[day_index]))
            primary_candidates = [
                attraction
                for area, attraction in remaining
                if area == primary_area
            ]
            while (
                primary_candidates
                and len(groups[day_index]) < self.max_attractions_per_day
            ):
                current_visit = sum(
                    item.visit_duration for item in groups[day_index]
                )
                if (
                    current_visit / max(1, time_budget_minutes)
                    >= self.target_utilization_ratio
                ):
                    break
                feasible = []
                for attraction in primary_candidates:
                    proposed = self._beam_search_order(
                        [*groups[day_index], attraction],
                        must_visit,
                        hotel_location,
                        transportation,
                        time_budget_minutes,
                    )
                    timing = self.estimate_day_timing(
                        proposed, hotel_location, transportation, profile
                    )
                    if timing.total_minutes <= time_budget_minutes:
                        feasible.append(
                            (
                                self._next_poi_rank(
                                    groups[day_index],
                                    attraction,
                                    hotel_location,
                                    transportation,
                                    time_budget_minutes,
                                ),
                                proposed,
                                attraction,
                            )
                        )
                if not feasible:
                    break
                _, proposed, attraction = min(feasible, key=lambda item: item[0])
                groups[day_index] = proposed
                assigned_ids.add(id(attraction))
                primary_candidates.remove(attraction)

        remaining = [
            pair for pair in remaining if id(pair[1]) not in assigned_ids
        ]

        # Phase 2: only under-filled days may adopt a second area. Remote days
        # accept additions only when the POI is genuinely nearby.
        for area, attraction in remaining:
            is_required = self._is_must_visit(attraction, must_visit)
            candidates = []
            for day_index, group in enumerate(groups):
                proposed_areas = group_areas[day_index] | {area}
                if len(proposed_areas) > 2 and not is_required:
                    continue
                if (
                    len(group) >= self.max_attractions_per_day
                    and not is_required
                ):
                    continue
                utilization = sum(
                    item.visit_duration for item in group
                ) / max(1, time_budget_minutes)
                if utilization >= self.min_utilization_ratio and not is_required:
                    continue
                current_timing = self.estimate_day_timing(
                    group, hotel_location, transportation, profile
                )
                if (
                    current_timing.travel_minutes
                    > self.remote_travel_threshold_minutes
                    and group
                    and haversine_meters(
                        attraction.location, self._centroid(group)
                    )
                    > self.remote_nearby_radius_meters
                    and not is_required
                ):
                    continue
                proposed = self._beam_search_order(
                    [*group, attraction],
                    must_visit,
                    hotel_location,
                    transportation,
                    time_budget_minutes,
                )
                timing = self.estimate_day_timing(
                    proposed, hotel_location, transportation, profile
                )
                if timing.total_minutes > time_budget_minutes and not is_required:
                    continue
                same_area_penalty = 0 if area in group_areas[day_index] else 1
                candidates.append(
                    (
                        same_area_penalty,
                        len(proposed_areas),
                        self._next_poi_rank(
                            group,
                            attraction,
                            hotel_location,
                            transportation,
                            time_budget_minutes,
                        ),
                        day_index,
                        proposed,
                    )
                )
            if not candidates:
                continue
            *_, day_index, proposed = min(candidates)
            groups[day_index] = proposed
            group_areas[day_index].add(area)
        return groups

    def _next_poi_rank(
        self,
        current_group: Sequence[Attraction],
        attraction: Attraction,
        hotel_location: Optional[Location],
        transportation: str,
        time_budget_minutes: int,
    ) -> tuple:
        """Rank a fill candidate by value, distance, walking and transfers."""

        origin = (
            current_group[-1].location
            if current_group
            else hotel_location or attraction.location
        )
        leg = estimate_leg(origin, attraction.location, transportation)
        value = self.attraction_scorer.route_increment_score(
            attraction,
            current_group,
            origin,
            time_budget_minutes,
        ) / 100
        time_cost = leg.travel_minutes / max(1, time_budget_minutes)
        walking_cost = leg.walking_meters / 8_000
        transfer_cost = leg.transfers
        bucket = self._diversity_bucket(attraction)
        repeated_category_count = sum(
            self._diversity_bucket(item) == bucket
            for item in current_group
        )
        diversity_cost = repeated_category_count * 0.08
        if (
            self._is_niche_cultural(attraction)
            and any(self._is_niche_cultural(item) for item in current_group)
            and not self._has_explicit_preference_match(attraction)
        ):
            diversity_cost += 0.2
        score = (
            value * 0.4
            - time_cost * 0.3
            - walking_cost * 0.2
            - transfer_cost * 0.1
            - diversity_cost
        )
        return (
            -score,
            -attraction.first_visit_priority,
            -attraction.popularity,
            attraction.name,
        )

    @staticmethod
    def _nearest_known_area(
        attraction: Attraction, attractions: Sequence[Attraction]
    ) -> str:
        known = [item for item in attractions if item.area and item is not attraction]
        if not known:
            return ""
        return min(
            known,
            key=lambda item: (
                haversine_meters(attraction.location, item.location),
                item.area,
            ),
        ).area

    def _rebalance_groups(
        self,
        groups: List[List[Attraction]],
        must_visit: Sequence[str],
        hotel_location: Optional[Location],
        transportation: str,
        profile: PaceProfile,
        time_budget_minutes: int,
    ) -> List[List[Attraction]]:
        """Avoid a heavily loaded day when a lighter day can accept an optional POI."""

        while len(groups) > 1:
            timings = [self.estimate_day_timing(group, hotel_location, transportation, profile) for group in groups]
            donor_index = max(range(len(groups)), key=lambda index: timings[index].total_minutes)
            target_index = min(range(len(groups)), key=lambda index: timings[index].total_minutes)
            if timings[donor_index].total_minutes - timings[target_index].total_minutes < 90:
                break
            spread_before = (
                max(item.total_minutes for item in timings)
                - min(item.total_minutes for item in timings)
            )
            optional = [item for item in groups[donor_index] if not self._is_must_visit(item, must_visit)]
            moved = False
            for attraction in sorted(optional, key=lambda item: (item.visit_duration, item.score, item.name), reverse=True):
                target_areas = {
                    item.area for item in groups[target_index] if item.area
                }
                donor_areas = {
                    item.area for item in groups[donor_index] if item.area
                }
                if (
                    attraction.area
                    and target_areas
                    and attraction.area not in target_areas
                    and len(donor_areas) <= 2
                ):
                    continue
                proposed = self._beam_search_order(
                    [*groups[target_index], attraction],
                    must_visit,
                    hotel_location,
                    transportation,
                    time_budget_minutes,
                )
                proposed_timing = self.estimate_day_timing(
                    proposed,
                    hotel_location,
                    transportation,
                    profile,
                )
                if proposed_timing.total_minutes > time_budget_minutes:
                    continue
                donor_after = [
                    item
                    for item in groups[donor_index]
                    if item is not attraction
                ]
                donor_timing = self.estimate_day_timing(
                    donor_after,
                    hotel_location,
                    transportation,
                    profile,
                )
                new_totals = [
                    timing.total_minutes
                    for index, timing in enumerate(timings)
                    if index not in {donor_index, target_index}
                ] + [
                    donor_timing.total_minutes,
                    proposed_timing.total_minutes,
                ]
                if max(new_totals) - min(new_totals) >= spread_before:
                    continue
                groups[donor_index].remove(attraction)
                groups[target_index] = proposed
                moved = True
                break
            if not moved:
                break
        return groups

    def estimate_day_timing(
        self,
        attractions: Sequence[Attraction],
        hotel_location: Optional[Location],
        transportation: str,
        profile: PaceProfile,
    ) -> DayTiming:
        if not attractions:
            return DayTiming(visit_minutes=0, travel_minutes=0, buffer_minutes=0)

        visit_minutes = sum(item.visit_duration for item in attractions)
        travel_minutes = sum(
            estimate_leg(left.location, right.location, transportation).travel_minutes
            for left, right in zip(attractions, attractions[1:])
        )
        if hotel_location is not None:
            travel_minutes += estimate_leg(
                hotel_location, attractions[0].location, transportation
            ).travel_minutes
            travel_minutes += estimate_leg(
                attractions[-1].location, hotel_location, transportation
            ).travel_minutes
        return DayTiming(
            visit_minutes=visit_minutes,
            travel_minutes=travel_minutes,
            buffer_minutes=profile.daily_buffer_minutes,
        )

    def _nearest_neighbor_order(
        self,
        attractions: Sequence[Attraction],
        must_visit: Sequence[str],
        hotel_location: Optional[Location],
    ) -> List[Attraction]:
        if not attractions:
            return []

        remaining = sorted(
            attractions, key=lambda item: self._priority_key(item, must_visit)
        )
        if hotel_location is not None:
            first = min(
                remaining,
                key=lambda item: (
                    closing_time_priority(item),
                    haversine_meters(hotel_location, item.location),
                    self._priority_key(item, must_visit),
                ),
            )
        else:
            first = remaining[0]

        ordered = [first]
        remaining.remove(first)
        while remaining:
            current = ordered[-1]
            nearest = min(
                remaining,
                key=lambda item: (
                    closing_time_priority(item),
                    haversine_meters(current.location, item.location),
                    self._priority_key(item, must_visit),
                ),
            )
            ordered.append(nearest)
            remaining.remove(nearest)
        return ordered

    def _beam_search_order(
        self,
        attractions: Sequence[Attraction],
        must_visit: Sequence[str],
        hotel_location: Optional[Location],
        transportation: str,
        time_budget_minutes: int,
        beam_width: int = 12,
    ) -> List[Attraction]:
        """Optimize a small daily route with a bounded, deterministic beam."""

        if len(attractions) <= 1:
            return list(attractions)
        ranked = sorted(attractions, key=lambda item: self._priority_key(item, must_visit))
        # state: (ordered, remaining, travel minutes, walk meters, transfers)
        beam = [([], ranked, 0, 0.0, 0)]
        while beam and beam[0][1]:
            expanded = []
            for ordered, remaining, travel, walk, transfers in beam:
                origin = ordered[-1].location if ordered else hotel_location
                for candidate in remaining:
                    leg = (
                        estimate_leg(origin, candidate.location, transportation)
                        if origin is not None
                        else LegEstimate("walking", 0, 0, 0)
                    )
                    next_ordered = [*ordered, candidate]
                    next_remaining = [item for item in remaining if item is not candidate]
                    expanded.append(
                        (
                            next_ordered,
                            next_remaining,
                            travel + leg.travel_minutes,
                            walk + leg.walking_meters,
                            transfers + leg.transfers,
                        )
                    )
            beam = sorted(
                expanded,
                key=lambda state: (
                    -self._partial_route_score(
                        state[0],
                        state[2],
                        state[3],
                        state[4],
                        time_budget_minutes,
                    ),
                    tuple(item.name for item in state[0]),
                ),
            )[:beam_width]

        completed = []
        for ordered, remaining, travel, walk, transfers in beam:
            if hotel_location is not None and ordered:
                leg = estimate_leg(
                    ordered[-1].location, hotel_location, transportation
                )
                travel += leg.travel_minutes
                walk += leg.walking_meters
                transfers += leg.transfers
            completed.append(
                (
                    -self._partial_route_score(
                        ordered, travel, walk, transfers, time_budget_minutes
                    ),
                    tuple(item.name for item in ordered),
                    ordered,
                )
            )
        return (
            min(completed, key=lambda item: (item[0], item[1]))[2]
            if completed
            else ranked
        )

    @staticmethod
    def _partial_route_score(
        ordered: Sequence[Attraction],
        travel_minutes: int,
        walking_meters: float,
        transfers: int,
        time_budget_minutes: int,
    ) -> float:
        if not ordered:
            return 0
        value = sum(item.score for item in ordered) / (100 * len(ordered))
        travel_penalty = min(1.0, travel_minutes / max(1, time_budget_minutes))
        walk_penalty = min(1.0, walking_meters / 8_000)
        transfer_penalty = min(1.0, transfers / max(1, len(ordered)))
        closing_penalty = sum(
            closing_time_priority(left) > closing_time_priority(right)
            for left, right in zip(ordered, ordered[1:])
        ) / max(1, len(ordered) - 1)
        return (
            value * 0.4
            - travel_penalty * 0.3
            - walk_penalty * 0.2
            - transfer_penalty * 0.1
            - closing_penalty * 0.2
        )

    def _priority_key(
        self, attraction: Attraction, must_visit: Sequence[str]
    ) -> Tuple[int, float, str, str, float, float]:
        return (
            0 if self._is_must_visit(attraction, must_visit) else 1,
            -attraction.score,
            normalize_place_name(attraction.name),
            attraction.poi_id or "",
            attraction.location.longitude,
            attraction.location.latitude,
        )

    def _is_must_visit(
        self, attraction: Attraction, must_visit: Sequence[str]
    ) -> bool:
        return any(place_names_match(name, attraction.name) for name in must_visit)

    def _centroid(self, attractions: Sequence[Attraction]) -> Location:
        count = len(attractions)
        return Location(
            longitude=sum(item.location.longitude for item in attractions) / count,
            latitude=sum(item.location.latitude for item in attractions) / count,
        )
