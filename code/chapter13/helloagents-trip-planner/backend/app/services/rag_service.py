"""Lightweight local travel-guide retrieval for planner evidence."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List

from ..models.schemas import EvidenceSource


class TravelGuideRAG:
    """A tiny BM25-like retriever over local Markdown guide files."""

    def __init__(self, data_dir: Path | None = None):
        root = Path(__file__).resolve().parents[1]
        self.data_dir = data_dir or root / "data" / "travel_guides"
        self._chunks: List[Dict[str, str]] = []
        self._loaded = False

    def search(self, city: str, query: str, top_k: int = 5) -> List[EvidenceSource]:
        self._load()
        terms = self._tokenize(f"{city} {query}")
        if not terms:
            return []

        scored = []
        for chunk in self._chunks:
            text_terms = self._tokenize(chunk["text"])
            if not text_terms:
                continue
            term_freq = sum(text_terms.count(term) for term in terms)
            city_bonus = 2 if city and city in chunk["text"] else 0
            score = (term_freq + city_bonus) / math.sqrt(len(text_terms))
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            EvidenceSource(
                title=chunk["title"],
                city=city,
                source=chunk["source"],
                snippet=chunk["text"][:240],
                score=round(score, 4),
            )
            for score, chunk in scored[:top_k]
        ]

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.data_dir.exists():
            return

        for path in self.data_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            sections = re.split(r"\n(?=##\s+)", text)
            for index, section in enumerate(sections):
                clean = section.strip()
                if not clean:
                    continue
                title_match = re.search(r"^#+\s+(.+)$", clean, re.MULTILINE)
                title = title_match.group(1).strip() if title_match else path.stem
                self._chunks.append(
                    {
                        "title": title,
                        "source": str(path.relative_to(self.data_dir.parent.parent)),
                        "text": clean,
                        "index": str(index),
                    }
                )

    def _tokenize(self, text: str) -> List[str]:
        words = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{1,2}", text.lower())
        return [word for word in words if word.strip()]


_rag_service = None


def get_travel_guide_rag() -> TravelGuideRAG:
    global _rag_service
    if _rag_service is None:
        _rag_service = TravelGuideRAG()
    return _rag_service
