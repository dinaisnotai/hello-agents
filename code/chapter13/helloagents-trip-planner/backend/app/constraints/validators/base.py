from __future__ import annotations

from abc import ABC, abstractmethod

from ...models.schemas import TripPlan
from ..schema import Constraint, ConstraintType, ValidationViolation


class ConstraintValidator(ABC):
    supported_types: frozenset[ConstraintType] = frozenset()

    def supports(self, constraint: Constraint) -> bool:
        return constraint.type in self.supported_types

    @abstractmethod
    def validate(
        self, plan: TripPlan, constraint: Constraint
    ) -> list[ValidationViolation]:
        raise NotImplementedError

    @staticmethod
    def violation(
        constraint: Constraint,
        *,
        code: str,
        message: str,
        day: int | None = None,
        actual=None,
        expected=None,
        repair_hint: str = "",
        poi_name: str | None = None,
    ) -> ValidationViolation:
        return ValidationViolation(
            type=code,
            constraint_type=constraint.type,
            day=day,
            message=message,
            actual=actual,
            expected=expected,
            severity=constraint.severity,
            repair_hint=repair_hint,
            poi_name=poi_name,
        )
