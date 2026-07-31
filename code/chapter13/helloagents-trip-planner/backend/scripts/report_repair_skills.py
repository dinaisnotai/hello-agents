"""Summarize persisted repair-skill executions for local review or CI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.repair_skill_metrics import (  # noqa: E402
    RepairSkillExecutionStore,
    RepairSkillMetricsAggregator,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "repair_skill_executions.jsonl")
    parser.add_argument("--issue-type")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    metrics = RepairSkillMetricsAggregator().aggregate(
        RepairSkillExecutionStore(args.input).load(), issue_type=args.issue_type,
    )
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0
    print("Skill\tSelected\tExecuted\tCommit Rate\tRollback Rate\tAvg Quality Δ\tAvg ms")
    for skill, item in metrics.items():
        percent = lambda value: "n/a" if value is None else f"{value * 100:.1f}%"
        delta = "n/a" if item["average_quality_delta"] is None else f"{item['average_quality_delta']:+.3f}"
        latency = "n/a" if item["average_execution_time_ms"] is None else f"{item['average_execution_time_ms']:.1f}"
        print(f"{skill}\t{item['selection_count']}\t{item['execution_count']}\t{percent(item['commit_rate'])}\t{percent(item['rollback_rate'])}\t{delta}\t{latency}")
        if item["rollback_reasons"]:
            print("  rollback reasons:", json.dumps(item["rollback_reasons"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
