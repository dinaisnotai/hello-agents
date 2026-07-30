"""Structured quality-gate and bounded-repair contracts."""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RepairStrategy(str, Enum):
    """The complete set of actions understood by RepairController."""

    ADD_UNUSED_CANDIDATE = "ADD_UNUSED_CANDIDATE"
    ADD_NEARBY_COMPLEMENTARY_POI = "ADD_NEARBY_COMPLEMENTARY_POI"
    SWAP_WITH_INDOOR_CANDIDATE = "SWAP_WITH_INDOOR_CANDIDATE"
    ADD_WEATHER_BACKUP = "ADD_WEATHER_BACKUP"
    RECLUSTER_ROUTE = "RECLUSTER_ROUTE"
    RESELECT_HOTEL = "RESELECT_HOTEL"
    REMOVE_DUPLICATE = "REMOVE_DUPLICATE"
    REPLACE_LOW_VALUE_CATEGORY = "REPLACE_LOW_VALUE_CATEGORY"
    RUN_CONSTRAINT_REPAIR = "RUN_CONSTRAINT_REPAIR"
    REDUCE_COST = "REDUCE_COST"
    ADD_MUST_VISIT = "ADD_MUST_VISIT"
    REMOVE_CLOSED_ATTRACTION = "REMOVE_CLOSED_ATTRACTION"
    RESOLVE_SAFETY_RISK = "RESOLVE_SAFETY_RISK"


class RepairAttemptStatus(str, Enum):
    """Lifecycle of an isolated plan mutation attempt."""

    CREATED = "created"
    APPLIED = "applied"
    VALIDATED = "validated"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class CommitDecisionCode(str, Enum):
    """Stable, explainable outcomes of the candidate commit gate."""

    COMMITTED = "COMMITTED"
    NEW_HARD_VIOLATION = "NEW_HARD_VIOLATION"
    TARGET_ISSUE_NOT_IMPROVED = "TARGET_ISSUE_NOT_IMPROVED"
    QUALITY_REGRESSION = "QUALITY_REGRESSION"
    STALE_BASE_PLAN_VERSION = "STALE_BASE_PLAN_VERSION"
    RECOMPUTE_FAILED = "RECOMPUTE_FAILED"
    MUTATION_FAILED = "MUTATION_FAILED"
    INVALID_CANDIDATE = "INVALID_CANDIDATE"


class PlanVersionMetadata(BaseModel):
    """Lineage metadata for the currently committed itinerary only."""

    version: int = Field(default=1, ge=1)
    parent_version: int | None = Field(default=None, ge=1)
    mutation_source: Literal[
        "initial_planner", "repair_controller", "replan", "resume"
    ] = "initial_planner"
    mutation_action: str = "initial_plan"
    mutation_reason: str = "initial_planner"
    attempt_id: str | None = None
    created_at: str = ""


class PlanDiff(BaseModel):
    """Stable identity based summary of a candidate plan mutation."""

    changed_days: list[int] = Field(default_factory=list)
    added_poi_ids: list[str] = Field(default_factory=list)
    removed_poi_ids: list[str] = Field(default_factory=list)
    moved_poi_ids: list[str] = Field(default_factory=list)
    hotel_changed: bool = False
    route_changed: bool = False
    cost_delta: float = 0
    walking_delta: float = 0
    transport_time_delta: int = 0


class RepairAttempt(BaseModel):
    """Auditable sandbox execution; rejected candidates never become versions."""

    attempt_id: str
    base_plan_version: int = Field(ge=1)
    issue_fingerprint: str
    issue_type: str
    repair_strategy: RepairStrategy
    status: RepairAttemptStatus = RepairAttemptStatus.CREATED
    validation_before: dict[str, Any] = Field(default_factory=dict)
    validation_after: dict[str, Any] = Field(default_factory=dict)
    quality_before: dict[str, Any] = Field(default_factory=dict)
    quality_after: dict[str, Any] = Field(default_factory=dict)
    quality_delta: float = 0
    plan_diff: PlanDiff = Field(default_factory=PlanDiff)
    rollback_reason: str = ""
    error: str = ""
    committed_plan_version: int | None = None


