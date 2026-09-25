"""Match a supplier hotel to a specific map lodging POI, never an address centroid."""
import re
from .venue_policy import venue_kind


def match_hotel_location(meta, city, amap):
    if not getattr(getattr(amap, "settings", None), "amap_api_key", ""):
        return None
    try:
        pois = amap.search_poi(meta["name"], city)
    except Exception:
        return None
    candidates = [p for p in pois if p.id and not p.id.startswith("fallback-")
                  and p.location_source == "provider"
                  and venue_kind(p.name, p.type) == "hotel"]
    # A same-name exact match is stronger than a keyword hit for a hotel restaurant.
    def compact(value):
        return re.sub(r"[\W_]", "", value.lower())
    exact = [p for p in candidates if compact(p.name) == compact(meta["name"])]
    if len(exact) == 1:
        return exact[0]
    # Cross-language names: require an unambiguous lodging result and matching street number.
    numbers = set(re.findall(r"\d+", meta.get("address", "")))
    matched = [p for p in candidates if numbers and numbers.intersection(re.findall(r"\d+", p.address))]
    return matched[0] if len(matched) == 1 and len(candidates) == 1 else None
