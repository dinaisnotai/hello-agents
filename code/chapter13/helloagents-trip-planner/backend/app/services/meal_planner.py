"""Meals are independent stops, selected from map records near the route."""
from .venue_policy import venue_kind
from .spatial_planner import haversine_meters
from ..models.schemas import Meal


def schedule_stops(day):
    stops = []
    for index, attraction in enumerate(day.attractions):
        stops.append(attraction)
        if index == 0:
            stops.extend(meal for meal in day.meals if meal.type == "lunch")
        if index == len(day.attractions) - 1:
            stops.extend(meal for meal in day.meals if meal.type == "dinner")
    return stops


def prepare_meals(day, request, amap):
    if not day.attractions:
        day.meals = []
        return
    selected = []
    for kind, anchor in (("lunch", day.attractions[0]), ("dinner", day.attractions[-1])):
        existing = next((meal for meal in day.meals if meal.type == kind and meal.location
                         and haversine_meters(meal.location, anchor.location) <= 1500), None)
        if existing:
            selected.append(existing)
            continue
        candidates = []
        # No provider key means map fallback fixtures are not restaurant evidence.
        if getattr(getattr(amap, "settings", None), "amap_api_key", ""):
            try:
                candidates = amap.search_poi(f"{anchor.name} 餐饮", request.city)
            except Exception:
                pass
        eligible = [poi for poi in candidates if poi.id and not poi.id.startswith("fallback-") and venue_kind(poi.name, poi.type) == "restaurant"
                    and haversine_meters(poi.location, anchor.location) <= 1500
                    and poi.id not in {meal.poi_id for meal in selected}]
        eligible.sort(key=lambda poi: (haversine_meters(poi.location, anchor.location), poi.name))
        poi = eligible[0] if eligible else None
        selected.append(Meal(type=kind, name=poi.name if poi else f"{anchor.name}附近用餐（餐厅待确认）",
                             address=poi.address if poi else None, location=poi.location if poi else None,
                             poi_id=poi.id if poi else "", source="map_poi" if poi else "unconfirmed",
                             description="餐厅位置来自地图；营业、菜单和饮食限制需确认。" + "、".join(request.dietary_restrictions),
                             estimated_cost=100 if "美食" in request.preferences else 70))
    day.meals = selected
