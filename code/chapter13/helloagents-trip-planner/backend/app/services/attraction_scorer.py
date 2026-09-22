"""Explainable scoring for attraction selection and route expansion."""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Iterable, Sequence

from ..models.schemas import Attraction, Location, TripRequest
from ..config import settings
from .poi_metadata_service import preference_matches_categories
from .travel_knowledge_service import TravelKnowledgeService, get_travel_knowledge_service


class AttractionScorer:
    """Score a POI for a request without enforcing hard constraints.

    Validators remain the authority for hard limits. This scorer deliberately
    handles only soft route quality: fit, value, proximity and variety.
    """

    def __init__(self, knowledge_service: TravelKnowledgeService | None = None) -> None:
        self.knowledge_service = knowledge_service or get_travel_knowledge_service()

    def score_candidates(
        self,
        attractions: Iterable[Attraction],
        request: TripRequest,
        hotel_location: Location | None,
        available_minutes: int,
        weather_risks: Sequence[str] = (),
    ) -> list[Attraction]:
        return [
            self.score_attraction(
                attraction,
                request,
                anchor=hotel_location,
                existing=(),
                available_minutes=available_minutes,
                weather_risks=weather_risks,
            )
            for attraction in attractions
        ]

    def score_attraction(
        self,
        attraction: Attraction,
        request: TripRequest,
        *,
        anchor: Location | None,
        existing: Sequence[Attraction],
        available_minutes: int,
        weather_risks: Sequence[str] = (),
    ) -> Attraction:
        tags = self._tags(attraction)
        inherited = dict(attraction.score_breakdown)
        has_curated_score = "base_quality" in inherited
        preference_matched = preference_matches_categories(
            request.preferences, set(attraction.categories)
        )
        preference = inherited.get(
            "pref_bonus",
            self._preference_score(tags, request.preferences),
        )
        popularity = inherited.get(
            "base_quality", attraction.popularity * 2.5
        )
        classic = inherited.get("classic_bonus", 0.0)
        # enrich_attraction already applies the curated classic and niche
        # policy. Preserve that intrinsic ranking; this layer only adds route
        # context instead of silently replacing it with a generic score.
        base_score = attraction.score if has_curated_score else 20.0 + popularity + preference
        travel_minutes = self.estimated_travel_minutes(anchor, attraction.location)
        distance_penalty = min(22.0, travel_minutes * 0.16)
        if travel_minutes > 90:
            distance_penalty += min(18.0, (travel_minutes - 90) * 0.25)
        budget_penalty = self._budget_penalty(attraction, request)
        duration = attraction.suggested_duration_minutes or attraction.visit_duration
        time_penalty = self._time_penalty(duration, available_minutes)
        category = self.category_bucket(attraction)
        repeat_count = sum(self.category_bucket(item) == category for item in existing)
        diversity_penalty = 0.0 if repeat_count == 0 else 8.0 * repeat_count
        knowledge_delta = 0.0
        knowledge_breakdown = {}
        if settings.enable_travel_knowledge:
            knowledge_delta, knowledge_breakdown = self.knowledge_service.score_delta(
                attraction,
                request,
                weather_risks=weather_risks,
                available_minutes=available_minutes,
            )

        total = max(
            0.0,
            min(
                100.0,
                base_score + knowledge_delta
                - distance_penalty - budget_penalty - time_penalty - diversity_penalty,
            ),
        )
        attraction.score = round(total, 2)
        attraction.score_breakdown = {
            **inherited,
            "selection_base_score": round(base_score, 2),
            "preference_score": round(preference, 2),
            "popularity_score": round(popularity, 2),
            "classic_score": round(classic, 2),
            "distance_penalty": round(-distance_penalty, 2),
            "budget_penalty": round(-budget_penalty, 2),
            "time_fit_penalty": round(-time_penalty, 2),
            "diversity_penalty": round(-diversity_penalty, 2),
            **knowledge_breakdown,
            "knowledge_total_delta": round(knowledge_delta, 2),
            "estimated_travel_minutes": round(travel_minutes, 2),
            "total_score": round(total, 2),
            "preference_match": 10.0 if preference_matched else inherited.get("preference_match", 0.0),
        }
        return attraction

    def route_increment_score(
        self,
        attraction: Attraction,
        existing: Sequence[Attraction],
        origin: Location | None,
        available_minutes: int,
    ) -> float:
        """Return route-context score while preserving the candidate base score."""

        travel_minutes = self.estimated_travel_minutes(origin, attraction.location)
        repeat_count = sum(
            self.category_bucket(item) == self.category_bucket(attraction)
            for item in existing
        )
        duration = attraction.suggested_duration_minutes or attraction.visit_duration
        return (
            attraction.score
            - min(35.0, travel_minutes * 0.35)
            - self._time_penalty(duration + travel_minutes, available_minutes)
            - repeat_count * 10.0
        )

    @staticmethod
    def category_bucket(attraction: Attraction) -> str:
        tags = set(AttractionScorer._tags(attraction))
        if "museum" in tags:
            return "museum"
        for category in ("park", "natural", "shopping", "temple", "amusement", "zoo", "historic"):
            if category in tags:
                return category
        return next(iter(sorted(tags)), "general")

    @staticmethod
    def estimated_travel_minutes(origin: Location | None, destination: Location) -> float:
        if origin is None:
            return 0.0
        distance = AttractionScorer._haversine_meters(origin, destination)
        # Conservative mixed public-transport estimate for candidate ranking.
        return distance / 333.0 + (15.0 if distance >= 1500 else 0.0)

    @staticmethod
    def _tags(attraction: Attraction) -> list[str]:
        return list(dict.fromkeys([*attraction.tags, *attraction.categories, (attraction.category or "").lower()]))

    @staticmethod
    def _preference_score(tags: Sequence[str], preferences: Sequence[str]) -> float:
        if not preferences:
            return 15.0  # General baseline; no implicit history/culture preference.
        text = " ".join(tags).lower()
        matches = sum(preference.lower() in text for preference in preferences)
        return min(35.0, 12.0 + matches * 12.0)

    @staticmethod
    def _budget_penalty(attraction: Attraction, request: TripRequest) -> float:
        if request.budget_limit is None:
            return 0.0
        daily_budget = request.budget_limit / max(1, request.travel_days)
        return min(25.0, attraction.ticket_price / max(1.0, daily_budget) * 40.0)

    @staticmethod
    def _time_penalty(duration: int, available_minutes: int) -> float:
        if duration <= available_minutes:
            return 0.0
        return min(30.0, (duration - available_minutes) / max(1, available_minutes) * 30.0)

    @staticmethod
    def _haversine_meters(left: Location, right: Location) -> float:
        earth_radius = 6_371_000
        latitude_delta = radians(right.latitude - left.latitude)
        longitude_delta = radians(right.longitude - left.longitude)
        value = sin(latitude_delta / 2) ** 2 + cos(radians(left.latitude)) * cos(radians(right.latitude)) * sin(longitude_delta / 2) ** 2
        return 2 * earth_radius * asin(min(1.0, sqrt(value)))
