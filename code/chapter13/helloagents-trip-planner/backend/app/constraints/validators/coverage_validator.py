from __future__ import annotations

from .base import ConstraintValidator
from ..schema import Constraint, ConstraintType, ValidationViolation
from ...models.schemas import TripPlan
from ...services.place_name_service import place_names_match


class CoverageValidator(ConstraintValidator):
    supported_types = frozenset(
        {
            ConstraintType.COVERAGE,
            ConstraintType.MUST_VISIT,
            ConstraintType.ATTRACTION_COUNT,
            ConstraintType.MAX_AREAS,
        }
    )

    def validate(
        self, plan: TripPlan, constraint: Constraint
    ) -> list[ValidationViolation]:
        if constraint.type == ConstraintType.COVERAGE:
            expected = int(constraint.value)
            has_required_coverage = (
                len(plan.days) == expected
                and (
                    expected == 1
                    or all(
                        day.attractions or self._is_short_partial_day(day)
                        for day in plan.days
                    )
                )
            )
            if has_required_coverage:
                return []
            return [
                self.violation(
                    constraint,
                    code="COVERAGE",
                    message=f"需要完整覆盖{expected}天且每天有行程",
                    actual={
                        "days": len(plan.days),
                        "empty_days": [
                            day.day_index + 1
                            for day in plan.days
                            if not day.attractions
                            and not self._is_short_partial_day(day)
                        ],
                    },
                    expected=expected,
                    repair_hint="从未使用候选中补充空白日",
                )
            ]
        if constraint.type == ConstraintType.MUST_VISIT:
            found = any(
                place_names_match(str(constraint.value), item.name)
                for day in plan.days
                for item in day.attractions
            )
            if found:
                return []
            return [
                self.violation(
                    constraint,
                    code="MUST_VISIT",
                    message=f"缺少必去景点：{constraint.value}",
                    expected=constraint.value,
                    repair_hint="优先加入该景点并重新分配日期",
                    poi_name=str(constraint.value),
                )
            ]

        violations = []
        for day in plan.days:
            if constraint.type == ConstraintType.ATTRACTION_COUNT:
                if self._is_short_partial_day(day):
                    continue
                low, high = constraint.value
                count = len(day.attractions)
                remote_solo = (
                    count == 1 and day.daily_travel_minutes > 120
                )
                if not remote_solo and not low <= count <= high:
                    violations.append(
                        self.violation(
                            constraint,
                            code="ATTRACTION_COUNT",
                            day=day.day_index + 1,
                            message=f"Day{day.day_index + 1}安排{count}个景点",
                            actual=count,
                            expected=constraint.value,
                            repair_hint="按剩余时间补点或删除最低优先级景点",
                        )
                    )
            else:
                areas = {item.area for item in day.attractions if item.area}
                if len(areas) > int(constraint.value):
                    violations.append(
                        self.violation(
                            constraint,
                            code="AREA_JUMP",
                            day=day.day_index + 1,
                            message=f"Day{day.day_index + 1}跨越{len(areas)}个区域",
                            actual=sorted(areas),
                            expected=constraint.value,
                            repair_hint="移动或删除最低优先级的跨区景点",
                        )
                    )
        return violations

    @staticmethod
    def _is_short_partial_day(day) -> bool:
        return (
            day.partial_day_reason in {"arrival", "departure"}
            and day.available_minutes is not None
            and day.available_minutes < 180
        )
