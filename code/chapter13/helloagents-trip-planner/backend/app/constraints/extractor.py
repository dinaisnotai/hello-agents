"""Convert forms, free text and presets into a normalized ConstraintSet."""

from __future__ import annotations

import re

from ..models.schemas import TripRequest
from .presets import CONSTRAINT_PRESETS, resolve_presets
from .schema import Constraint, ConstraintSet, ConstraintType


class ConstraintExtractor:
    def extract(self, request: TripRequest) -> ConstraintSet:
        constraints: list[Constraint] = []
        texts = [
            *request.travelers,
            *request.hard_constraints,
            *request.soft_preferences,
            request.free_text_input or "",
        ]
        presets = resolve_presets(texts)
        for preset in presets:
            constraints.extend(
                item.model_copy(deep=True)
                for item in CONSTRAINT_PRESETS[preset]
            )

        if request.daily_end_time:
            constraints.append(
                Constraint(
                    type=ConstraintType.DAILY_END_TIME,
                    operator="max",
                    value=request.daily_end_time,
                    unit="HH:MM",
                )
            )
        if request.max_daily_walk_km is not None:
            constraints.append(
                Constraint(
                    type=ConstraintType.WALKING_DISTANCE,
                    operator="max",
                    value=request.max_daily_walk_km,
                    unit="km",
                )
            )
        if request.budget_limit is not None:
            constraints.append(
                Constraint(
                    type=ConstraintType.BUDGET,
                    operator="max",
                    value=request.budget_limit,
                    unit="CNY",
                )
            )
        constraints.extend(
            Constraint(
                type=ConstraintType.MUST_VISIT,
                operator="require",
                value=name,
            )
            for name in request.must_visit
        )
        constraints.extend(
            Constraint(
                type=ConstraintType.AVOID_CATEGORY,
                operator="avoid",
                value=category,
            )
            for category in request.avoid_categories
        )
        constraints.extend(
            [
                Constraint(
                    type=ConstraintType.COVERAGE,
                    operator="eq",
                    value=request.travel_days,
                    unit="days",
                ),
                Constraint(
                    type=ConstraintType.TRANSPORTATION,
                    operator="require",
                    value=request.transportation,
                ),
                Constraint(
                    type=ConstraintType.ATTRACTION_COUNT,
                    operator="between",
                    value=[2, 5],
                    unit="pois/day",
                    severity="soft",
                ),
                Constraint(
                    type=ConstraintType.MAX_AREAS,
                    operator="max",
                    value=2,
                    unit="areas/day",
                    severity="soft",
                ),
            ]
        )

        free_text = " ".join(texts)
        end_time_match = re.search(
            r"(?:每天|每日)?.{0,4}?(\d{1,2})(?::(\d{2}))?\s*(?:点)?前",
            free_text,
        )
        if end_time_match and not request.daily_end_time:
            hour = int(end_time_match.group(1))
            minute = int(end_time_match.group(2) or 0)
            constraints.append(
                Constraint(
                    type=ConstraintType.DAILY_END_TIME,
                    operator="max",
                    value=f"{hour:02d}:{minute:02d}",
                    unit="HH:MM",
                    source="free_text",
                )
            )
        if any(
            phrase in free_text
            for phrase in ("中午需要休息", "中午休息", "需要午休", "午休")
        ):
            constraints.append(
                Constraint(
                    type=ConstraintType.TIME_WINDOW,
                    operator="require",
                    value=60,
                    unit="minutes",
                    start="12:00",
                    end="14:00",
                    reason="rest",
                    source="free_text",
                )
            )
        walking_match = re.search(
            r"(?:步行|走路).{0,8}(?:不超过|最多|控制在)\s*(\d+)\s*分钟",
            free_text,
        )
        if walking_match:
            constraints.append(
                Constraint(
                    type=ConstraintType.WALKING_DURATION,
                    operator="max",
                    value=int(walking_match.group(1)),
                    unit="minutes",
                    source="free_text",
                )
            )
        elif any(
            phrase in free_text
            for phrase in (
                "不能长时间步行",
                "不要长时间步行",
                "不能走太久",
                "少走路",
            )
        ):
            constraints.append(
                Constraint(
                    type=ConstraintType.WALKING_DURATION,
                    operator="max",
                    value=20,
                    unit="minutes",
                    source="free_text",
                )
            )
        distance_match = re.search(
            r"(?:步行|走路).{0,8}(?:不超过|最多|控制在)\s*(\d+(?:\.\d+)?)\s*(?:km|公里)",
            free_text,
            re.IGNORECASE,
        )
        if distance_match and request.max_daily_walk_km is None:
            constraints.append(
                Constraint(
                    type=ConstraintType.WALKING_DISTANCE,
                    operator="max",
                    value=float(distance_match.group(1)),
                    unit="km",
                    source="free_text",
                )
            )

        unique: dict[tuple, Constraint] = {}
        for constraint in constraints:
            existing = unique.get(constraint.stable_key)
            if existing is None or (
                existing.severity == "soft" and constraint.severity == "hard"
            ):
                unique[constraint.stable_key] = constraint
        return ConstraintSet(
            constraints=list(unique.values()),
            applied_presets=presets,
        )
