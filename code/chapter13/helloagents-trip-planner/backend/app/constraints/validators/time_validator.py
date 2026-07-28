from __future__ import annotations

from .base import ConstraintValidator
from ..schema import Constraint, ConstraintType, ValidationViolation
from ...models.schemas import TripPlan


def _minutes(value: str | None) -> int:
    if not value:
        return -1
    day_offset = 24 * 60 if value.startswith("次日") else 0
    value = value.removeprefix("次日")
    try:
        hour, minute = value.split(":", 1)
        return day_offset + int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return -1


class TimeValidator(ConstraintValidator):
    supported_types = frozenset(
        {ConstraintType.DAILY_END_TIME, ConstraintType.TIME_WINDOW}
    )

    def validate(
        self, plan: TripPlan, constraint: Constraint
    ) -> list[ValidationViolation]:
        if constraint.type == ConstraintType.DAILY_END_TIME:
            limit = _minutes(str(constraint.value))
            return [
                self.violation(
                    constraint,
                    code="TIME_LIMIT",
                    day=day.day_index + 1,
                    message=(
                        f"Day{day.day_index + 1}在{day.planned_end_time}结束，"
                        f"超过{constraint.value}"
                    ),
                    actual=day.planned_end_time,
                    expected=constraint.value,
                    repair_hint="移除耗时最高的低优先级景点或缩短交通",
                )
                for day in plan.days
                if _minutes(day.planned_end_time) > limit >= 0
            ]

        start = _minutes(constraint.start)
        end = _minutes(constraint.end)
        required_duration = int(constraint.value or 0)
        violations = []
        for day in plan.days:
            blocks = [
                block
                for block in day.schedule_blocks
                if (
                    not constraint.reason
                    or block.reason == constraint.reason
                    or block.type == constraint.reason
                )
            ]
            valid_block = False
            for block in blocks:
                block_start = _minutes(block.start_time)
                block_end = _minutes(block.end_time)
                if (
                    block_start >= start
                    and block_end <= end
                    and block_end - block_start >= required_duration
                ):
                    valid_block = True
                    break
            if not valid_block:
                violations.append(
                    self.violation(
                        constraint,
                        code="REST_WINDOW",
                        day=day.day_index + 1,
                        message=(
                            f"Day{day.day_index + 1}缺少"
                            f"{constraint.start}-{constraint.end}内的"
                            f"{required_duration}分钟休息"
                        ),
                        actual=[block.model_dump() for block in blocks],
                        expected={
                            "start": constraint.start,
                            "end": constraint.end,
                            "minutes": required_duration,
                        },
                        repair_hint="在指定窗口插入休息块并顺延后续景点",
                    )
                )
        return violations
