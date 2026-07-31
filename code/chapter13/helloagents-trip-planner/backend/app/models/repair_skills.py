"""Static, non-executable metadata for deterministic repair skills."""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from .quality import CommitDecisionCode, RepairStrategy


class RepairSkillDefinition(BaseModel):
    """Describes a handler capability; it never contains executable logic."""

    skill_id: str
    version: int = Field(default=1, ge=1)
    strategy: RepairStrategy
    supported_issue_types: list[str]
    applicable_scopes: list[str]
    description: str
    required_context_fields: list[str] = Field(default_factory=list)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepairSkillExecution(BaseModel):
    """One auditable selection-to-commit lifecycle, without a plan snapshot."""

    execution_id: str = Field(default_factory=lambda: uuid4().hex)
    skill_id: str
    skill_version: int = Field(ge=1)
    run_id: str = ""
    attempt_id: str = ""
    base_plan_version: int = Field(ge=1)
    issue_type: str
    issue_severity: str
    issue_fingerprint: str
    target_scope: list[str] = Field(default_factory=list)
    selected: bool = True
    mutation_succeeded: bool = False
    committed: bool = False
    quality_improved: bool | None = None
    commit_decision: CommitDecisionCode | None = None
    rollback_reason: str = ""
    quality_before: float | None = None
    quality_after: float | None = None
    quality_delta: float | None = None
    hard_violations_before: int = 0
    hard_violations_after: int = 0
    warning_count_before: int = 0
    warning_count_after: int = 0
    execution_time_ms: float = 0
    pipeline_mode: str = "deterministic"
    evaluation_mode: str = "offline"
    error: str = ""

