"""Run a small offline evaluation for the constraint-aware planner.

Usage:
    python scripts/evaluate_planner.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.trip_planner_agent import MultiAgentTripPlanner  # noqa: E402
from app.models.schemas import TripRequest  # noqa: E402


def load_cases(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield TripRequest(**json.loads(line))


def main() -> int:
    cases_path = ROOT / "eval_cases.jsonl"
    report_path = ROOT / "eval_report.md"
    planner = MultiAgentTripPlanner()

    results = []
    for case in load_cases(cases_path):
        started = time.perf_counter()
        plan = planner.plan_trip(case)
        latency = time.perf_counter() - started
        results.append(
            {
                "city": case.city,
                "latency": latency,
                "constraint_score": plan.constraint_report.score,
                "constraint_passed": plan.constraint_report.passed,
                "budget_total": plan.budget.total if plan.budget else 0,
                "budget_limit": case.budget_limit,
                "max_daily_distance": max((day.daily_distance_km for day in plan.days), default=0),
                "warnings": len(plan.risk_warnings),
                "evidence": len(plan.evidence_sources),
            }
        )

    pass_rate = sum(1 for item in results if item["constraint_passed"]) / len(results)
    avg_score = statistics.mean(item["constraint_score"] for item in results)
    avg_latency = statistics.mean(item["latency"] for item in results)

    lines = [
        "# Trip Planner Evaluation Report",
        "",
        f"- Cases: {len(results)}",
        f"- Constraint pass rate: {pass_rate:.1%}",
        f"- Average constraint score: {avg_score:.3f}",
        f"- Average latency: {avg_latency:.2f}s",
        "",
        "| City | Score | Passed | Budget | Max daily km | Warnings | Evidence | Latency |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for item in results:
        budget = f"{item['budget_total']}/{item['budget_limit'] or '-'}"
        lines.append(
            f"| {item['city']} | {item['constraint_score']:.3f} | {item['constraint_passed']} | "
            f"{budget} | {item['max_daily_distance']:.1f} | {item['warnings']} | "
            f"{item['evidence']} | {item['latency']:.2f}s |"
        )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
