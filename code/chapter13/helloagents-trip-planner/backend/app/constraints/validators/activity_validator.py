from __future__ import annotations

from .base import ConstraintValidator
from ..schema import Constraint, ConstraintType, ValidationViolation
from ...models.schemas import TripPlan


class ActivityValidator(ConstraintValidator):
    supported_types = frozenset(
        {
            ConstraintType.AVOID_ACTIVITY,
            ConstraintType.PREFER_ACTIVITY,
            ConstraintType.DAILY_LOAD,
            ConstraintType.AVOID_CATEGORY,
        }
    )

    def validate(
        self, plan: TripPlan, constraint: Constraint
    ) -> list[ValidationViolation]:
        violations = []
        if constraint.type == ConstraintType.PREFER_ACTIVITY:
            found = any(
                str(constraint.value)
                in f"{item.category} {' '.join(item.categories)}"
                for day in plan.days
                for item in day.attractions
            )
            if not found:
                violations.append(
                    self.violation(
                        constraint,
                        code="PREFERRED_ACTIVITY_MISSING",
                        message=f"行程未包含偏好活动：{constraint.value}",
                        expected=constraint.value,
                        repair_hint="在有余量的日期加入对应类型活动",
                    )
                )
            return violations

        for day in plan.days:
            if constraint.type == ConstraintType.DAILY_LOAD:
                if constraint.value == "low" and (
                    len(day.attractions) > 3
                    or day.daily_visit_minutes > 420
                ):
                    violations.append(
                        self.violation(
                            constraint,
                            code="DAILY_LOAD",
                            day=day.day_index + 1,
                            message=f"Day{day.day_index + 1}活动负荷偏高",
                            actual={
                                "attractions": len(day.attractions),
                                "utilization": day.day_utilization_score,
                            },
                            expected="low",
                            repair_hint="移除低优先级景点并增加休息",
                        )
                    )
                continue
            for item in day.attractions:
                if constraint.type == ConstraintType.AVOID_ACTIVITY:
                    matched = (
                        constraint.value == "high_intensity"
                        and item.intensity_level == "high"
                    )
                else:
                    matched = str(constraint.value) in item.categories
                if matched:
                    violations.append(
                        self.violation(
                            constraint,
                            code="ACTIVITY_LEVEL",
                            day=day.day_index + 1,
                            poi_name=item.name,
                            message=(
                                f"{item.name}不符合活动约束"
                                if constraint.type
                                == ConstraintType.AVOID_ACTIVITY
                                else f"{item.name}属于需避开的类型"
                            ),
                            actual=(
                                item.intensity_level
                                if constraint.type
                                == ConstraintType.AVOID_ACTIVITY
                                else item.categories
                            ),
                            expected=f"avoid {constraint.value}",
                            repair_hint="删除该景点并用同区域低强度景点替换",
                        )
                    )
        return violations
