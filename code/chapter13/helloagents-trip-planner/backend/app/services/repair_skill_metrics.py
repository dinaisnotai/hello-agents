"""JSONL persistence and deterministic aggregation for repair-skill events."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Iterable

from ..models.repair_skills import RepairSkillExecution


class RepairSkillExecutionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, execution: RepairSkillExecution) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(execution.model_dump_json() + "\n")

    def load(self) -> list[RepairSkillExecution]:
        if not self.path.exists():
            return []
        result = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                result.append(RepairSkillExecution.model_validate_json(line))
        return result


class RepairSkillMetricsAggregator:
    def aggregate(self, executions: Iterable[RepairSkillExecution], *, issue_type: str | None = None) -> dict[str, dict]:
        groups: dict[str, list[RepairSkillExecution]] = defaultdict(list)
        for item in executions:
            if issue_type is None or item.issue_type == issue_type:
                groups[item.skill_id].append(item)
        return {skill: self._one(items) for skill, items in sorted(groups.items())}

    @staticmethod
    def _one(items: list[RepairSkillExecution]) -> dict:
        count = len(items)
        quality = [item.quality_delta for item in items if item.quality_delta is not None]
        rollback = Counter(item.rollback_reason or "UNKNOWN" for item in items if not item.committed)
        return {
            "selection_count": sum(item.selected for item in items),
            "execution_count": count,
            "mutation_success_count": sum(item.mutation_succeeded for item in items),
            "commit_count": sum(item.committed for item in items),
            "rollback_count": sum(not item.committed and not item.error for item in items),
            "failure_count": sum(bool(item.error) for item in items),
            "mutation_success_rate": sum(item.mutation_succeeded for item in items) / count if count else None,
            "commit_rate": sum(item.committed for item in items) / count if count else None,
            "rollback_rate": sum(not item.committed and not item.error for item in items) / count if count else None,
            "average_quality_delta": sum(quality) / len(quality) if quality else None,
            "median_quality_delta": median(quality) if quality else None,
            "average_hard_violation_delta": sum(item.hard_violations_after - item.hard_violations_before for item in items) / count if count else None,
            "average_warning_delta": sum(item.warning_count_after - item.warning_count_before for item in items) / count if count else None,
            "average_execution_time_ms": sum(item.execution_time_ms for item in items) / count if count else None,
            "rollback_reasons": dict(rollback),
            "issue_type_breakdown": dict(Counter(item.issue_type for item in items)),
            "scope_breakdown": dict(
                Counter(scope for item in items for scope in item.target_scope)
            ),
            "pipeline_mode_breakdown": dict(Counter(item.pipeline_mode for item in items)),
        }
