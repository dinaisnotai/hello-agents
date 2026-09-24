"""Deterministic hotel selection using itinerary accessibility and budget."""

from __future__ import annotations

from copy import deepcopy
from typing import Sequence

from ..models.schemas import Attraction, Hotel, TripRequest
from .spatial_planner import estimate_leg
from .trip_cost_service import stay_nights


class AccommodationSelector:
    """Choose among existing hotel candidates without changing hotel search."""

    def select(
        self,
        request: TripRequest,
        candidates: Sequence[Hotel],
        attractions: Sequence[Attraction],
    ) -> Hotel:
        if not candidates:
            return Hotel(
                name=f"{request.hotel_area or request.city}待确认酒店",
                type=request.accommodation,
            )

        requested_tier = self._tier(request.accommodation)
        matching = [hotel for hotel in candidates if self._tier(hotel.type) == requested_tier and requested_tier != "unspecified"]
        affordable = [hotel for hotel in matching if request.budget_limit is None or hotel.estimated_cost * stay_nights(request) * request.room_count <= request.budget_limit * 0.5]
        scored = [
            self.score_candidate(request, deepcopy(hotel), attractions)
            for hotel in (affordable or candidates)
        ]
        selected = max(
            scored,
            key=lambda hotel: (
                hotel.selection_score,
                -hotel.estimated_cost,
                hotel.name,
            ),
        )
        if requested_tier != "unspecified" and self._tier(selected.type) != requested_tier:
            selected.price_note += "；未确认符合所选住宿档次，请核实后预订"
        return selected

    def score_candidate(
        self,
        request: TripRequest,
        hotel: Hotel,
        attractions: Sequence[Attraction],
    ) -> Hotel:
        anchors = self._anchors(request, attractions)
        if hotel.location is None or not anchors:
            average_minutes = 90.0
            accessible_ratio = 0.0
        else:
            travel_minutes = [
                estimate_leg(
                    hotel.location,
                    attraction.location,
                    request.transportation,
                ).travel_minutes
                for attraction in anchors
            ]
            average_minutes = sum(travel_minutes) / len(travel_minutes)
            accessible_ratio = sum(
                minutes <= 45 for minutes in travel_minutes
            ) / len(travel_minutes)

        accessibility_score = accessible_ratio * 45
        transport_penalty = min(35.0, average_minutes * 0.45)
        budget_score = self._budget_score(request, hotel)
        type_score = self._type_score(request.accommodation, hotel)
        total = (
            accessibility_score
            + budget_score
            + type_score
            - transport_penalty
        )
        hotel.selection_score = round(total, 2)
        hotel.score_breakdown = {
            "attraction_accessibility": round(accessibility_score, 2),
            "transport_cost": round(-transport_penalty, 2),
            "budget": round(budget_score, 2),
            "accommodation_type": round(type_score, 2),
            "average_attraction_travel_minutes": round(
                average_minutes, 2
            ),
        }
        hotel.distance = (
            f"到重点景点平均约{average_minutes:.0f}分钟"
            if hotel.location and anchors
            else "位置数据不足，需确认交通"
        )
        return hotel

    @staticmethod
    def _anchors(
        request: TripRequest,
        attractions: Sequence[Attraction],
    ) -> list[Attraction]:
        required = [
            item
            for item in attractions
            if any(name in item.name or item.name in name for name in request.must_visit)
        ]
        remaining = [
            item for item in attractions if item not in required
        ]
        # This consumes existing attraction scores but does not change ranking.
        remaining.sort(key=lambda item: (-item.score, item.name))
        return [*required, *remaining[: max(4, request.travel_days * 2)]]

    @staticmethod
    def _budget_score(request: TripRequest, hotel: Hotel) -> float:
        if request.budget_limit is None:
            return max(0.0, 25.0 - hotel.estimated_cost / 100)
        hotel_total = hotel.estimated_cost * stay_nights(request) * request.room_count
        allocation = request.budget_limit * 0.5
        if hotel_total <= allocation:
            return 30.0
        over_ratio = (hotel_total - allocation) / max(1, allocation)
        return max(-30.0, 30.0 - over_ratio * 60)

    @classmethod
    def _type_score(cls, requested: str, hotel: Hotel) -> float:
        requested_tier = cls._tier(requested)
        candidate_tier = cls._tier(hotel.type)
        if requested_tier == "unspecified" or candidate_tier == "unspecified":
            return 0.0
        return 20.0 if requested_tier == candidate_tier else -20.0

    @staticmethod
    def _tier(text: str) -> str:
        value = (text or "").lower()
        if any(term in value for term in ("民宿", "homestay", "bnb", "guesthouse")):
            return "homestay"
        if any(term in value for term in ("豪华", "奢华", "luxury", "五星", "5-star")):
            return "luxury"
        if any(term in value for term in ("舒适", "高档型", "comfortable", "comfort")):
            return "comfortable"
        if any(term in value for term in ("经济", "旅馆招待所", "budget", "economy", "hostel")):
            return "budget"
        return "unspecified"
