"""Constraint presets translate personas into ordinary constraints."""

from __future__ import annotations

from .schema import Constraint, ConstraintType


CONSTRAINT_PRESETS: dict[str, list[Constraint]] = {
    "elderly": [
        Constraint(
            type=ConstraintType.WALKING_DURATION,
            operator="max",
            value=20,
            unit="minutes",
            source="preset:elderly",
        ),
        Constraint(
            type=ConstraintType.AVOID_ACTIVITY,
            operator="avoid",
            value="high_intensity",
            source="preset:elderly",
        ),
        Constraint(
            type=ConstraintType.DAILY_LOAD,
            operator="eq",
            value="low",
            source="preset:elderly",
        ),
    ],
    "child": [
        Constraint(
            type=ConstraintType.PREFER_ACTIVITY,
            operator="prefer",
            value="interactive_activity",
            severity="soft",
            source="preset:child",
        )
    ],
    "wheelchair": [
        Constraint(
            type=ConstraintType.ACCESSIBLE_ROUTE,
            operator="require",
            value=True,
            source="preset:wheelchair",
        )
    ],
}

PRESET_ALIASES = {
    "老人": "elderly",
    "老年": "elderly",
    "senior": "elderly",
    "elderly": "elderly",
    "儿童": "child",
    "孩子": "child",
    "小孩": "child",
    "child": "child",
    "轮椅": "wheelchair",
    "无障碍": "wheelchair",
    "wheelchair": "wheelchair",
}


def resolve_presets(texts: list[str]) -> list[str]:
    resolved = []
    haystack = " ".join(texts).lower()
    for alias, preset in PRESET_ALIASES.items():
        if alias.lower() in haystack and preset not in resolved:
            resolved.append(preset)
    return resolved
