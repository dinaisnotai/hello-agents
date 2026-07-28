from __future__ import annotations

from .base import ConstraintValidator
from ..schema import Constraint, ConstraintType, ValidationViolation
from ...models.schemas import TripPlan


class BudgetValidator(ConstraintValidator):
    supported_types = frozenset({ConstraintType.BUDGET})

    def validate(
        self, plan: TripPlan, constraint: Constraint
    ) -> list[ValidationViolation]:
        actual = plan.budget.total if plan.budget else 0
        if actual <= float(constraint.value):
            return []
        return [
            self.violation(
                constraint,
                code="BUDGET_LIMIT",
                message=f"预算{actual}元超过{constraint.value}元",
                actual=actual,
                expected=constraint.value,
                repair_hint="降低酒店、门票或餐饮成本",
            )
        ]
