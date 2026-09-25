"""Deterministic hotel selection using itinerary accessibility and budget."""

from __future__ import annotations

from copy import deepcopy
from typing import Sequence

from ..models.schemas import Attraction, Hotel, TripRequest
from .spatial_planner import estimate_leg
from .trip_cost_service import stay_nights


class AccommodationSelector:
    """Choose among existing hotel candidates without changing hotel search."""

    def select_for_days(self, request, candidates, days, amap):
        attractions = [a for day in days for a in day.attractions]
        baseline = self.select(request, candidates, attractions)
        if not getattr(getattr(amap, "settings", None), "amap_api_key", ""):
            return baseline
        from .mixed_route_selector import select_mixed_route
        tier = self._tier(request.accommodation)
        located = [h for h in candidates if h.location is not None]
        matching = [h for h in located if self._tier(h.type) == tier
                    and (request.budget_limit is None or h.estimated_cost * stay_nights(request) * request.room_count <= request.budget_limit * 0.5)]
        pool = sorted((self.score_candidate(request, deepcopy(h), attractions) for h in (matching or located)),
                      key=lambda h: -h.selection_score)[:3]
        checked = []
        for hotel in pool:
            routes = []
            for day in days:
                if not day.attractions:
                    continue
                for outbound, attraction in ((True, day.attractions[0]), (False, day.attractions[-1])):
                    point = (attraction.entrance_location if outbound else attraction.exit_location) or attraction.location
                    kwargs = dict(origin_name=hotel.name if outbound else attraction.name,
                                  origin_address="", origin=hotel.location if outbound else point,
                                  destination_name=attraction.name if outbound else hotel.name,
                                  destination_address="", destination=point if outbound else hotel.location, city=request.city)
                    try:
                        if any(x in request.transportation.lower() for x in ("混合", "mixed")):
                            route = select_mixed_route(amap, kwargs, request, 1000)
                        else:
                            mode = estimate_leg(kwargs["origin"], kwargs["destination"], request.transportation).mode
                            route = amap.route_between_pois(**kwargs, route_type=mode)
                        if not route.description.startswith("[amap]"):
                            routes = []
                            break
                        routes.append(route)
                    except Exception:
                        routes = []
                        break
                if not routes:
                    break
            expected = 2 * sum(bool(day.attractions) for day in days)
            if len(routes) != expected or not routes:
                continue
            minutes = sum(r.duration / 60 for r in routes) / len(routes)
            walking = sum(r.distance if r.route_type == "walking" else r.walking_distance for r in routes) / len(routes)
            hotel.selection_score = round(self._budget_score(request, hotel) + self._type_score(request.accommodation, hotel)
                                          - minutes - walking / 100, 2)
            hotel.score_breakdown.update(actual_commute_minutes=round(minutes, 1), actual_commute_walking_meters=round(walking))
            hotel.distance = f"按每日首末景点比较，单程平均约{minutes:.0f}分钟；已比较{len(pool)}家候选"
            checked.append(hotel)
        return max(checked, key=lambda h: h.selection_score) if checked else baseline

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
