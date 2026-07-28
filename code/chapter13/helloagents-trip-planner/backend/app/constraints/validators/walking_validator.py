from __future__ import annotations

from .base import ConstraintValidator
from ..schema import Constraint, ConstraintType, ValidationViolation
from ...models.schemas import TripPlan


class WalkingValidator(ConstraintValidator):
    supported_types = frozenset(
        {ConstraintType.WALKING_DISTANCE, ConstraintType.WALKING_DURATION}
    )

    def validate(
        self, plan: TripPlan, constraint: Constraint
    ) -> list[ValidationViolation]:
        violations = []
        limit = float(constraint.value)
        for day in plan.days:
            if constraint.type == ConstraintType.WALKING_DISTANCE:
                internal = sum(
                    item.estimated_internal_walking_km
                    for item in day.attractions
                )
                actual = day.daily_walking_distance_km + internal
                if actual > limit:
                    violations.append(
                        self.violation(
                            constraint,
                            code="WALKING_LIMIT",
                            day=day.day_index + 1,
                            message=(
                                f"Day{day.day_index + 1}步行约{actual:.1f}km，"
                                f"超过{limit:.1f}km"
                            ),
                            actual=round(actual, 2),
                            expected=limit,
                            repair_hint="替换内部步行较多的景点或改用接驳交通",
                        )
                    )
            else:
                longest = max(
                    (
                        segment.walking_duration_minutes
                        if segment.route_type != "walking"
                        else segment.duration_minutes
                        for segment in day.route_segments
                    ),
                    default=0,
                )
                if longest > limit:
                    violations.append(
                        self.violation(
                            constraint,
                            code="WALKING_DURATION",
                            day=day.day_index + 1,
                            message=(
                                f"Day{day.day_index + 1}存在连续"
                                f"{longest}分钟步行，超过{limit:.0f}分钟"
                            ),
                            actual=longest,
                            expected=limit,
                            repair_hint="将该段改为公交、地铁、出租车或接驳车",
                        )
                    )
        return violations
