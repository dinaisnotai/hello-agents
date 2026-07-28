"""Provider-neutral schemas used by every planning constraint."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ConstraintType(str, Enum):
    DAILY_END_TIME = "daily_end_time"
    WALKING_DISTANCE = "walking_distance"
    WALKING_DURATION = "walking_duration"
    TIME_WINDOW = "time_window"
    AVOID_ACTIVITY = "avoid_activity"
    PREFER_ACTIVITY = "prefer_activity"
    DAILY_LOAD = "daily_load"
    ACCESSIBLE_ROUTE = "accessible_route"
    TRANSPORTATION = "transportation"
    BUDGET = "budget"
    COVERAGE = "coverage"
    MUST_VISIT = "must_visit"
    ATTRACTION_COUNT = "attraction_count"
    MAX_AREAS = "max_areas"
    AVOID_CATEGORY = "avoid_category"


class Constraint(BaseModel):
    """One normalized rule; downstream code never needs traveler identity."""

    type: ConstraintType
    operator: Literal[
        "max", "min", "eq", "require", "avoid", "prefer", "between"
    ] = "require"
    value: Any = None
    unit: str = ""
    start: str | None = None
    end: str | None = None
    reason: str = ""
    severity: Literal["hard", "soft"] = "hard"
    source: str = "request"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> "Constraint":
        if self.type == ConstraintType.TIME_WINDOW and (
            not self.start or not self.end
        ):
            raise ValueError("time_window requires start and end")
        return self

    @property
    def stable_key(self) -> tuple:
        value = (
            tuple(self.value)
            if isinstance(self.value, list)
            else str(self.value)
        )
        return (
            self.type.value,
            self.operator,
            value,
            self.unit,
            self.start,
            self.end,
            self.reason,
        )


class ConstraintSet(BaseModel):
    constraints: list[Constraint] = Field(default_factory=list)
    applied_presets: list[str] = Field(default_factory=list)

    def get(self, constraint_type: ConstraintType) -> list[Constraint]:
        return [
            constraint
            for constraint in self.constraints
            if constraint.type == constraint_type
        ]

    def first(self, constraint_type: ConstraintType) -> Constraint | None:
        return next(iter(self.get(constraint_type)), None)


class ValidationViolation(BaseModel):
    type: str
    constraint_type: ConstraintType
    day: int | None = None
    message: str
    actual: Any = None
    expected: Any = None
    severity: Literal["hard", "soft"] = "hard"
    repair_hint: str = ""
    poi_name: str | None = None


class ValidationResult(BaseModel):
    valid: bool = True
    violations: list[ValidationViolation] = Field(default_factory=list)
    checked_constraints: int = 0
    score: float = Field(default=1.0, ge=0, le=1)

    @classmethod
    def from_violations(
        cls,
        violations: list[ValidationViolation],
        checked_constraints: int,
    ) -> "ValidationResult":
        hard = [item for item in violations if item.severity == "hard"]
        penalty = sum(
            1.0 if item.severity == "hard" else 0.35
            for item in violations
        )
        score = max(
            0.0,
            1.0 - penalty / max(1, checked_constraints),
        )
        return cls(
            valid=not hard,
            violations=violations,
            checked_constraints=checked_constraints,
            score=round(score, 3),
        )
