"""Validated state contracts for the LangGraph trip-planning workflow."""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field


class TravelIntent(BaseModel):
    """Normalized intent. Explicit request fields are never overwritten."""

    city: str
    start_date: str
    end_date: str
    travel_days: int = Field(ge=1, le=30)
    transportation: str
    accommodation: str
    preferences: list[str] = Field(default_factory=list)
    must_visit: list[str] = Field(default_factory=list)
    budget_limit: int | None = Field(default=None, ge=0)
    pace: Literal["relaxed", "balanced", "packed"] = "balanced"
    travelers: list[str] = Field(default_factory=list)
    energy_preference: Literal["low", "normal", "high"] | None = None
    daily_start_time: str = "09:00"
    daily_end_time: str = "20:00"
    hard_constraints: list[str] = Field(default_factory=list)
    soft_preferences: list[str] = Field(default_factory=list)
    free_text: str = ""
    intent_source: Literal["form", "form_with_free_text", "fallback"] = "form"


class PlanningDraft(BaseModel):
    """A bounded planning brief; it is advisory and never changes hard rules."""

    summary: str = ""
    source: Literal["context_only", "llm", "fallback"] = "context_only"
    suggested_themes: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)


class FreeTextIntentSupplement(BaseModel):
    """Fields an LLM is allowed to infer from unstructured user text."""

    travelers: list[str] = Field(default_factory=list)
    energy_preference: Literal["low", "normal", "high"] | None = None
    daily_start_time: str | None = None
    daily_end_time: str | None = None
    hard_constraints: list[str] = Field(default_factory=list)
    soft_preferences: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    must_visit: list[str] = Field(default_factory=list)


class WorkflowError(BaseModel):
    node: str
    message: str


class WorkflowTrace(BaseModel):
    node: str
    event: str
    detail: str = ""


class ParseIntentInput(BaseModel):
    raw_request: dict[str, Any]


class SpecialistNodeInput(BaseModel):
    request: dict[str, Any]
    intent: TravelIntent


class PlanningState(TypedDict, total=False):
    """Serializable graph state. Pydantic payloads are stored as JSON dicts."""

    raw_request: dict[str, Any]
    run_id: str
    request: dict[str, Any]
    intent: dict[str, Any]
    attraction_result: dict[str, Any]
    weather_result: dict[str, Any]
    hotel_result: dict[str, Any]
    rag_results: list[dict[str, Any]]
    llm_draft: dict[str, Any]
    deterministic_plan: dict[str, Any]
    final_plan: dict[str, Any]
    repair_count: int
    errors: Annotated[list[dict[str, Any]], operator.add]
    degraded_services: Annotated[list[str], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]
