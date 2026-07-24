"""Canonicalize city names before they reach map and guide services."""

from __future__ import annotations

import re
import unicodedata


# Keep aliases explicit. Similar-looking names can refer to different cities,
# so fuzzy string matching is deliberately not used for city resolution.
_CITY_ALIASES = {
    "北京": "北京",
    "北京市": "北京",
    "北平": "北京",
    "beijing": "北京",
    "beijingcity": "北京",
    "上海": "上海",
    "上海市": "上海",
    "shanghai": "上海",
    "杭州": "杭州",
    "杭州市": "杭州",
    "hangzhou": "杭州",
    "成都": "成都",
    "成都市": "成都",
    "chengdu": "成都",
}


def normalize_city_name(city: str | None) -> str:
    """Return a supported canonical city name, or a clean unknown input.

    This is intentionally a deterministic alias lookup. Unknown names remain
    unchanged so callers do not silently plan a trip for the wrong city.
    """

    cleaned = unicodedata.normalize("NFKC", city or "").strip()
    key = re.sub(r"[\s,，.。'\"-]+", "", cleaned).casefold()
    return _CITY_ALIASES.get(key, cleaned)
