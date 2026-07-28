from __future__ import annotations

from .base import ConstraintValidator
from ..schema import Constraint, ConstraintType, ValidationViolation
from ...models.schemas import TripPlan


class TransportValidator(ConstraintValidator):
    supported_types = frozenset(
        {ConstraintType.TRANSPORTATION, ConstraintType.ACCESSIBLE_ROUTE}
    )

    def validate(
        self, plan: TripPlan, constraint: Constraint
    ) -> list[ValidationViolation]:
        violations = []
        if constraint.type == ConstraintType.ACCESSIBLE_ROUTE:
            for day in plan.days:
                for item in day.attractions:
                    if item.accessible is not True:
                        violations.append(
                            self.violation(
                                constraint,
                                code="ACCESSIBILITY",
                                day=day.day_index + 1,
                                poi_name=item.name,
                                message=f"{item.name}缺少无障碍可达确认",
                                actual=item.accessible,
                                expected=True,
                                repair_hint="替换为已确认无障碍的景点与路线",
                            )
                        )
            return violations

        required = str(constraint.value)
        public_required = any(
            term in required for term in ("公共", "公交", "地铁", "transit")
        )
        for day in plan.days:
            invalid = (
                public_required
                and any(
                    segment.route_type == "driving"
                    for segment in day.route_segments
                )
            )
            if invalid:
                violations.append(
                    self.violation(
                        constraint,
                        code="TRANSPORT_MODE",
                        day=day.day_index + 1,
                        message=f"Day{day.day_index + 1}包含不允许的驾车路段",
                        actual=[
                            segment.route_type
                            for segment in day.route_segments
                        ],
                        expected=required,
                        repair_hint="重新计算为步行或公共交通路线",
                    )
                )
        return violations
