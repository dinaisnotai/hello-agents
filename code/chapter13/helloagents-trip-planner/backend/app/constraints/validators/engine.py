from __future__ import annotations

from ...models.schemas import TripPlan
from ..schema import ConstraintSet, ValidationResult
from .activity_validator import ActivityValidator
from .budget_validator import BudgetValidator
from .coverage_validator import CoverageValidator
from .time_validator import TimeValidator
from .transport_validator import TransportValidator
from .walking_validator import WalkingValidator


class ConstraintValidationEngine:
    def __init__(self) -> None:
        self.validators = [
            TimeValidator(),
            WalkingValidator(),
            TransportValidator(),
            ActivityValidator(),
            BudgetValidator(),
            CoverageValidator(),
        ]

    def validate(
        self, plan: TripPlan, constraint_set: ConstraintSet
    ) -> ValidationResult:
        violations = []
        checked = 0
        for constraint in constraint_set.constraints:
            validator = next(
                (
                    item
                    for item in self.validators
                    if item.supports(constraint)
                ),
                None,
            )
            if validator is None:
                continue
            checked += 1
            violations.extend(validator.validate(plan, constraint))
        return ValidationResult.from_violations(violations, checked)
