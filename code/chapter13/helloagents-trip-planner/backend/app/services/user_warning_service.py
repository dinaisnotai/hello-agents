"""Curate compact, actionable Chinese warnings for itinerary consumers."""

from __future__ import annotations

from collections.abc import Iterable

from ..models.schemas import Attraction, TripPlan


_RISK_WORDS = ("雨", "雷", "高温", "炎热", "暴晒", "台风", "hot", "rain")
_INTERNAL_MARKERS = (
    "experienceevaluator:", "reviewer", "已合并", "可选改进：",
    "quality gate", "affects outdoor primary visits", "移动距离较长",
    "行程利用率", "远距离景点日", "重规划备注：",
    "约束未完全满足",
)


def curate_user_warnings(plan: TripPlan) -> TripPlan:
    """Replace internal diagnostics with final-itinerary, user-facing notices.

    Diagnostic facts remain in ``planning_trace``, ``review_scores`` and
    quality issues. This field is deliberately for actions a traveller can
    take before or during the trip.
    """

    selected = [
        item for day in plan.days for item in (day.primary_plan or day.attractions)
    ]
    selected_names = {item.name for item in selected}
    warnings: list[str] = []

    # Day-level weather text is generated only from final primary POIs, not
    # evaluator candidate lists that may mention removed places such as 太庙.
    weather_by_date = {item.date: item for item in plan.weather_info}
    for day in plan.days:
        weather = weather_by_date.get(day.date)
        condition = (
            f"{weather.day_weather} {weather.night_weather}".strip()
            if weather else ""
        )
        primary = day.primary_plan or day.attractions
        outdoor = [item for item in primary if not _is_indoor(item)]
        if not condition or not outdoor or not _has_risk(condition):
            continue
        if day.weather_warning:
            warnings.append(day.weather_warning)
        else:
            names = "、".join(item.name for item in outdoor)
            warnings.append(
                f"第{day.day_index + 1}天{condition}，室外行程（{names}）可能受影响；"
                "请携带雨具/防晒用品，并关注实时天气。"
            )

    for warning in plan.risk_warnings:
        text = " ".join(str(warning).split())
        lowered = text.lower()
        if not text or any(marker in lowered for marker in _INTERNAL_MARKERS):
            continue
        # Raw evaluator weather evidence can name stale POIs. The day-level
        # message above is authoritative and already tied to final primary
        # visits, so discard every raw variant.
        if "天气存在降雨" in text:
            continue
        if _looks_like_stale_poi_notice(text, selected_names):
            continue
        if _mostly_english(text) and "来源" not in text:
            continue
        warnings.append(text)

    # Preserve insertion order, cap noise while leaving detailed diagnostics
    # available through the observability trace.
    plan.risk_warnings = list(dict.fromkeys(warnings))[:5]
    return plan


def _is_indoor(item: Attraction) -> bool:
    text = " ".join([item.name, item.category or "", *item.categories]).lower()
    return any(term in text for term in ("museum", "博物馆", "展览", "室内", "剧院", "科技馆"))


def _has_risk(condition: str) -> bool:
    lowered = condition.lower()
    return any(word.lower() in lowered for word in _RISK_WORDS)


def _looks_like_stale_poi_notice(text: str, selected_names: Iterable[str]) -> bool:
    # Evaluator messages have a colon followed by a candidate list. If none
    # of those names is in the final primary itinerary, it is stale evidence.
    if "outdoor primary visits" not in text.lower():
        return False
    tail = text.replace("：", ":").rsplit(":", 1)[-1]
    known = tuple(selected_names)
    return bool(tail.strip()) and not any(name in tail for name in known)


def _mostly_english(text: str) -> bool:
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
    latin = sum(char.isascii() and char.isalpha() for char in text)
    return latin > chinese * 2 and latin >= 8
