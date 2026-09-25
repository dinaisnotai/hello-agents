"""Small dependency-free hybrid retriever for the curated corpus."""

from __future__ import annotations

import math
import re
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from ..models.schemas import EvidenceSource
from .travel_knowledge_repository import TravelKnowledgeRepository, get_travel_knowledge_repository


class HybridKnowledgeRetriever:
    """Fuse exact/sparse and dense rankings after deterministic metadata filters."""

    def __init__(
        self,
        documents: Sequence[dict[str, Any]],
        embedder: Any | None = None,
        repository: TravelKnowledgeRepository | None = None,
        cache_path: Path | str | None = None,
        model_version: str = "",
    ) -> None:
        self.documents = [dict(item) for item in documents]
        self.embedder = embedder
        self.repository = repository or get_travel_knowledge_repository()
        self.cache_path = Path(cache_path) if cache_path else None
        self.model_version = model_version or str(getattr(embedder, "model", "unknown"))
        self._vectors: list[list[float]] | None = None
        self._dense_available = False
        self._dense_disabled_until = 0.0
        self._corpus_hash = hashlib.sha256(
            json.dumps({"model": self.model_version, "documents": self.documents}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def tokenize(value: str) -> list[str]:
        normalized = str(value or "").lower()
        terms = re.findall(r"[a-z0-9_]{2,}", normalized)
        for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
            terms.append(phrase)
            terms.extend(phrase[index:index + 2] for index in range(len(phrase) - 1))
        return list(dict.fromkeys(terms))

    def search(
        self,
        city: str,
        query: str,
        top_k: int = 5,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> list[EvidenceSource]:
        metadata = metadata or {}
        candidates = [
            (index, doc)
            for index, doc in enumerate(self.documents)
            if self._matches(doc, city, metadata) and not self._stale_penalty(doc)
        ]
        if not candidates or not query.strip() or top_k <= 0:
            return []
        sparse = self._bm25(candidates, query)
        dense = self._dense(candidates, city, query)
        ranks: dict[str, float] = {}
        raw_scores: dict[str, float] = {}
        for rank, (doc_id, score, _) in enumerate(sparse, start=1):
            ranks[doc_id] = ranks.get(doc_id, 0.0) + 1.0 / (60 + rank)
            raw_scores[doc_id] = max(raw_scores.get(doc_id, 0.0), score)
        for rank, (doc_id, score, _) in enumerate(dense, start=1):
            ranks[doc_id] = ranks.get(doc_id, 0.0) + 1.0 / (60 + rank)
            raw_scores[doc_id] = max(raw_scores.get(doc_id, 0.0), score)
        by_id = {doc["id"]: doc for _, doc in candidates}
        ordered = sorted(
            ranks,
            key=lambda doc_id: (
                self._stale_penalty(by_id[doc_id]),
                -ranks[doc_id],
                -raw_scores.get(doc_id, 0.0),
                by_id[doc_id].get("title", ""),
            ),
        )
        query_terms = set(self.tokenize(query))
        output = []
        for doc_id in ordered[:top_k]:
            doc = by_id[doc_id]
            stale_penalty = self._stale_penalty(doc)
            exact_boost = 0.02 if query_terms.intersection(set(self.tokenize(doc.get("title", "")))) else 0.0
            output.append(
                EvidenceSource(
                    title=doc.get("title", doc_id),
                    city=doc.get("city", city),
                    source=doc.get("source") or f"knowledge:{doc_id}",
                    snippet=doc.get("text", "")[:240],
                    score=round(min(1.0, max(0.0, ranks[doc_id] + exact_boost - stale_penalty)), 4),
                )
            )
        return output

    def _matches(self, doc: dict[str, Any], city: str, metadata: dict[str, Any]) -> bool:
        if city and doc.get("city") != city:
            return False
        for key, expected in metadata.items():
            if expected in (None, "", []):
                continue
            actual = doc.get(key, [])
            if not isinstance(actual, list):
                actual = [actual]
            expected_values = expected if isinstance(expected, (list, tuple, set)) else [expected]
            if not set(map(str.lower, map(str, expected_values))).intersection(
                set(map(str.lower, map(str, actual)))
            ):
                return False
        return True

    @staticmethod
    def _stale_penalty(doc: dict[str, Any]) -> float:
        expires_at = doc.get("expires_at")
        if not expires_at or doc.get("claim_type") != "hard_fact":
            return 0.0
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expiry <= datetime.now(timezone.utc):
                return 0.25
        except (TypeError, ValueError):
            return 0.0
        return 0.0

    def _bm25(self, candidates: Sequence[tuple[int, dict[str, Any]]], query: str):
        query_terms = self.tokenize(query)
        if not query_terms:
            return []
        tokenized = [self.tokenize(doc.get("text", "")) for _, doc in candidates]
        document_frequency = Counter(term for terms in tokenized for term in set(terms))
        avg_len = sum(len(item) for item in tokenized) / max(1, len(tokenized))
        scored = []
        for (_, doc), terms in zip(candidates, tokenized):
            counts = Counter(terms)
            score = 0.0
            for term in query_terms:
                if not counts[term]:
                    continue
                idf = math.log(1 + (len(tokenized) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
                score += idf * (counts[term] * 2.2) / (counts[term] + 1.2 * (0.75 + 0.25 * len(terms) / max(1, avg_len)))
            if score > 0:
                scored.append((doc["id"], score, doc))
        return sorted(scored, key=lambda item: (-item[1], item[2].get("title", "")))

    def _dense(self, candidates: Sequence[tuple[int, dict[str, Any]]], city: str, query: str):
        if self.embedder is None:
            return []
        if time.monotonic() < self._dense_disabled_until:
            return []
        try:
            if self._vectors is None:
                self._vectors = self._load_cached_vectors()
                if self._vectors is None:
                    self._vectors = self.embedder.embed(
                        [f"标题：{doc.get('title', '')}\n内容：{doc.get('text', '')}" for doc in self.documents]
                    )
                    self._save_cached_vectors(self._vectors)
                self._dense_available = True
            query_vector = self.embedder.embed([f"城市：{city}\n问题：{query}"])[0]
        except Exception:
            self._dense_available = False
            self._dense_disabled_until = time.monotonic() + 60.0
            return []
        scored = []
        for index, doc in candidates:
            if index >= len(self._vectors):
                continue
            similarity = self._cosine(query_vector, self._vectors[index])
            if similarity > 0:
                scored.append((doc["id"], similarity, doc))
        return sorted(scored, key=lambda item: (-item[1], item[2].get("title", "")))

    def _load_cached_vectors(self) -> list[list[float]] | None:
        if self.cache_path is None or not self.cache_path.exists():
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            vectors = payload.get("vectors")
            if payload.get("corpus_hash") != self._corpus_hash or payload.get("model_version") != self.model_version or not isinstance(vectors, list):
                return None
            if len(vectors) != len(self.documents) or not all(isinstance(item, list) for item in vectors):
                return None
            return [[float(value) for value in item] for item in vectors]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _save_cached_vectors(self, vectors: list[list[float]]) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"corpus_hash": self._corpus_hash, "model_version": self.model_version, "vectors": vectors}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            return

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if not left or len(left) != len(right):
            return 0.0
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
