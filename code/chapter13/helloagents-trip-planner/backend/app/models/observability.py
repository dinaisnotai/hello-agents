"""Structured, serializable observability contracts for one planning run."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from .quality import QualityGateTrace


class UserRequirementTrace(BaseModel):
    age_group: str = "unspecified"
    budget: int | None = None
    duration: int
    location: str
    preferences: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    soft_preferences: list[str] = Field(default_factory=list)


class CandidatePOITrace(BaseModel):
    name: str
    visit_key: str = ""
    landmark_tier: str = "complementary_attraction"
    final_selected: bool = False
    category: str = "general"
    location: dict[str, float] = Field(default_factory=dict)
    base_score: float = 0
    preference_score: float = 0
    final_score: float = 0
    rejection_reason: str | None = None
    selection_role: str = "complementary_attraction"
    selection_reason: str = ""
    selection_trace: list[dict[str, Any]] = Field(default_factory=list)
    score_breakdown: dict[str, Any] = Field(default_factory=dict)


class PortfolioMetricsTrace(BaseModel):
    core_landmark_count: int = 0
    major_attraction_count: int = 0
    complementary_attraction_count: int = 0
    niche_attraction_count: int = 0
    niche_ratio: float = 0
    repeated_subcategory_ratio: float = 0
    preference_coverage: float = 0
    landmark_coverage: float = 0
    portfolio_balance_score: float = 0
    displaced_core_major: list[dict[str, Any]] = Field(default_factory=list)


class DailyScheduleTrace(BaseModel):
    day: int
    date: str
    pois: list[str] = Field(default_factory=list)
    planned_start_time: str | None = None
    planned_end_time: str | None = None
    travel_time: int = 0
    walking_distance: float = 0


class RoutePlanningTrace(BaseModel):
    selected_pois: list[str] = Field(default_factory=list)
    travel_time: int = 0
    walking_distance: float = 0
    daily_schedule: list[DailyScheduleTrace] = Field(default_factory=list)
    category_distribution: dict[str, int] = Field(default_factory=dict)


class ValidationTrace(BaseModel):
    constraint_pass: bool
    violations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PlanningRunTrace(BaseModel):
    event: Literal["travel_planning_run"] = "travel_planning_run"
    schema_version: str = "1.0"
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    run_type: str = "planning"
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    user_requirement: UserRequirementTrace
    candidate_pois: list[CandidatePOITrace] = Field(default_factory=list)
    route_planning: RoutePlanningTrace
    validation_result: ValidationTrace
    quality_gate: QualityGateTrace = Field(default_factory=QualityGateTrace)
    portfolio_metrics: PortfolioMetricsTrace = Field(
        default_factory=PortfolioMetricsTrace
    )
