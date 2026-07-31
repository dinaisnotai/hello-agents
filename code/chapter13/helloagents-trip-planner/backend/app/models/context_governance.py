"""Typed contracts for role-scoped LLM context, not planning decisions."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ContextRole(str, Enum):
    EXPERIENCE_EVALUATOR = "experience_evaluator"
    REPAIR_STRATEGIST = "repair_strategist"
    FINAL_EXPLAINER = "final_explainer"


class ContextPolicy(BaseModel):
    role: ContextRole
    policy_version: int = 1
    required_sections: list[str]
    optional_sections: list[str] = Field(default_factory=list)
    forbidden_sections: list[str] = Field(default_factory=list)
    max_estimated_tokens: int = Field(default=2200, ge=1)
    max_repair_history_items: int = Field(default=3, ge=0)
    max_candidates: int = Field(default=8, ge=0)
    max_rag_evidence_items: int = Field(default=3, ge=0)
    max_plan_days: int = Field(default=7, ge=1)
    section_priorities: dict[str, int] = Field(default_factory=dict)


class GovernedContext(BaseModel):
    role: ContextRole
    policy_version: int
    payload: dict[str, Any]
    estimated_tokens_before: int = 0
    estimated_tokens: int
    included_sections: list[str] = Field(default_factory=list)
    omitted_sections: list[str] = Field(default_factory=list)
    truncated_sections: list[str] = Field(default_factory=list)
    truncation_reasons: list[str] = Field(default_factory=list)
    artifact_references: list[str] = Field(default_factory=list)
    hard_constraints_present: bool
    plan_version: int
    required_context_over_budget: bool = False


class ContextTrace(BaseModel):
    run_id: str = ""
    node_name: str
    role: ContextRole
    policy_version: int
    plan_version: int
    issue_fingerprint: str = ""
    estimated_tokens_before: int = 0
    estimated_tokens_after: int = 0
    included_sections: list[str] = Field(default_factory=list)
    omitted_sections: list[str] = Field(default_factory=list)
    truncated_sections: list[str] = Field(default_factory=list)
    truncation_reasons: list[str] = Field(default_factory=list)
    artifact_references: list[str] = Field(default_factory=list)
    hard_constraints_present: bool = False
    required_context_over_budget: bool = False
    model: str = ""
    actual_input_tokens: int | None = None
