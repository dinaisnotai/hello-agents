"""Map changing POI names/types into a small, stable category vocabulary."""

from __future__ import annotations

from typing import Dict, Set, Tuple


POI_CATEGORY_RULES: Dict[str, Tuple[str, ...]] = {
    "park": ("公园", "园林", "植物园", "湿地公园"),
    "museum": ("博物馆", "纪念馆", "展览馆", "科技馆", "美术馆"),
    "shopping": ("购物服务", "商场", "购物中心", "商业综合体", "百货", "商业街", "大悦城"),
    "temple": ("寺庙", "寺院", "道观", "宗教场所"),
    "amusement": ("游乐园", "主题乐园", "游乐场"),
    "zoo": ("动物园", "海洋馆", "水族馆"),
    "natural": ("自然风光", "山岳", "森林", "湿地", "湖泊", "海滩"),
    "historic": ("文物古迹", "遗址", "历史建筑", "名胜古迹", "古镇"),
}

POI_CATEGORY_LABELS: Dict[str, str] = {
    "park": "公园园林",
    "museum": "博物馆展馆",
    "shopping": "购物商场",
    "temple": "寺庙宗教场所",
    "amusement": "游乐园",
    "zoo": "动物园和海洋馆",
    "natural": "自然风光",
    "historic": "历史古迹",
}


def classify_poi(name: str, poi_type: str) -> Set[str]:
    """Derive zero or more canonical categories from live or fallback POI text."""

    searchable_text = f"{name or ''} {poi_type or ''}"
    return {
        category
        for category, keywords in POI_CATEGORY_RULES.items()
        if any(keyword in searchable_text for keyword in keywords)
    }
