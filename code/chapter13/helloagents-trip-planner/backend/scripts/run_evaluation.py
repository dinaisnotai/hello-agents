"""Run the data-driven travel-planner evaluation harness.

Usage:
    python scripts/run_evaluation.py
    python scripts/run_evaluation.py --pipeline langgraph --output eval_report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.multi_agent_orchestrator import MultiAgentOrchestrator  # noqa: E402
from app.agents.trip_planner_agent import MultiAgentTripPlanner  # noqa: E402
from app.config import settings  # noqa: E402
from app.evaluation import EvaluationHarness, load_evaluation_cases  # noqa: E402
from app.services.rag_service import EmbeddingError, TravelGuideRAG  # noqa: E402
from app.workflows.langgraph_trip_workflow import LangGraphTripWorkflow  # noqa: E402


class _OfflineEmbedder:
    def embed(self, texts):
        raise EmbeddingError("offline evaluation uses keyword retrieval")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT / "evals" / "travel_scenarios.json",
    )
    parser.add_argument(
        "--pipeline",
        choices=("deterministic", "legacy", "langgraph"),
        default="deterministic",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "eval_report.json",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Run only the named case; repeat to select multiple cases.",
    )
    parser.add_argument(
        "--live-services",
        action="store_true",
        help="Use configured map, embedding, and LLM services instead of deterministic fallbacks.",
    )
    parser.add_argument("--fail-on-regression", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.live_services:
        settings.amap_api_key = ""
        settings.embedding_api_key = ""

    workflow = None
    if args.pipeline == "deterministic":
        plan_builder = MultiAgentTripPlanner()
        if not args.live_services:
            plan_builder.rag = TravelGuideRAG(embedder=_OfflineEmbedder())
        planner = plan_builder.plan_trip
    elif args.pipeline == "legacy":
        orchestrator = MultiAgentOrchestrator(
            enable_llm=args.live_services
        )
        if not args.live_services:
            orchestrator.plan_builder.rag = TravelGuideRAG(
                embedder=_OfflineEmbedder()
            )
        planner = orchestrator.plan_trip
    else:
        orchestrator = MultiAgentOrchestrator(
            enable_llm=args.live_services
        )
        if not args.live_services:
            orchestrator.plan_builder.rag = TravelGuideRAG(
                embedder=_OfflineEmbedder()
            )
        workflow = LangGraphTripWorkflow(orchestrator)
        planner = lambda request: workflow.run(request)[0]

    try:
        cases = load_evaluation_cases(args.dataset)
        if args.case:
            selected = set(args.case)
            cases = [case for case in cases if case.name in selected]
            missing = selected.difference(case.name for case in cases)
            if missing:
                raise ValueError(
                    f"Unknown evaluation case(s): {', '.join(sorted(missing))}"
                )
        report = EvaluationHarness(planner).run(cases)
    finally:
        if workflow is not None:
            workflow.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"{report.passed_cases}/{report.total_cases} cases passed "
        f"({report.pass_rate:.1%}); wrote {args.output}"
    )
    return int(args.fail_on_regression and report.passed_cases < report.total_cases)


if __name__ == "__main__":
    raise SystemExit(main())
