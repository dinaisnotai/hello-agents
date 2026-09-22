"""Vector retrieval over local travel-guide Markdown knowledge cards."""

from __future__ import annotations

import math
import os
import re
import logging
from pathlib import Path
from typing import Any, Dict, List, Protocol, Sequence

import httpx

from ..config import settings
from ..models.schemas import EvidenceSource
from .city_name_service import normalize_city_name
from .hybrid_knowledge_retriever import HybridKnowledgeRetriever
from .travel_knowledge_repository import get_travel_knowledge_repository


logger = logging.getLogger("uvicorn.error")


CITY_BY_GUIDE_STEM = {
    "beijing": "北京",
    "hangzhou": "杭州",
    "shanghai": "上海",
}


class EmbeddingError(RuntimeError):
    """Raised when a configured embedding provider cannot return vectors."""


class Embedder(Protocol):
    """Small boundary that lets retrieval use a hosted or test embedding model."""

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        ...


class OpenAICompatibleEmbedder:
    """Calls Volcengine Ark's multimodal embedding endpoint for text vectors."""

    def __init__(self) -> None:
        self.api_key = (
            settings.embedding_api_key
            or os.getenv("EMBEDDING_API_KEY")
            or os.getenv("LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or settings.openai_api_key
        )
        self.base_url = (
            settings.embedding_base_url
            or os.getenv("EMBEDDING_BASE_URL")
            or os.getenv("LLM_BASE_URL")
            or settings.openai_base_url
        ).rstrip("/")
        self.model = settings.embedding_model or os.getenv("EMBEDDING_MODEL")

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        if not self.api_key:
            raise EmbeddingError(
                "Embedding is not configured. Set EMBEDDING_API_KEY (or LLM_API_KEY) "
                "before enabling vector retrieval."
            )

        try:
            # Ark treats ``input`` as content parts of one multimodal sample,
            # not as a batch. Request one sample per RAG text.
            vectors = []
            for text in texts:
                response = httpx.post(
                    f"{self.base_url}/embeddings/multimodal",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    json={
                        "model": self.model,
                        "input": [{"type": "text", "text": text}],
                    },
                    timeout=20.0,
                )
                response.raise_for_status()
                payload = response.json()
                vectors.append(payload["data"]["embedding"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"Embedding request failed: {exc}") from exc

        if len(vectors) != len(texts) or not all(isinstance(vector, list) for vector in vectors):
            raise EmbeddingError("Embedding provider returned an invalid vector payload.")
        return [[float(value) for value in vector] for vector in vectors]


class TravelGuideRAG:
    """In-memory vector retriever with strict city filtering.

    Guide sections are loaded once, embedded once, then queried through cosine
    similarity. The corpus is intentionally small, so exact in-memory vector
    search is simpler and more transparent than adding a separate vector DB.
    """

    def __init__(self, data_dir: Path | None = None, embedder: Embedder | None = None):
        root = Path(__file__).resolve().parents[1]
        self.data_dir = data_dir or root / "data" / "travel_guides"
        self.embedder = embedder or OpenAICompatibleEmbedder()
        self._chunks: List[Dict[str, Any]] = []
        self._loaded = False
        self._vector_search_available = False
        self._hybrid_retriever: HybridKnowledgeRetriever | None = None

    def search(
        self,
        city: str,
        query: str,
        top_k: int = 5,
        *,
        metadata: Dict[str, Any] | None = None,
    ) -> List[EvidenceSource]:
        city = normalize_city_name(city)
        self._load()

        if settings.enable_travel_knowledge and settings.travel_knowledge_retrieval_mode == "hybrid":
            return self._hybrid_search(city, query, top_k, metadata=metadata)

        candidates = [chunk for chunk in self._chunks if not city or chunk["city"] == city]
        if not candidates or not query.strip() or top_k <= 0:
            return []

        if not self._vector_search_available:
            return self._keyword_search(candidates, query, top_k)

        try:
            query_vector = self.embedder.embed([f"城市：{city}\n问题：{query}"])[0]
        except (EmbeddingError, IndexError) as exc:
            logger.warning("RAG query embedding failed; using keyword retrieval: %s", exc)
            self._vector_search_available = False
            return self._keyword_search(candidates, query, top_k)

        scored = []
        for chunk in candidates:
            score = self._cosine_similarity(query_vector, chunk["embedding"])
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            EvidenceSource(
                title=chunk["title"],
                city=chunk["city"],
                source=chunk["source"],
                snippet=chunk["text"][:240],
                score=round(score, 4),
            )
            for score, chunk in scored[:top_k]
        ]

    def _keyword_search(
        self, candidates: Sequence[Dict[str, Any]], query: str, top_k: int
    ) -> List[EvidenceSource]:
        """Dependency-free fallback used when the embedding service is unavailable."""

        terms = self._keyword_terms(query)
        scored = []
        for chunk in candidates:
            searchable = " ".join((chunk["title"], chunk["tags"], chunk["text"])).lower()
            hits = sum(searchable.count(term) for term in terms)
            score = hits / max(1, len(terms)) if terms else 0.0
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda item: (item[0], -int(item[1]["index"])), reverse=True)
        return [
            EvidenceSource(
                title=chunk["title"],
                city=chunk["city"],
                source=chunk["source"],
                snippet=chunk["text"][:240],
                score=round(score, 4),
            )
            for score, chunk in scored[:top_k]
        ]

    def _hybrid_search(
        self,
        city: str,
        query: str,
        top_k: int,
        *,
        metadata: Dict[str, Any] | None = None,
    ) -> List[EvidenceSource]:
        if self._hybrid_retriever is None:
            documents = []
            for chunk in self._chunks:
                documents.append(
                    {
                        "id": f"guide:{chunk['source']}:{chunk['index']}",
                        "title": chunk["title"],
                        "text": f"{chunk['tags']} {chunk['text']}",
                        "city": chunk["city"],
                        "source": chunk["source"],
                    }
                )
            documents.extend(get_travel_knowledge_repository().all_documents())
            self._hybrid_retriever = HybridKnowledgeRetriever(
                documents,
                embedder=self.embedder,
                cache_path=settings.travel_knowledge_embedding_cache_path,
                model_version=settings.embedding_model,
            )
        return self._hybrid_retriever.search(
            city,
            query,
            top_k,
            metadata={"city": city, **(metadata or {})},
        )

    @staticmethod
    def _keyword_terms(query: str) -> List[str]:
        normalized = query.lower()
        latin_terms = re.findall(r"[a-z0-9]{2,}", normalized)
        chinese_terms = re.findall(r"[\u4e00-\u9fff]{2,}", normalized)
        terms = latin_terms[:]
        for phrase in chinese_terms:
            terms.append(phrase)
            terms.extend(phrase[index : index + 2] for index in range(len(phrase) - 1))
        return list(dict.fromkeys(term for term in terms if term))

    def _load(self) -> None:
        if self._loaded:
            return
        if not self.data_dir.exists():
            self._loaded = True
            return

        chunks: List[Dict[str, Any]] = []
        for path in self.data_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            city = CITY_BY_GUIDE_STEM.get(path.stem, path.stem)
            for index, section in enumerate(re.split(r"\n(?=##\s+)", text)):
                clean = section.strip()
                if not clean:
                    continue
                title_match = re.search(r"^#+\s+(.+)$", clean, re.MULTILINE)
                title = title_match.group(1).strip() if title_match else path.stem
                tags = " ".join(re.findall(r"\[([^\]]+)\]", title))
                chunks.append(
                    {
                        "title": title,
                        "source": path.relative_to(self.data_dir.parent.parent).as_posix(),
                        "city": city,
                        "tags": tags,
                        "text": clean,
                        "index": str(index),
                    }
                )

        self._chunks = chunks
        self._loaded = True
        if not chunks:
            return

        try:
            vectors = self.embedder.embed(
                [f"标题：{chunk['title']}\n标签：{chunk['tags']}\n内容：{chunk['text']}" for chunk in chunks]
            )
            if len(vectors) != len(chunks):
                raise EmbeddingError("The number of document vectors does not match the chunks.")
            for chunk, vector in zip(chunks, vectors):
                chunk["embedding"] = vector
            self._vector_search_available = True
        except EmbeddingError as exc:
            # Preserve the parsed corpus, so callers still receive local guide
            # evidence through keyword ranking instead of an empty RAG result.
            logger.warning("RAG document embedding failed; using keyword retrieval: %s", exc)

    @staticmethod
    def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


_rag_service = None


def get_travel_guide_rag() -> TravelGuideRAG:
    global _rag_service
    if _rag_service is None:
        _rag_service = TravelGuideRAG()
    return _rag_service
