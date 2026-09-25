"""Meals are independent stops, selected from map records near the route."""
from .venue_policy import venue_kind
from .spatial_planner import haversine_meters
from ..models.schemas import Meal

# Cuisine directions, not endorsements of individual restaurants.
# Beijing source: https://www.visitbeijing.com.cn/article/4N2Q8LkLW5F
LOCAL_CUISINES = {"北京": ("烤鸭", "涮肉", "炸酱面")}
# A default dining window is a product rule, not venue opening-hours data. It
# prevents a short sightseeing day from turning a 15:00 arrival into dinner.
# A user-provided short day may still require an earlier meal.
EARLIEST_DINNER_MINUTES = 17 * 60
_SNACK_OR_DRINK_TYPES = (
    "咖啡", "茶室", "茶饮", "甜品", "饮品", "冰淇淋", "糕饼", "面包",
    "西饼", "饼屋", "零食", "糖果",
)


def is_restaurant_record(name, provider_type):
    """A name containing '饭店' is not restaurant evidence."""
    provider_type = (provider_type or "").lower()
    return (
        any(term in provider_type for term in ("餐饮", "餐厅", "餐馆", "restaurant"))
        and not any(term in provider_type for term in _SNACK_OR_DRINK_TYPES)
        and venue_kind(name, provider_type) == "restaurant"
    )


def schedule_stops(day):
    stops = []
    for index, attraction in enumerate(day.attractions):
        stops.append(attraction)
        if index == 0:
            stops.extend(meal for meal in day.meals if meal.type == "lunch")
        if index == len(day.attractions) - 1:
            stops.extend(meal for meal in day.meals if meal.type == "dinner")
    return stops


def prepare_meals(day, request, amap, *, previous_meal_ids=()):
    if not day.attractions:
        day.meals = []
        return
    selected = []
    specialties = LOCAL_CUISINES.get(request.city, ())
    restrictions = " ".join(request.dietary_restrictions)
    if any(word in restrictions for word in ("素食", "不吃肉", "不吃羊", "不吃鸭")):
        specialties = ()  # Do not let a regional-food preference override dietary restrictions.
    for kind, anchor in (("lunch", day.attractions[0]), ("dinner", day.attractions[-1])):
        anchor_location = anchor.exit_location or anchor.location
        hotel = day.hotel if kind == "dinner" and day.hotel and day.hotel.location else None
        # With only one attraction, dinner belongs near the hotel: the guest
        # can return and rest after lunch instead of making a special 15:00
        # dinner trip from the attraction district.
        return_to_hotel_before_dinner = (
            kind == "dinner" and len(day.attractions) == 1 and hotel is not None
        )
        locations = [hotel.location] if return_to_hotel_before_dinner else [
            anchor_location,
            *([hotel.location] if hotel else []),
        ]
        def suitable(poi):
            text = f"{poi.name} {poi.type}"
            return (is_restaurant_record(poi.name, poi.type)
                    and getattr(poi, "location_source", "provider") == "provider"
                    and not any(word in text for word in ("咖啡", "茶室", "甜品", "酒吧", "饮品", "冰淇淋"))
                    and not (poi.parent_poi_id and poi.parent_poi_id == anchor.poi_id)
                    and not any(word in poi.name for word in (anchor.name, "景运门", "故宫", "博物馆"))
                    and any(haversine_meters(poi.location, loc) <= 1500 for loc in locations))
        # Re-query through the map cache. Old saved meals lack trustworthy type evidence.
        candidates = []
        # No provider key means map fallback fixtures are not restaurant evidence.
        if getattr(getattr(amap, "settings", None), "amap_api_key", ""):
            # The around-search already receives a coordinate. Including an attraction or
            # hotel name in its keyword turns it back into a fuzzy text search and can hide
            # close-by restaurants of the requested cuisine.
            anchors = locations
            desired = specialties[(day.day_index * 2 + (kind == "dinner")) % len(specialties)] if specialties else ""
            food_query = "素食餐厅" if "素食" in restrictions else "中餐厅"
            for place_location in anchors:
                for query in [food_query, *([desired] if desired else [])]:
                    try:
                        nearby = getattr(amap, "search_nearby_poi", None)
                        if callable(nearby):
                            candidates.extend(nearby(query, request.city, place_location, 1500))
                        else:
                            candidates.extend(amap.search_poi(query, request.city))
                    except Exception:
                        continue
        else:
            desired = ""
        eligible = []
        seen_ids = set()
        for poi in candidates:
            if (
                not poi.id or poi.id.startswith("fallback-") or poi.id in seen_ids
                or poi.id in {meal.poi_id for meal in selected} or not suitable(poi)
            ):
                continue
            seen_ids.add(poi.id)
            eligible.append(poi)
        def score(poi):
            detour = haversine_meters(poi.location, anchor_location)
            if hotel:
                detour += haversine_meters(poi.location, hotel.location)
                # Prefer a meal near the hotel when the return-route detour is comparable.
                detour += min(1500, haversine_meters(poi.location, hotel.location)) * 0.3
            cuisine = next((word for word in specialties if word in f"{poi.name} {poi.type}"), "")
            # Modest bounded bonus: local food is preferred, but never at any travel cost.
            bonus = 600 if cuisine else 0
            if cuisine and cuisine == desired:
                bonus += 200
            if cuisine and any(meal.cuisine_hint == cuisine for meal in selected):
                bonus -= 600
            repeated = 2000 if poi.id in previous_meal_ids else 0
            return detour - bonus + repeated, poi.name
        eligible.sort(key=score)
        poi = eligible[0] if eligible else None
        cuisine = next((word for word in specialties if poi and word in f"{poi.name} {poi.type}"), "")
        reason = (f"名称或地图类型匹配本地特色方向「{cuisine}」，结合路线绕行与重复程度选择；菜品需向餐厅核实。"
                  if cuisine else "附近未找到更合适且可核实的特色候选，选择沿线普通正餐；不宣称为当地特色。") if poi else "未查到类型和位置均可靠的正餐候选。"
        fallback_anchor_name = (
            hotel.name if return_to_hotel_before_dinner and hotel else anchor.name
        )
        selected.append(Meal(type=kind, name=poi.name if poi else f"{fallback_anchor_name}附近用餐（餐厅待确认）",
                             address=poi.address if poi else None, location=poi.location if poi else None,
                             poi_id=poi.id if poi else "", source="map_poi" if poi else "unconfirmed",
                             provider_type=poi.type if poi else "", cuisine_hint=cuisine, selection_reason=reason,
                             description=reason + "营业、菜单和饮食限制需确认。" + "、".join(request.dietary_restrictions),
                             estimated_cost=100 if "美食" in request.preferences else 70))
    day.meals = selected
