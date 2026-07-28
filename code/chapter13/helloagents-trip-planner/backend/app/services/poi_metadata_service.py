"""Deterministic POI metadata and traveler preference profiles.

Map providers are good at names and coordinates but commonly omit planning
attributes such as dwell time and first-visit value.  This module keeps those
attributes explicit, explainable, and safe to fall back when a POI is unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..models.schemas import Attraction, POIInfo, TripRequest
from .poi_category_service import classify_poi


@dataclass(frozen=True)
class PreferenceProfile:
    first_visit: bool = False
    prefer_classic: bool = False


@dataclass(frozen=True)
class POIMetadata:
    categories: tuple[str, ...]
    area: str
    popularity: int
    first_visit_priority: int
    duration_minutes: int
    crowd_level: int
    intensity_level: str = "medium"
    estimated_internal_walking_km: float = 0.5
    accessible: bool | None = None


# A curated seed is deliberately small. Unknown/live POIs still receive
# metadata through the transparent rules below.
BEIJING_POI_METADATA: dict[str, POIMetadata] = {
    "故宫": POIMetadata(("historic", "culture"), "东城区", 10, 10, 180, 9, "medium", 1.8, True),
    "天坛": POIMetadata(("historic", "culture", "park"), "东城区", 9, 10, 180, 8, "medium", 1.8, True),
    "八达岭长城": POIMetadata(("historic", "natural"), "延庆区", 10, 10, 300, 9, "high", 4.0, False),
    "慕田峪长城": POIMetadata(("historic", "natural"), "怀柔区", 9, 9, 300, 8, "high", 4.0, False),
    "颐和园": POIMetadata(("historic", "culture", "park"), "海淀区", 10, 10, 180, 9, "medium", 2.5, True),
    "圆明园": POIMetadata(("historic", "park"), "海淀区", 8, 8, 150, 7),
    "北京大学": POIMetadata(("culture", "campus"), "海淀区", 8, 7, 120, 7),
    "国家博物馆": POIMetadata(("historic", "culture", "museum"), "东城区", 9, 9, 180, 9),
    "景山": POIMetadata(("historic", "park"), "东城区", 8, 8, 90, 7),
    "王府井": POIMetadata(("shopping", "food", "culture"), "东城区", 8, 7, 90, 8),
    "什刹海": POIMetadata(("historic", "culture", "lake"), "西城区", 8, 8, 120, 8),
    "南锣鼓巷": POIMetadata(("historic", "food", "shopping"), "东城区", 8, 7, 90, 9),
    "798": POIMetadata(("art", "culture"), "朝阳区", 7, 5, 120, 6),
    "中国电影博物馆": POIMetadata(("museum", "film"), "朝阳区", 6, 3, 150, 5),
    "中国海关博物馆": POIMetadata(("museum", "specialist"), "东城区", 4, 2, 90, 3),
}

_AREA_PATTERN = re.compile(
    r"(东城区|西城区|朝阳区|海淀区|丰台区|石景山区|通州区|昌平区|"
    r"大兴区|顺义区|房山区|门头沟区|怀柔区|延庆区|密云区|平谷区)"
)
_NICHE_TERMS = ("专业", "专题", "行业", "海关", "电影博物馆", "邮政", "税务")

# --- 各城市人工精选的经典景点名（用于判定 is_curated）---
# names 做子字符串匹配，所以写短名即可（如 "故宫" 能匹配 "故宫博物院"）
CITY_CLASSIC_NAMES: dict[str, set[str]] = {
    "北京": {
        "故宫", "天坛", "八达岭长城", "慕田峪长城", "颐和园", "圆明园",
        "北京大学", "国家博物馆", "景山", "王府井", "什刹海", "南锣鼓巷",
        "798", "中国电影博物馆", "中国海关博物馆",
    },
    "上海": {
        "外滩", "上海博物馆", "豫园", "陆家嘴", "武康路", "静安寺",
        "东方明珠", "迪士尼",
    },
    "杭州": {
        "西湖", "灵隐寺", "浙江省博物馆", "河坊街", "西溪湿地", "龙井村",
        "雷峰塔", "钱塘江",
    },
    "成都": {
        "武侯祠", "宽窄巷子", "杜甫草堂", "金沙遗址博物馆", "锦里", "人民公园",
        "大熊猫", "青城山", "都江堰",
    },
    "广州": {
        "广州塔", "白云山", "陈家祠", "沙面", "珠江", "越秀公园",
        "长隆", "中山纪念堂",
    },
    "深圳": {
        "世界之窗", "欢乐谷", "华侨城", "大小梅沙", "深圳湾公园", "锦绣中华",
    },
}

# --- 用户偏好关键词 → POI 分类映射 ---
PREFERENCE_CATEGORY_MAP: dict[str, set[str]] = {
    "历史": {"historic", "temple"},
    "文化": {"culture", "museum", "temple", "historic", "art"},
    "自然": {"natural", "park", "zoo"},
    "美食": {"food", "shopping"},
    "购物": {"shopping"},
    "艺术": {"art", "museum"},
    "亲子": {"amusement", "zoo", "park"},
    "经典": {"historic", "culture"},
    "地标": {"historic", "natural"},
    "必打卡": {"historic", "culture"},
}


def get_city_classic_names(city: str) -> set[str]:
    """返回该城市人工精选的经典景点名，无数据时返回空集。"""
    return CITY_CLASSIC_NAMES.get(city, set())


def preference_matches_categories(
    preferences: list[str], categories: set[str],
) -> bool:
    """用户的偏好关键词是否命中了该 POI 的分类。

    例如：用户选 "历史"，POI 分类是 {"historic", "museum"}
    → "历史" 映射到 {"historic", "temple"}，和 {"historic", "museum"} 有交集 → True
    """
    if not preferences:
        return False
    for pref in preferences:
        expected = PREFERENCE_CATEGORY_MAP.get(pref)
        if expected and (categories & expected):
            return True
    return False


def build_preference_profile(request: TripRequest) -> PreferenceProfile:
    text = " ".join(
        [
            request.free_text_input or "",
            *request.preferences,
            *request.soft_preferences,
        ]
    )
    inferred_first = any(term in text for term in ("第一次", "首次", "初次", "头一回"))
    first_visit = request.first_visit if request.first_visit is not None else inferred_first
    inferred_classic = (
        first_visit
        or not request.preferences
        or any(term in text for term in ("经典", "地标", "必打卡"))
    )
    prefer_classic = (
        request.prefer_classic
        if request.prefer_classic is not None
        else inferred_classic
    )
    return PreferenceProfile(first_visit=first_visit, prefer_classic=prefer_classic)


def metadata_for_poi(
    *,
    city: str,
    name: str,
    poi_type: str = "",
    address: str = "",
    rating: float | None = None,
    provider_categories: Iterable[str] = (),
    provider_area: str = "",
    provider_popularity: int = 5,
    provider_first_visit_priority: int = 5,
    provider_duration_minutes: int | None = None,
    provider_crowd_level: int = 5,
) -> POIMetadata:
    if city == "北京":
        for alias, metadata in sorted(
            BEIJING_POI_METADATA.items(), key=lambda item: -len(item[0])
        ):
            if alias in name:
                return metadata

    text = f"{name} {poi_type}"
    canonical = tuple(sorted(classify_poi(name, poi_type)))
    categories = tuple(dict.fromkeys([*provider_categories, *canonical]))
    area_match = _AREA_PATTERN.search(f"{provider_area} {address}")
    area = provider_area or (area_match.group(1) if area_match else "")
    popularity = provider_popularity
    if rating is not None:
        popularity = max(popularity, min(10, round(float(rating) * 2)))
    first_priority = provider_first_visit_priority
    if any(term in text for term in ("地标", "名胜", "古迹", "遗址")):
        first_priority = max(first_priority, 7)
    if any(term in text for term in _NICHE_TERMS):
        first_priority = min(first_priority, 3)
        popularity = min(popularity, 6)

    duration = provider_duration_minutes or 120
    if any(term in text for term in ("长城", "大型景区", "主题乐园", "动物园")):
        duration = 240
    elif any(term in text for term in ("博物馆", "美术馆", "科技馆", "风景区")):
        duration = 180
    elif any(term in text for term in ("街", "巷", "广场", "观景台")):
        duration = 90
    intensity = (
        "high"
        if any(term in text for term in ("爬山", "登山", "长城", "徒步", "攀岩"))
        else "low"
        if any(term in text for term in ("街", "广场", "室内", "博物馆"))
        else "medium"
    )
    internal_walk = 2.5 if intensity == "high" else 0.6 if intensity == "low" else 1.2
    return POIMetadata(
        categories=categories,
        area=area,
        popularity=max(0, min(10, popularity)),
        first_visit_priority=max(0, min(10, first_priority)),
        duration_minutes=duration,
        crowd_level=max(0, min(10, provider_crowd_level)),
        intensity_level=intensity,
        estimated_internal_walking_km=internal_walk,
    )


def enrich_attraction(
    attraction: Attraction, request: TripRequest, profile: PreferenceProfile
) -> Attraction:
    metadata = metadata_for_poi(
        city=request.city,
        name=attraction.name,
        poi_type=attraction.category or "",
        address=attraction.address,
        rating=attraction.rating,
        provider_categories=attraction.categories,
        provider_area=attraction.area,
        provider_popularity=attraction.popularity,
        provider_first_visit_priority=attraction.first_visit_priority,
        provider_duration_minutes=attraction.visit_duration,
        provider_crowd_level=attraction.crowd_level,
    )
    attraction.categories = list(metadata.categories)
    attraction.tags = list(
        dict.fromkeys([*metadata.categories, *(attraction.category or "").lower().split(";")])
    )
    attraction.coordinates = [
        attraction.location.latitude,
        attraction.location.longitude,
    ]
    attraction.area = metadata.area
    attraction.popularity = metadata.popularity
    attraction.first_visit_priority = metadata.first_visit_priority
    attraction.crowd_level = metadata.crowd_level
    attraction.intensity_level = metadata.intensity_level
    attraction.estimated_internal_walking_km = (
        metadata.estimated_internal_walking_km
    )
    attraction.accessible = metadata.accessible
    attraction.suggested_duration_minutes = metadata.duration_minutes
    attraction.visit_duration = metadata.duration_minutes

    pref_categories = set(attraction.categories)

    # --- 三维打分：基础质量 + 经典加成 + 偏好加分 - 小众扣分 ---
    classic_names = get_city_classic_names(request.city)
    is_curated = any(
        curated_name in attraction.name
        for curated_name in classic_names
    ) and metadata.first_visit_priority >= 7
    is_unknown_museum = (
        "博物馆" in attraction.name and not is_curated
    )
    pref_matched = preference_matches_categories(
        request.preferences, pref_categories
    )

    base_quality = metadata.popularity * 6                        # 0-60
    classic_bonus = 30 if is_curated else 0                      # 0 或 30
    pref_bonus = 10 if pref_matched else 0                       # 0 或 10
    niche_penalty = -40 if is_unknown_museum else 0               # 0 或 -40

    score = base_quality + classic_bonus + pref_bonus + niche_penalty
    score = max(0, min(100, score))

    if any(name in attraction.name or attraction.name in name for name in request.must_visit):
        score = max(score, 100)
    attraction.score = max(0, min(100, score))
    attraction.score_breakdown = {
        "base_quality": base_quality,
        "classic_bonus": classic_bonus,
        "pref_bonus": pref_bonus,
        "niche_penalty": niche_penalty,
        "preference_match": 10.0 if pref_matched else 0.0,  # 给 spatial_planner 兼容
        "is_curated": is_curated,
    }
    if not attraction.recall_sources:
        attraction.recall_sources = ["specialist_agent"]
    return attraction


def enrich_poi(poi: POIInfo, city: str) -> POIInfo:
    metadata = metadata_for_poi(
        city=city,
        name=poi.name,
        poi_type=poi.type,
        address=poi.address,
        rating=poi.rating,
        provider_categories=poi.categories,
        provider_area=poi.area,
        provider_popularity=poi.popularity,
        provider_first_visit_priority=poi.first_visit_priority,
        provider_duration_minutes=poi.duration_minutes,
        provider_crowd_level=poi.crowd_level,
    )
    poi.categories = list(metadata.categories)
    poi.coordinates = [poi.location.latitude, poi.location.longitude]
    poi.area = metadata.area
    poi.popularity = metadata.popularity
    poi.first_visit_priority = metadata.first_visit_priority
    poi.duration_minutes = metadata.duration_minutes
    poi.crowd_level = metadata.crowd_level
    return poi