IssueType = Literal[
    "empty_day",
    "underfilled_day",
    "duplicate_visit",
    "constraint_failure",
    "long_transport",
    "low_landmark_coverage",
    "poor_diversity",
    "budget_violation",
    "time_violation",
    "experience_quality",
    "weather_risk",
    "missing_must_visit",
    "attraction_closed",
    "safety_risk",
    "impossible_schedule",
    "preference_alignment",
]
IssueSeverity = Literal["info", "warning", "high", "critical"]
IssueResolution = Literal[
    "open",
    "mitigated",
    "resolved",
    "unresolved_non_blocking",
    "unresolved_blocking",
]

BLOCKING_ISSUE_TYPES = frozenset(
    {
        "constraint_failure",
        "empty_day",
        "duplicate_visit",
        "missing_must_visit",
        "attraction_closed",
        "safety_risk",
        "impossible_schedule",
        "budget_violation",
        "time_violation",
    }
)


class ExperienceIssue(BaseModel):
    issue_type: IssueType
    severity: IssueSeverity
    day: int | None = Field(default=None, ge=1)
    evidence: str
    repair_strategy: RepairStrategy
    affected_visit_keys: list[str] = Field(default_factory=list)
    source: Literal["deterministic", "llm"] = "deterministic"
    resolution_status: IssueResolution = "open"
    fingerprint: str = ""

    @model_validator(mode="after")
    def establish_fingerprint(self):
        stable = "|".join(
            [
                self.issue_type,
                str(self.day or 0),
                ",".join(sorted(set(self.affected_visit_keys))),
                self.repair_strategy.value,
            ]
        )
        self.fingerprint = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]
        return self

    @property
    def identity(self) -> tuple[str, int | None]:
        return self.issue_type, self.day

    @property
    def is_blocking(self) -> bool:
        return self.issue_type in BLOCKING_ISSUE_TYPES


class ExperienceEvaluation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    passed: bool = Field(alias="pass")
    overall_score: float = Field(default=10, ge=0, le=10)
    issues: list[ExperienceIssue] = Field(default_factory=list)
    source: Literal["deterministic", "llm", "merged"] = "deterministic"
    contract_errors: list[str] = Field(default_factory=list)


class PlanChange(BaseModel):
    day: int | None = Field(default=None, ge=1)
    before_pois: list[str] = Field(default_factory=list)
    after_pois: list[str] = Field(default_factory=list)
    before_hotel: str | None = None
    after_hotel: str | None = None
    before_weather_backup: list[str] = Field(default_factory=list)
    after_weather_backup: list[str] = Field(default_factory=list)
    details: str = ""
    changed_fields: list[str] = Field(default_factory=list)


class RepairIteration(BaseModel):
    iteration: int = Field(ge=1)
    trigger_issue: ExperienceIssue
    issue_fingerprint: str = ""
    selected_action: str
    action_result: str = ""
    accepted: bool
    rejection_reason: str = ""
    changes: list[PlanChange] = Field(default_factory=list)
    constraint_pass_before: bool
    constraint_pass_after: bool
    quality_score_before: float = Field(ge=0, le=10)
    quality_score_after: float = Field(ge=0, le=10)
    issues_before: list[ExperienceIssue] = Field(default_factory=list)
    issues_after: list[ExperienceIssue] = Field(default_factory=list)
    mutation_scope: list[str] = Field(default_factory=list)
    actual_modified_fields: list[str] = Field(default_factory=list)
    itinerary_before: list[dict[str, Any]] = Field(default_factory=list)
    itinerary_after: list[dict[str, Any]] = Field(default_factory=list)
    constraint_delta: dict[str, Any] = Field(default_factory=dict)
    issue_delta: dict[str, Any] = Field(default_factory=dict)
    rollback_reason: str = ""

    @model_validator(mode="after")
    def copy_fingerprint(self):
        if not self.issue_fingerprint:
            self.issue_fingerprint = self.trigger_issue.fingerprint
        return self


class QualityGateTrace(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    reviewer_issues: list[dict[str, Any]] = Field(default_factory=list)
    contract_errors: list[str] = Field(default_factory=list)
    repair_iterations: list[dict[str, Any]] = Field(default_factory=list)
    repair_attempts: list[dict[str, Any]] = Field(default_factory=list)
    final_reason: str = ""
