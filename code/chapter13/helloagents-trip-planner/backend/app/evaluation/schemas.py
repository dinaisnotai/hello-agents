"""Schemas for reproducible travel-planner evaluation cases and reports."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from ..models.schemas import TripRequest


class ConstraintExpectations(BaseModel):
    walking_limit_km: float | None = Field(default=None, ge=0)
    avoid_hiking: bool = False
    end_time: str | None = None
    budget_limit: int | None = Field(default=None, ge=0)
    must_visit: list[str] = Field(default_factory=list)


class QualityExpectations(BaseModel):
    low_transport_time: bool = False
    relaxed_pace: bool = False
    category_diversity: bool = False
    preference_alignment: bool = False
    family_friendly: bool = False
    within_budget: bool = False
    max_average_transport_minutes: int = Field(default=120, ge=0)
    min_categories: int = Field(default=3, ge=1)


class EvaluationCase(BaseModel):
    name: str
    description: str = ""
    input_request: TripRequest
    constraints: ConstraintExpectations = Field(
        default_factory=ConstraintExpectations
    )
    quality_expectations: QualityExpectations = Field(
        default_factory=QualityExpectations
    )


class CriterionResult(BaseModel):
    name: str
    passed: bool
    actual: Any = None
    expected: Any = None
    detail: str = ""
    severity: str = "quality"


class EvaluationResult(BaseModel):
    case_name: str
    passed: bool
    constraint_passed: bool
    quality_passed: bool
    planner_constraint_passed: bool
    criteria: list[CriterionResult] = Field(default_factory=list)
    metrics: dict[str, float | int | bool] = Field(default_factory=dict)
    latency_seconds: float = 0


class EvaluationReport(BaseModel):
    schema_version: str = "1.0"
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    total_cases: int
    passed_cases: int
    pass_rate: float
    results: list[EvaluationResult] = Field(default_factory=list)
