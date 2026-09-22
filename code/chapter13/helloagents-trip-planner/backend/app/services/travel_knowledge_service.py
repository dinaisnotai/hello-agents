"""Safe projection of curated knowledge into existing planning primitives."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Iterable, Optional

from ..models.schemas import Attraction, TripRequest
from ..models.travel_knowledge import (
    EnvironmentType,
    KnowledgeClaimType,
    POIKnowledge,
    ScopedPlanningKnowledge,
    WalkingIntensity,
)
from .travel_knowledge_repository import TravelKnowledgeRepository, get_travel_knowledge_repository
from .poi_metadata_service import preference_matches_categories


class TravelKnowledgeService:
    """Resolve knowledge and expose bounded, explainable planning signals."""

    MAX_SCORE_DELTA = 15.0

    def __init__(self, repository: TravelKnowledgeRepository | None = None) -> None:
        self.repository = repository or get_travel_knowledge_repository()

    def resolve_poi(self, city: str, attraction: Attraction) -> Optional[POIKnowledge]:
        item = self.repository.resolve_poi(city, attraction)
        if item is None:
            return None
        if item.confidence < 0.6:
            # Low confidence remains available to explanation/retrieval, but
            # must not silently alter deterministic candidate features.
            return item
        return item

    def enrich_attraction(self, attraction: Attraction, request: TripRequest) -> Attraction:
        result = deepcopy(attraction)
        knowledge = self.resolve_poi(request.city, result)
        if knowledge is None or knowledge.confidence < 0.6:
            return result
        if knowledge.categories:
            result.categories = list(dict.fromkeys([*result.categories, *knowledge.categories]))
        if knowledge.experience_tags:
            result.tags = list(dict.fromkeys([*result.tags, *knowledge.experience_tags]))
        if knowledge.planning_zone and not result.area:
            result.area = knowledge.planning_zone
        if knowledge.recommended_duration_min is not None:
            lower = knowledge.recommended_duration_min
            upper = knowledge.recommended_duration_max or lower
            result.suggested_duration_minutes = round((lower + upper) / 2)
        if knowledge.walking_intensity != WalkingIntensity.UNKNOWN:
            result.intensity_level = knowledge.walking_intensity.value if knowledge.walking_intensity.value in {"low", "medium", "high"} else result.intensity_level
        if knowledge.estimated_internal_walking_km is not None:
            result.estimated_internal_walking_km = knowledge.estimated_internal_walking_km
        if knowledge.accessibility_features:
            result.accessible = True
        result.selection_trace = [
            *result.selection_trace,
            {
                "source": "travel_knowledge",
                "knowledge_id": knowledge.knowledge_id,
                "confidence": knowledge.confidence,
                "planning_zone": knowledge.planning_zone,
            },
        ]
        return result

    def scenario_tags(self, request: TripRequest, weather_risks: Iterable[str] = ()) -> list[str]:
        text = " ".join([request.free_text_input or "", *request.preferences, *request.soft_preferences]).lower()
        tags: list[str] = []
        travelers = " ".join(request.travelers).lower()
        if any(token in travelers + text for token in ("elderly", "senior", "老人", "老年", "少走")):
            tags.append("elderly_relaxed_trip")
        if any(token in travelers + text for token in ("child", "family", "亲子", "儿童", "孩子")):
            tags.append("family_with_children")
        if request.pace == "relaxed" or request.energy_preference == "low":
            tags.append("relaxed_first_visit")
        if any(token in " ".join(weather_risks).lower() for token in ("雨", "rain", "雷")):
            tags.append("rainy_day")
        if any(token in " ".join(weather_risks).lower() for token in ("高温", "hot", "炎热")):
            tags.append("hot_weather")
        if request.first_visit:
            tags.append("first_visit")
        return list(dict.fromkeys(tags))

    def query_scope_for_request(
        self,
        request: TripRequest,
        *,
        weather_risks: Iterable[str] = (),
        zone: str = "",
        categories: Iterable[str] = (),
    ) -> list[ScopedPlanningKnowledge]:
        return self.repository.query_scope(
            request.city,
            scenario_tags=self.scenario_tags(request, weather_risks),
            zone=zone,
            categories=categories,
        )

    def score_delta(
        self,
        attraction: Attraction,
        request: TripRequest,
        *,
        weather_risks: Iterable[str] = (),
        available_minutes: int = 0,
    ) -> tuple[float, dict[str, float]]:
        knowledge = self.resolve_poi(request.city, attraction)
        if knowledge is None or knowledge.confidence < 0.6:
            return 0.0, {}
        breakdown: dict[str, float] = {}
        preference_text = " ".join([*request.preferences, *request.soft_preferences]).lower()
        deep_exploration = any(
            token in (request.free_text_input or "").lower()
            for token in ("repeat visitor", "avoid popular", "in-depth", "深度", "小众", "多次到访")
        )
        if not deep_exploration and preference_text and (
            preference_matches_categories(request.preferences, set(knowledge.categories))
            or any(tag.lower() in preference_text for tag in [*knowledge.categories, *knowledge.experience_tags])
        ):
            breakdown["knowledge_preference_fit"] = round(8.0 * knowledge.confidence, 2)
        travelers = " ".join(request.travelers).lower()
        if travelers:
            if any(token in travelers for token in ("elderly", "senior", "老人", "老年")):
                if knowledge.walking_intensity == WalkingIntensity.LOW:
                    breakdown["knowledge_traveler_fit"] = round(6.0 * knowledge.confidence, 2)
                elif knowledge.walking_intensity == WalkingIntensity.HIGH:
                    breakdown["knowledge_traveler_fit"] = round(-8.0 * knowledge.confidence, 2)
            if any(token in travelers for token in ("child", "family", "亲子", "儿童")):
                if any(tag in {"family", "interactive", "zoo", "park"} for tag in [*knowledge.traveler_tags, *knowledge.experience_tags]):
                    breakdown["knowledge_traveler_fit"] = round(6.0 * knowledge.confidence, 2)
        risks = " ".join(weather_risks).lower()
        for condition, value in knowledge.weather_fit.items():
            if condition.lower() in risks:
                breakdown["knowledge_weather_fit"] = round(float(value) * knowledge.confidence, 2)
                break
        if travelers and any(token in travelers for token in ("elderly", "senior", "老人", "老年")) and knowledge.accessibility_features:
            breakdown["knowledge_accessibility_fit"] = round(5.0 * knowledge.confidence, 2)
        total = max(-self.MAX_SCORE_DELTA, min(self.MAX_SCORE_DELTA, sum(breakdown.values())))
        return round(total, 2), breakdown

    def pair_delta(self, left: Attraction, right: Attraction, request: TripRequest) -> float:
        left_knowledge = self.resolve_poi(request.city, left)
        right_name = right.name.lower()
        if left_knowledge is None or left_knowledge.confidence < 0.6:
            return 0.0
        if any(item.lower() in right_name for item in left_knowledge.recommended_combinations):
            return min(4.0, 4.0 * left_knowledge.confidence)
        if any(item.lower() in right_name for item in left_knowledge.avoid_combinations):
            return max(-12.0, -12.0 * left_knowledge.confidence)
        return 0.0

    def pair_delta_any(self, left: Attraction, right: Attraction) -> float:
        """Resolve a pair without changing SpatialPlanner's public signature.

        POI names are city-scoped in the repository.  We only use this helper
        for small curated corpora and return the strongest matching signal;
        normal route feasibility remains authoritative.
        """
        best = 0.0
        right_name = right.name.lower()
        for city in self.repository.cities():
            knowledge = self.repository.resolve_poi(city, left)
            if knowledge is None or knowledge.confidence < 0.6:
                continue
            if any(item.lower() in right_name for item in knowledge.recommended_combinations):
                best = max(best, min(4.0, 4.0 * knowledge.confidence))
            if any(item.lower() in right_name for item in knowledge.avoid_combinations):
                best = min(best, max(-12.0, -12.0 * knowledge.confidence))
        return best

    @staticmethod
    def is_indoor(knowledge: POIKnowledge | None) -> bool:
        return (
            knowledge is not None
            and knowledge.confidence >= 0.6
            and knowledge.environment_type in {EnvironmentType.INDOOR, EnvironmentType.MIXED}
        )


_service: TravelKnowledgeService | None = None


def get_travel_knowledge_service() -> TravelKnowledgeService:
    global _service
    if _service is None:
        _service = TravelKnowledgeService()
    return _service
