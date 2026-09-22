"""Run the same frozen evaluation cases with Travel Knowledge off and on."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.trip_planner_agent import MultiAgentTripPlanner  # noqa: E402
from app.config import settings  # noqa: E402
from app.evaluation import EvaluationHarness, load_evaluation_cases  # noqa: E402
from app.services.rag_service import EmbeddingError, TravelGuideRAG  # noqa: E402


class OfflineEmbedder:
    def embed(self, texts):
        raise EmbeddingError("offline A/B uses sparse retrieval")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--dataset", type=Path, default=ROOT / "evals" / "travel_knowledge_scenarios.json")
    result.add_argument("--output", type=Path, default=ROOT / "eval_knowledge_ab_report.json")
    result.add_argument("--retrieval", choices=("legacy", "hybrid"), default="hybrid")
    return result


def make_planner(*, enabled: bool, retrieval: str):
    def planner(request):
        previous_enabled = settings.enable_travel_knowledge
        previous_mode = settings.travel_knowledge_retrieval_mode
        try:
            settings.enable_travel_knowledge = enabled
            settings.travel_knowledge_retrieval_mode = retrieval if enabled else "legacy"
            builder = MultiAgentTripPlanner()
            builder.rag = TravelGuideRAG(embedder=OfflineEmbedder())
            return builder.plan_trip(request)
        finally:
            settings.enable_travel_knowledge = previous_enabled
            settings.travel_knowledge_retrieval_mode = previous_mode
    return planner


def main() -> int:
    args = parser().parse_args()
    settings.amap_api_key = ""
    settings.embedding_api_key = ""
    cases = load_evaluation_cases(args.dataset)
    baseline = make_planner(enabled=False, retrieval="legacy")
    enabled = make_planner(enabled=True, retrieval=args.retrieval)
    report = EvaluationHarness(baseline).run_ab(cases, enabled)
    args.output.write_text(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"baseline={report.baseline.pass_rate:.1%} enabled={report.enabled.pass_rate:.1%} hard_regression={report.hard_regression}")
    return int(report.hard_regression)


if __name__ == "__main__":
    raise SystemExit(main())
