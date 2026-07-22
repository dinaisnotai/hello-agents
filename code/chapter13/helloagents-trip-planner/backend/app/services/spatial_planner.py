"""Deterministic, lightweight spatial planning for daily itineraries."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..models.schemas import Attraction, Location
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


class SpatialItineraryPlanner:
    """Select high-priority POIs, cluster them by day, then order each day."""

    def plan(
        self,
        attractions: Sequence[Attraction],
        travel_days: int,
        pace: str,
        must_visit: Sequence[str],
        hotel_location: Optional[Location] = None,
        transportation: str = "公共交通",
    ) -> List[List[Attraction]]:
        if travel_days <= 0:
            return []

        selected = self.select_attractions(attractions, travel_days, pace, must_visit)
        for attraction in selected:
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
        )
        ordered_groups = [
            self._nearest_neighbor_order(group, must_visit, hotel_location)
            for group in groups
        ]
        return ordered_groups + [[] for _ in range(travel_days - len(ordered_groups))]

    def select_attractions(
        self,
        attractions: Sequence[Attraction],
        travel_days: int,
        pace: str,
        must_visit: Sequence[str],
    ) -> List[Attraction]:
        """Keep must-visits, then fill the pace capacity with top-scoring POIs."""

        unique = {}
        for attraction in attractions:
            key = normalize_place_name(attraction.name)
            existing = unique.get(key)
            if existing is None or self._priority_key(
                attraction, must_visit
            ) < self._priority_key(existing, must_visit):
                unique[key] = attraction

        ranked = sorted(unique.values(), key=lambda item: self._priority_key(item, must_visit))
        must_visit_count = sum(self._is_must_visit(item, must_visit) for item in ranked)
        pace_capacity = travel_days * get_pace_profile(pace).attractions_per_day
        selection_limit = max(pace_capacity, must_visit_count)
        return ranked[:selection_limit]

    def _cluster_by_day(
        self,
        attractions: Sequence[Attraction],
        travel_days: int,
        must_visit: Sequence[str],
        pace: str,
        hotel_location: Optional[Location],
        transportation: str,
    ) -> List[List[Attraction]]:
        if not attractions:
            return []

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
                        timing.total_minutes > profile.daily_time_budget_minutes
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
            estimate_travel_minutes(left.location, right.location, transportation)
            for left, right in zip(attractions, attractions[1:])
        )
        if hotel_location is not None:
            travel_minutes += estimate_travel_minutes(
                hotel_location, attractions[0].location, transportation
            )
            travel_minutes += estimate_travel_minutes(
                attractions[-1].location, hotel_location, transportation
            )
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
                    haversine_meters(current.location, item.location),
                    self._priority_key(item, must_visit),
                ),
            )
            ordered.append(nearest)
            remaining.remove(nearest)
        return ordered

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
