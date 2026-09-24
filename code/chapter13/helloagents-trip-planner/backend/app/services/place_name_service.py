"""Helpers for matching attraction aliases with canonical POI names."""

from __future__ import annotations

import re


# Keep aliases explicit: short place names cannot be derived reliably with fuzzy
# string matching. Values are canonical comparison keys, not display names.
_PLACE_ALIASES = {
    "国博": "中国国家博物馆",
    "国家博物馆": "中国国家博物馆",
    "中国国家博物馆": "中国国家博物馆",
    "故宫": "故宫博物院",
    "故宫博物院": "故宫博物院",
    "北京故宫博物院": "故宫博物院",
    "天坛": "天坛公园",
    "景山": "景山公园",
    "王府井": "王府井步行街",
    "上博": "上海博物馆",
    "上海博物馆": "上海博物馆",
    "浙博": "浙江省博物馆",
    "浙江省博物馆": "浙江省博物馆",
}


def normalize_place_name(name: str) -> str:
    """Return a stable comparison key for a user or POI place name."""

    compact = re.sub(r"[\s·•・,，。.!！?？()（）\-—_]+", "", name or "").lower()
    return _PLACE_ALIASES.get(compact, compact)


def landmark_names_match(left: str, right: str) -> bool:
    """Exact known aliases only: a named hospital or branch is not a landmark."""
    return bool(left and right) and normalize_place_name(left) == normalize_place_name(right)


def place_names_match(requested_name: str, poi_name: str) -> bool:
    """Match exact names, full/short variants, and known colloquial aliases."""

    requested = normalize_place_name(requested_name)
    poi = normalize_place_name(poi_name)
    if not requested or not poi:
        return False
    from .venue_policy import venue_kind
    if venue_kind(requested_name) != venue_kind(poi_name):
        return False
    return requested == poi or requested in poi or poi in requested
