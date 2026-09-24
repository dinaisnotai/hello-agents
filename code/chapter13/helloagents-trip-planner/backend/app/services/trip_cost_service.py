"""Transparent planning allowances. No estimate is a supplier quote."""

import math
from datetime import date

from ..config import settings
from ..models.schemas import DayPlan, TripRequest


def stay_nights(request: TripRequest) -> int:
    # The itinerary ends on checkout day. A day trip needs no hotel night.
    return max(0, (date.fromisoformat(request.end_date) - date.fromisoformat(request.start_date)).days)


def transport_mode(transportation: str, route_type: str) -> str:
    if route_type == "walking":
        return "walking"
    if route_type in {"transit", "bus", "subway", "railway"}:
        return "transit"
    if any(word in transportation.lower() for word in ("租车", "rental")):
        return "rental"
    if any(word in transportation.lower() for word in ("自驾", "自有", "driving", "own car")):
        return "own_car"
    return "taxi"


def price_day(day: DayPlan, request: TripRequest, *, rental_days: int = 0) -> int:
    vehicles = math.ceil(request.party_size / max(1, settings.vehicle_capacity))
    for leg in day.route_segments:
        mode = transport_mode(day.transportation, leg.route_type)
        distance = max(0, leg.distance_meters) / 1000
        leg.cost_mode = mode
        leg.cost_source = "planning_estimate"
        if mode == "walking":
            cost, note = 0, "步行，无交通票价"
        elif mode == "transit":
            cost = settings.transit_fare_per_leg * request.party_size
            note = f"每段预留{settings.transit_fare_per_leg:g}元/人 × {request.party_size}人；未查询实际票价，城际交通可能更高"
        elif mode == "taxi":
            cost = (settings.taxi_base_fare + max(0, distance - settings.taxi_included_km) * settings.taxi_per_km) * vehicles
            note = f"按{vehicles}辆普通车型估算；起步{settings.taxi_base_fare:g}元含{settings.taxi_included_km:g}km，超出{settings.taxi_per_km:g}元/km；未含等待、夜间、动态加价及通行费"
        else:
            cost = distance * (settings.driving_cost_per_km + settings.toll_per_km) * vehicles
            note = f"{vehicles}辆车，油电预留{settings.driving_cost_per_km:g}元/km，通行费预留{settings.toll_per_km:g}元/km；非实际账单"
        leg.estimated_cost = math.ceil(cost)
        leg.cost_low = math.floor(cost * 0.8)
        leg.cost_high = math.ceil(cost * 1.5)
        leg.cost_note = note
    day.transport_fixed_costs = {}
    own_vehicle = any(word in day.transportation.lower() for word in ("自驾", "自有", "driving", "own car", "租车", "rental"))
    if own_vehicle and any(leg.route_type == "driving" for leg in day.route_segments):
        day.transport_fixed_costs["停车费预留"] = math.ceil(settings.parking_per_day * vehicles)
    if rental_days:
        day.transport_fixed_costs["租车租金"] = math.ceil(settings.rental_per_day * rental_days * vehicles)
        day.transport_fixed_costs["租车保险预留"] = math.ceil(settings.rental_insurance_per_day * rental_days * vehicles)
    day.daily_transport_cost = sum(leg.estimated_cost for leg in day.route_segments) + sum(day.transport_fixed_costs.values())
    return day.daily_transport_cost
