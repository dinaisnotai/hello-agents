import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.evaluation.harness import EvaluationHarness
from app.evaluation.schemas import EvaluationCase
from app.models.schemas import Attraction, DayPlan, Location, TripPlan, TripRequest
from app.services.attraction_scorer import AttractionScorer
from app.services.context_governance import RoleContextBuilder
from app.models.context_governance import ContextRole
from app.services.hybrid_knowledge_retriever import HybridKnowledgeRetriever
from app.services.travel_knowledge_repository import TravelKnowledgeRepository
from app.services.travel_knowledge_service import TravelKnowledgeService


def request(city="北京", **updates):
    payload = {
        "city": city,
        "start_date": "2026-10-10",
        "end_date": "2026-10-10",
        "travel_days": 1,
        "transportation": "public transit",
        "accommodation": "budget",
    }
    payload.update(updates)
    return TripRequest(**payload)


def poi(name="故宫", **updates):
    payload = {
        "name": name,
        "location": Location(longitude=116.4, latitude=39.9),
        "score": 60,
        "visit_key": name,
    }
    payload.update(updates)
    return Attraction(**payload)


class _SemanticEmbedder:
    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [
            [1.0, 0.0] if "故宫" in text or "历史" in text else [0.0, 1.0]
            for text in texts
        ]


class TravelKnowledgeRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.repository = TravelKnowledgeRepository()

    def test_loads_three_city_corpora_and_aliases(self):
        self.assertEqual(self.repository.cities(), ["上海", "北京", "杭州"])
        item = self.repository.resolve_poi("北京", name="故宫")
        self.assertIsNotNone(item)
        self.assertEqual(item.canonical_name, "故宫博物院")
        self.assertTrue(self.repository.corpus_hash)

    def test_provider_id_is_an_exact_city_scoped_lookup(self):
        item = self.repository.resolve_poi("北京", provider_poi_id="amap-bj-forbidden-city")
        self.assertIsNotNone(item)
        self.assertEqual(item.poi_key, "bj-forbidden-city")

    def test_expired_hard_fact_is_not_returned_by_planning_scope(self):
        payload = {
            "schema_version": "1.0",
            "city": "北京",
            "evidence": [{
                "evidence_id": "official",
                "source_type": "official_attraction",
                "source_uri": "https://example.test",
                "last_verified_at": "2026-01-01T00:00:00Z",
                "confidence": 0.9,
            }],
            "pois": [],
            "planning_knowledge": [{
                "knowledge_id": "stale",
                "scope_type": "city",
                "city": "北京",
                "claim_type": "hard_fact",
                "statement": "stale",
                "evidence_ids": ["official"],
                "confidence": 0.9,
                "last_updated": "2026-01-01T00:00:00Z",
                "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "beijing.json").write_text(json.dumps(payload), encoding="utf-8")
            repository = TravelKnowledgeRepository(directory)
            self.assertEqual(repository.query_scope("北京"), [])

    def test_invalid_file_fails_open(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "broken.json").write_text("{not json", encoding="utf-8")
            repository = TravelKnowledgeRepository(directory)
            self.assertEqual(repository.cities(), [])
            self.assertEqual(repository.all_documents(), [])

    def test_enrichment_does_not_override_dynamic_provider_facts(self):
        service = TravelKnowledgeService(self.repository)
        attraction = poi("故宫", ticket_price=999, opening_time="10:00", closing_time="12:00")
        enriched = service.enrich_attraction(attraction, request())
        self.assertEqual(enriched.ticket_price, 999)
        self.assertEqual(enriched.opening_time, "10:00")
        self.assertIn("historic", enriched.categories)


class TravelKnowledgeIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.repository = TravelKnowledgeRepository()
        self.service = TravelKnowledgeService(self.repository)

    def test_scorer_uses_bounded_knowledge_delta_only_when_enabled(self):
        candidate = poi("八达岭长城", intensity_level="high")
        senior_request = request(travelers=["elderly"])
        scorer = AttractionScorer(knowledge_service=self.service)
        with patch.object(settings, "enable_travel_knowledge", False):
            disabled = scorer.score_attraction(candidate.model_copy(deep=True), senior_request, anchor=None, existing=(), available_minutes=500)
        with patch.object(settings, "enable_travel_knowledge", True):
            enabled = scorer.score_attraction(candidate.model_copy(deep=True), senior_request, anchor=None, existing=(), available_minutes=500)
        self.assertEqual(disabled.score_breakdown["knowledge_total_delta"], 0)
        self.assertGreaterEqual(enabled.score_breakdown["knowledge_total_delta"], -15)
        self.assertLessEqual(enabled.score_breakdown["knowledge_total_delta"], 15)
        self.assertLess(enabled.score, disabled.score)

    def test_hybrid_retrieval_is_city_scoped_and_exact(self):
        guide_document = {
            "id": "guide-bj",
            "city": "北京",
            "title": "故宫攻略",
            "text": "故宫 历史 文化",
            "source": "guide",
        }
        documents = [guide_document, *self.repository.all_documents()]
        retriever = HybridKnowledgeRetriever(documents, embedder=_SemanticEmbedder())
        results = retriever.search("北京", "故宫历史", top_k=3)
        self.assertTrue(results)
        self.assertTrue(all(item.city == "北京" for item in results))
        self.assertIn("故宫", results[0].title)
        self.assertEqual(retriever.search("成都", "故宫", top_k=3), [])

    def test_hybrid_metadata_filter_is_applied_before_ranking(self):
        documents = [
            {"id": "indoor", "city": "北京", "title": "国博", "text": "历史文化", "categories": ["museum"], "source": "x"},
            {"id": "park", "city": "北京", "title": "景山", "text": "历史文化", "categories": ["park"], "source": "y"},
        ]
        retriever = HybridKnowledgeRetriever(documents)
        results = retriever.search("北京", "历史文化", metadata={"categories": ["museum"]})
        self.assertEqual([item.title for item in results], ["国博"])

    def test_expired_hard_fact_is_penalized_in_retrieval(self):
        documents = [
            {"id": "stale", "city": "北京", "title": "开放状态", "text": "开放状态", "claim_type": "hard_fact", "expires_at": "2020-01-01T00:00:00Z", "source": "stale"},
            {"id": "guide", "city": "北京", "title": "开放说明", "text": "开放状态", "claim_type": "heuristic", "source": "guide"},
        ]
        results = HybridKnowledgeRetriever(documents).search("北京", "开放状态", top_k=2)
        self.assertEqual(results[0].source, "guide")

    def test_hybrid_reuses_hash_matched_embedding_cache(self):
        documents = [{"id": "one", "city": "北京", "title": "故宫", "text": "故宫 历史", "source": "test"}]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory, "vectors.json")
            first_embedder = _SemanticEmbedder()
            HybridKnowledgeRetriever(documents, embedder=first_embedder, cache_path=cache).search("北京", "故宫")
            second_embedder = _SemanticEmbedder()
            HybridKnowledgeRetriever(documents, embedder=second_embedder, cache_path=cache).search("北京", "故宫")
            self.assertEqual(second_embedder.calls, 1)  # query only; document vectors came from cache

    def test_context_governance_accepts_capped_knowledge_summary(self):
        trip = TripPlan(
            city="北京", start_date="2026-10-10", end_date="2026-10-10",
            overall_suggestions="", days=[DayPlan(date="2026-10-10", day_index=0, description="", transportation="public transit", accommodation="budget", attractions=[poi()])],
        )
        governed = RoleContextBuilder().build(
            role=ContextRole.EXPERIENCE_EVALUATOR,
            request=request(), plan=trip,
            knowledge_summary=[{"knowledge_id": str(index)} for index in range(12)],
        )
        self.assertEqual(len(governed.payload["knowledge_summary"]), 8)

    def test_paired_ab_harness_reports_metrics(self):
        case = EvaluationCase(name="baseline", input_request=request())

        def planner(_):
            return TripPlan(city="北京", start_date="2026-10-10", end_date="2026-10-10", overall_suggestions="", days=[DayPlan(date="2026-10-10", day_index=0, description="", transportation="public transit", accommodation="budget", attractions=[poi()])])

        report = EvaluationHarness(planner).run_ab([case], planner)
        self.assertFalse(report.hard_regression)
        self.assertIn("final_experience_score", report.metric_deltas)


if __name__ == "__main__":
    unittest.main()
