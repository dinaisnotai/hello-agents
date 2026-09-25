"""Compare actual mode candidates before accepting a mixed-mode leg."""
import math
from copy import deepcopy
from .spatial_planner import haversine_meters
from ..config import settings


def select_mixed_route(amap, kwargs, request, walking_allowance_m):
    distance = haversine_meters(kwargs["origin"], kwargs["destination"])
    modes = ["transit", "driving"]
    if distance <= 1800:
        modes.insert(0, "walking")
    candidates = []
    for mode in modes:
        try:
            route = amap.route_between_pois(**kwargs, route_type=mode)
            if route.duration > 0 and route.distance >= 0:
                candidates.append(route)
        except Exception:
            continue
    if not candidates:
        raise RuntimeError("混合交通没有可计算的路线")
    # Estimates may not beat verified routes by claiming implausibly low walking/time.
    verified = [r for r in candidates if r.description.startswith("[amap]")]
    candidates = verified or candidates
    def walking(route):
        return route.distance if route.route_type == "walking" else route.walking_distance
    feasible = [r for r in candidates if walking(r) <= walking_allowance_m]
    if request is not None:
        party = request.party_size
        # A bounded willingness-to-pay heuristic, not an assertion of affordability.
        per_person_day = (request.budget_limit or 1500) / max(1, request.travel_days * party)
        minutes_per_yuan = max(0.15, min(2.0, 150 / max(1, per_person_day)))
    else:
        party, minutes_per_yuan = 1, 1.0
    def cost(route):
        if route.route_type == "walking":
            return 0
        if route.route_type == "transit":
            return settings.transit_fare_per_leg * party
        return (settings.taxi_base_fare + max(0, route.distance / 1000 - settings.taxi_included_km) * settings.taxi_per_km) * math.ceil(party / settings.vehicle_capacity)
    def rank(route):
        return (max(0, walking(route) - walking_allowance_m),
                route.duration / 60 + walking(route) / 1000 * 15 + cost(route) / party * minutes_per_yuan)
    selected = deepcopy(min(feasible or candidates, key=rank))
    selected.description += f"；混合比选{len(candidates)}种方式，兼顾耗时、步行和估算费用"
    if selected.route_type == "driving":
        selected.description += "；按打车计费，上下车点到入口步行待核实，景区内部步行另计"
    return selected
