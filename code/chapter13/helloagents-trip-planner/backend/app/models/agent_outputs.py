"""Structured contracts exchanged between specialist agents."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from .schemas import Attraction, Hotel, WeatherInfo


class AttractionSearchResult(BaseModel):
    """Validated output from the attraction search specialist."""

    search_keywords: List[str] = Field(default_factory=list)
    attractions: List[Attraction] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    used_fallback: bool = False


class WeatherQueryResult(BaseModel):
    """Validated output from the weather specialist."""

    weather: List[WeatherInfo] = Field(default_factory=list)
    risk_summary: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    used_fallback: bool = False


class HotelSearchResult(BaseModel):
    """Validated output from the hotel search specialist."""

    search_keywords: List[str] = Field(default_factory=list)
    candidates: List[Hotel] = Field(default_factory=list)
    recommended_hotel: Optional[Hotel] = None
    reason: str = ""
    warnings: List[str] = Field(default_factory=list)
    used_fallback: bool = False


class PlannerSoftWarning(BaseModel):
    """One auditable soft warning from the planner LLM."""

    message: str
    basis: Literal["weather", "constraint", "rag"]
    evidence_source: Optional[str] = None


class PlannerReviewResult(BaseModel):
    """Soft review produced by PlannerAgent after deterministic planning."""

    summary: str = ""
    soft_warnings: List[PlannerSoftWarning] = Field(default_factory=list)
    evidence_sources: List[str] = Field(default_factory=list)
