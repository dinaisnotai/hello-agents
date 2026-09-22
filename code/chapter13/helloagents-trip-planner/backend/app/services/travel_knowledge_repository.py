"""Validated, read-only repository for curated travel knowledge."""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from ..models.schemas import Attraction
from ..models.travel_knowledge import (
    KnowledgeClaimType,
    KnowledgeEvidence,
    KnowledgeScopeType,
    KnowledgeSourceType,
    POIKnowledge,
    ScopedPlanningKnowledge,
    TravelKnowledgeCorpus,
)
from ..config import settings
from .place_name_service import normalize_place_name

logger = logging.getLogger("uvicorn.error")


class TravelKnowledgeRepository:
    """Loads small city corpora and exposes defensive indexed lookups."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        root = Path(__file__).resolve().parents[1]
        self.data_dir = Path(data_dir) if data_dir else root / "data" / "travel_knowledge"
        self._loaded = False
        self._corpora: dict[str, TravelKnowledgeCorpus] = {}
        self._evidence: dict[str, KnowledgeEvidence] = {}
        self._pois_by_id: dict[tuple[str, str], POIKnowledge] = {}
        self._pois_by_provider_id: dict[tuple[str, str], POIKnowledge] = {}
        self._pois_by_alias: dict[tuple[str, str], POIKnowledge] = {}
        self._ambiguous_aliases: set[tuple[str, str]] = set()
        self._scoped: list[ScopedPlanningKnowledge] = []
        self._knowledge_ids: set[str] = set()
        self._corpus_hash = ""
        self._source_fingerprint = ""

    @property
    def corpus_hash(self) -> str:
        self._load()
        return self._corpus_hash

    def cities(self) -> list[str]:
        self._load()
        return sorted(self._corpora)

    def resolve_poi(
        self,
        city: str,
        attraction: Attraction | None = None,
        *,
        poi_key: str = "",
        provider_poi_id: str = "",
        name: str = "",
    ) -> Optional[POIKnowledge]:
        self._load()
        city = str(city or "")
        if attraction is not None:
            poi_key = poi_key or attraction.visit_key
            provider_poi_id = provider_poi_id or attraction.poi_id or ""
            name = name or attraction.name
        item = self._pois_by_id.get((city, poi_key)) if poi_key else None
        if item is None and provider_poi_id:
            item = self._pois_by_provider_id.get((city, provider_poi_id))
        if item is None and name:
            item = self._pois_by_alias.get((city, normalize_place_name(name)))
        return deepcopy(item) if item is not None else None

    def query_scope(
        self,
        city: str,
        *,
        scenario_tags: Iterable[str] = (),
        zone: str = "",
        categories: Iterable[str] = (),
        scope_type: KnowledgeScopeType | None = None,
    ) -> list[ScopedPlanningKnowledge]:
        self._load()
        wanted_scenarios = {str(item).lower() for item in scenario_tags if item}
        wanted_categories = {str(item).lower() for item in categories if item}
        result: list[ScopedPlanningKnowledge] = []
        for item in self._scoped:
            if item.city != city:
                continue
            if self._is_expired_hard_fact(item):
                # Stale facts may still be retrieved as explicitly stale
                # evidence, but never influence deterministic planning.
                continue
            if scope_type is not None and item.scope_type != scope_type:
                continue
            if zone and zone not in item.scope_keys:
                continue
            if wanted_scenarios and wanted_scenarios.isdisjoint({x.lower() for x in item.scenario_tags}):
                continue
            if wanted_categories and item.conditions.get("categories"):
                if wanted_categories.isdisjoint({str(x).lower() for x in item.conditions["categories"]}):
                    continue
            result.append(deepcopy(item))
        return result

    def evidence_for(self, evidence_ids: Iterable[str]) -> list[KnowledgeEvidence]:
        self._load()
        return [deepcopy(self._evidence[item]) for item in evidence_ids if item in self._evidence]

    def all_documents(self) -> list[dict]:
        """Return retrieval-ready structured documents with metadata."""
        self._load()
        documents: list[dict] = []
        for corpus in self._corpora.values():
            for poi in corpus.pois:
                documents.append(
                    {
                        "id": poi.knowledge_id,
                        "city": poi.city,
                        "scope_type": KnowledgeScopeType.POI.value,
                        "poi_key": poi.poi_key,
                        "planning_zone": poi.planning_zone,
                        "categories": poi.categories,
                        "scenario_tags": poi.traveler_tags,
                        "environment_type": poi.environment_type.value,
                        "claim_type": KnowledgeClaimType.HEURISTIC.value,
                        "confidence": poi.confidence,
                        "expires_at": None,
                        "title": poi.canonical_name,
                        "text": " ".join(
                            [poi.canonical_name, *poi.aliases, *poi.categories,
                             *poi.experience_tags, *poi.traveler_tags,
                             *poi.best_time_slots, *poi.visit_tips]
                        ),
                        "source": ",".join(poi.evidence_ids),
                    }
                )
            for item in corpus.planning_knowledge:
                documents.append(
                    {
                        "id": item.knowledge_id,
                        "city": item.city,
                        "scope_type": item.scope_type.value,
                        "poi_key": "",
                        "planning_zone": ",".join(item.scope_keys),
                        "categories": item.conditions.get("categories", []),
                        "scenario_tags": item.scenario_tags,
                        "environment_type": "unknown",
                        "claim_type": item.claim_type.value,
                        "confidence": item.confidence,
                        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
                        "title": item.knowledge_id,
                        "text": item.statement,
                        "source": ",".join(item.evidence_ids),
                    }
                )
        return deepcopy(documents)

    def _load(self) -> None:
        fingerprint = self._current_fingerprint()
        if self._loaded and fingerprint == self._source_fingerprint:
            return
        self._reset()
        self._loaded = True
        self._source_fingerprint = fingerprint
        if not self.data_dir.exists():
            return
        raw_files = sorted(self.data_dir.glob("*.json"))
        digest = hashlib.sha256()
        for path in raw_files:
            raw = path.read_bytes()
            digest.update(path.name.encode("utf-8"))
            digest.update(raw)
            try:
                corpus = TravelKnowledgeCorpus.model_validate(json.loads(raw.decode("utf-8")))
                self._index_corpus(corpus)
            except Exception as exc:
                logger.warning("Travel knowledge file ignored: %s (%s)", path, exc)
        self._corpus_hash = digest.hexdigest()[:16]

    def _current_fingerprint(self) -> str:
        if not self.data_dir.exists():
            return "missing"
        entries = []
        for path in sorted(self.data_dir.glob("*.json")):
            try:
                stat = path.stat()
                entries.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
            except OSError:
                continue
        return hashlib.sha256("|".join(entries).encode("utf-8")).hexdigest()

    def _reset(self) -> None:
        self._corpora = {}
        self._evidence = {}
        self._pois_by_id = {}
        self._pois_by_provider_id = {}
        self._pois_by_alias = {}
        self._ambiguous_aliases = set()
        self._scoped = []
        self._knowledge_ids = set()
        self._corpus_hash = ""

    def _index_corpus(self, corpus: TravelKnowledgeCorpus) -> None:
        if corpus.city in self._corpora:
            logger.warning("Duplicate travel knowledge city ignored: %s", corpus.city)
            return
        evidence_by_id: dict[str, KnowledgeEvidence] = {}
        for evidence in corpus.evidence:
            if evidence.source_type == KnowledgeSourceType.LLM_DRAFT:
                logger.warning("LLM draft evidence excluded: %s", evidence.evidence_id)
                continue
            if evidence.evidence_id in self._evidence:
                logger.warning("Duplicate global evidence id isolated: %s", evidence.evidence_id)
                continue
            evidence_by_id[evidence.evidence_id] = evidence
            self._evidence[evidence.evidence_id] = evidence
        for poi in corpus.pois:
            if not poi.city:
                poi = poi.model_copy(update={"city": corpus.city})
            if (
                poi.city != corpus.city
                or poi.knowledge_id in self._knowledge_ids
                or any(item not in evidence_by_id for item in poi.evidence_ids)
            ):
                logger.warning("POI knowledge record isolated: %s", poi.knowledge_id)
                continue
            if not poi.evidence_ids or poi.confidence < 0.6:
                logger.warning("Low-confidence POI knowledge kept for evidence only: %s", poi.knowledge_id)
            self._pois_by_id.setdefault((corpus.city, poi.knowledge_id), poi)
            self._pois_by_id.setdefault((corpus.city, poi.poi_key), poi)
            self._knowledge_ids.add(poi.knowledge_id)
            for provider_id in poi.provider_poi_ids:
                index_key = (corpus.city, provider_id)
                existing_provider = self._pois_by_provider_id.get(index_key)
                if existing_provider is None:
                    self._pois_by_provider_id[index_key] = poi
                elif existing_provider.knowledge_id != poi.knowledge_id:
                    logger.warning("Ambiguous provider POI id isolated: %s", provider_id)
                    self._pois_by_provider_id.pop(index_key, None)
            for alias in [poi.canonical_name, *poi.aliases]:
                alias_key = (corpus.city, normalize_place_name(alias))
                existing = self._pois_by_alias.get(alias_key)
                if existing is None and alias_key not in self._ambiguous_aliases:
                    self._pois_by_alias[alias_key] = poi
                elif existing is not None and existing.knowledge_id != poi.knowledge_id:
                    self._pois_by_alias.pop(alias_key, None)
                    self._ambiguous_aliases.add(alias_key)
                    logger.warning("Ambiguous POI alias requires provider id: %s", alias)
        for item in corpus.planning_knowledge:
            if (
                item.city != corpus.city
                or item.knowledge_id in self._knowledge_ids
                or any(ref not in evidence_by_id for ref in item.evidence_ids)
            ):
                logger.warning("Scoped knowledge record isolated: %s", item.knowledge_id)
                continue
            if item.claim_type == KnowledgeClaimType.HARD_FACT:
                evidence = [evidence_by_id[ref] for ref in item.evidence_ids]
                sources = [source.source_type for source in evidence]
                if not sources or all(source in {KnowledgeSourceType.PUBLIC_GUIDE, KnowledgeSourceType.LLM_DRAFT} for source in sources):
                    logger.warning("Unverified hard fact isolated: %s", item.knowledge_id)
                    continue
                if any(not source.source_uri or source.last_verified_at is None for source in evidence):
                    logger.warning("Hard fact without provenance isolated: %s", item.knowledge_id)
                    continue
            self._scoped.append(item)
            self._knowledge_ids.add(item.knowledge_id)
        self._corpora[corpus.city] = corpus

    @staticmethod
    def _is_expired_hard_fact(item: ScopedPlanningKnowledge) -> bool:
        return bool(
            item.claim_type == KnowledgeClaimType.HARD_FACT
            and item.expires_at is not None
            and item.expires_at <= datetime.now(timezone.utc)
        )


_repository: TravelKnowledgeRepository | None = None


def get_travel_knowledge_repository() -> TravelKnowledgeRepository:
    global _repository
    if _repository is None:
        _repository = TravelKnowledgeRepository(settings.travel_knowledge_data_dir or None)
    return _repository
