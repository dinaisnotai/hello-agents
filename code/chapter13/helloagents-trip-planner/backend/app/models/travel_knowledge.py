"""Internal, provenance-aware travel knowledge contracts.

These models intentionally do not appear in the public TripPlan/Attraction
schema.  They are an advisory layer used by deterministic planning and the
experience evaluator.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class KnowledgeClaimType(str, Enum):
    HARD_FACT = "hard_fact"
    HEURISTIC = "heuristic"
    SUBJECTIVE = "subjective"


class KnowledgeScopeType(str, Enum):
    POI = "poi"
    CITY = "city"
    PLANNING_ZONE = "planning_zone"
    SCENARIO = "scenario"
    POI_PAIR = "poi_pair"
    PLANNING_PATTERN = "planning_pattern"


class KnowledgeSourceType(str, Enum):
    MAP = "map"
    OFFICIAL_ATTRACTION = "official_attraction"
    OFFICIAL_TOURISM = "official_tourism"
    CURATED_GUIDE = "curated_guide"
    PUBLIC_GUIDE = "public_guide"
    USER_FEEDBACK = "user_feedback"
    LLM_DRAFT = "llm_draft"


class EnvironmentType(str, Enum):
    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class WalkingIntensity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class KnowledgeEffectType(str, Enum):
    EVIDENCE_ONLY = "evidence_only"
    SCORE_ADJUSTMENT = "score_adjustment"
    PAIR_ADJUSTMENT = "pair_adjustment"
    DURATION_HINT = "duration_hint"
    ORDERING_HINT = "ordering_hint"


class KnowledgeEvidence(BaseModel):
    evidence_id: str
    source_type: KnowledgeSourceType
    source_uri: str
    publisher: str = ""
    title: str = ""
    published_at: Optional[datetime] = None
    last_verified_at: datetime
    expires_at: Optional[datetime] = None
    confidence: float = Field(ge=0, le=1)
    license_note: str = ""
    content_hash: str = ""
    reviewed_by: str = ""

    @field_validator("source_type")
    @classmethod
    def reject_llm_production_source(cls, value: KnowledgeSourceType) -> KnowledgeSourceType:
        # LLM drafts may be represented for audit, but the repository refuses
        # to index them as production evidence.
        return value


class POIKnowledge(BaseModel):
    knowledge_id: str
    poi_key: str
    city: str = ""
    canonical_name: str
    aliases: List[str] = Field(default_factory=list)
    provider_poi_ids: List[str] = Field(default_factory=list)
    planning_zone: str = ""
    categories: List[str] = Field(default_factory=list)
    experience_tags: List[str] = Field(default_factory=list)
    recommended_duration_min: Optional[int] = Field(default=None, ge=30)
    recommended_duration_max: Optional[int] = Field(default=None, ge=30)
    environment_type: EnvironmentType = EnvironmentType.UNKNOWN
    walking_intensity: WalkingIntensity = WalkingIntensity.UNKNOWN
    estimated_internal_walking_km: Optional[float] = Field(default=None, ge=0)
    accessibility_features: List[str] = Field(default_factory=list)
    traveler_tags: List[str] = Field(default_factory=list)
    weather_fit: Dict[str, int] = Field(default_factory=dict)
    best_time_slots: List[str] = Field(default_factory=list)
    recommended_combinations: List[str] = Field(default_factory=list)
    avoid_combinations: List[str] = Field(default_factory=list)
    visit_tips: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    last_updated: datetime

    @model_validator(mode="after")
    def validate_soft_ranges(self):
        if (
            self.recommended_duration_max is not None
            and self.recommended_duration_min is not None
            and self.recommended_duration_max < self.recommended_duration_min
        ):
            raise ValueError("recommended_duration_max must be >= minimum")
        if self.recommended_duration_max is not None and self.recommended_duration_max > 720:
            raise ValueError("recommended duration must be <= 720 minutes")
        if any(value < -10 or value > 6 for value in self.weather_fit.values()):
            raise ValueError("weather_fit values must be between -10 and 6")
        return self


class ScopedPlanningKnowledge(BaseModel):
    knowledge_id: str
    scope_type: KnowledgeScopeType
    city: str
    scope_keys: List[str] = Field(default_factory=list)
    scenario_tags: List[str] = Field(default_factory=list)
    claim_type: KnowledgeClaimType
    statement: str
    conditions: Dict[str, Any] = Field(default_factory=dict)
    effect_type: KnowledgeEffectType = KnowledgeEffectType.EVIDENCE_ONLY
    effect_value: Any = None
    evidence_ids: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    last_updated: datetime
    expires_at: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_effect_range(self):
        if isinstance(self.effect_value, (int, float)):
            if self.effect_type == KnowledgeEffectType.SCORE_ADJUSTMENT and not -15 <= self.effect_value <= 15:
                raise ValueError("score adjustment must be within [-15, 15]")
            if self.effect_type == KnowledgeEffectType.PAIR_ADJUSTMENT and not -12 <= self.effect_value <= 4:
                raise ValueError("pair adjustment must be within [-12, 4]")
        return self


class TravelKnowledgeCorpus(BaseModel):
    schema_version: str = "1.0"
    city: str
    evidence: List[KnowledgeEvidence] = Field(default_factory=list)
    pois: List[POIKnowledge] = Field(default_factory=list)
    planning_knowledge: List[ScopedPlanningKnowledge] = Field(default_factory=list)
