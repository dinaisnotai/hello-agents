import unittest
from unittest.mock import MagicMock, patch

from app.config import settings
from app.services.rag_service import (
    EmbeddingError,
    OpenAICompatibleEmbedder,
    TravelGuideRAG,
)


class FakeSemanticEmbedder:
    """A deterministic semantic-model stand-in for vector retrieval tests."""

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        vectors = []
        for text in texts:
            if "地铁附近住得便宜" in text:
                vectors.append([1.0, 1.0, 0.0])
            elif "住宿" in text and "地铁" in text:
                vectors.append([0.9, 1.0, 0.0])
            elif "预算" in text or "经济" in text:
                vectors.append([1.0, 0.7, 0.0])
            else:
                vectors.append([0.2, 0.0, 1.0])
        return vectors


class FailingEmbedder:
    def embed(self, texts):
        raise EmbeddingError("embedding endpoint unavailable")


class TravelGuideRAGTest(unittest.TestCase):
    def setUp(self):
        self.embedder = FakeSemanticEmbedder()
        self.rag = TravelGuideRAG(embedder=self.embedder)

    def test_search_is_limited_to_requested_city(self):
        results = self.rag.search("北京", "雨天室内博物馆", top_k=10)

        self.assertTrue(results)
        self.assertTrue(
            all(item.source == "data/travel_guides/beijing.md" for item in results)
        )

    def test_vector_search_can_match_a_semantic_paraphrase(self):
        results = self.rag.search("上海", "想在地铁附近住得便宜", top_k=3)

        self.assertTrue(results)
        self.assertIn("住宿", results[0].title)
        self.assertGreater(results[0].score, 0.9)

    def test_document_vectors_are_cached_after_first_load(self):
        self.rag.search("杭州", "西湖", top_k=2)
        self.rag.search("杭州", "雨天", top_k=2)

        # First call embeds the whole corpus and its query; second call embeds
        # only its query, proving document vectors are kept in memory.
        self.assertEqual(self.embedder.calls, 3)

    def test_unknown_city_does_not_receive_other_city_evidence(self):
        self.assertEqual(self.rag.search("成都", "预算 景点", top_k=5), [])

    def test_keyword_fallback_returns_city_local_evidence_when_embedding_fails(self):
        rag = TravelGuideRAG(embedder=FailingEmbedder())

        results = rag.search("北京", "故宫 博物馆", top_k=3)

        self.assertTrue(results)
        self.assertTrue(all(item.city == "北京" for item in results))
        self.assertFalse(rag._vector_search_available)

    @patch("app.services.rag_service.httpx.post")
    def test_ark_multimodal_embedder_requests_each_text_separately(self, post):
        response = MagicMock()
        response.json.side_effect = [
            {"data": {"embedding": [1.0, 2.0]}},
            {"data": {"embedding": [3.0, 4.0]}},
        ]
        post.return_value = response

        with (
            patch.object(settings, "embedding_api_key", "test-key"),
            patch.object(settings, "embedding_base_url", "https://ark.example/api/v3"),
            patch.object(settings, "embedding_model", "doubao-embedding-vision-251215"),
        ):
            vectors = OpenAICompatibleEmbedder().embed(["first", "second"])

        self.assertEqual(vectors, [[1.0, 2.0], [3.0, 4.0]])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            post.call_args_list[0].kwargs["json"],
            {
                "model": "doubao-embedding-vision-251215",
                "input": [{"type": "text", "text": "first"}],
            },
        )


if __name__ == "__main__":
    unittest.main()
