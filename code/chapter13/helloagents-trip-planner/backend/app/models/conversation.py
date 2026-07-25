"""Typed contracts for persisted trip sessions and conversational replanning."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .schemas import TripPlan, TripRequest


class TripRequestPatch(BaseModel):
    """A deliberately narrow set of changes supported by the first chat release."""

    budget_limit: Optional[int] = Field(default=None, ge=0)
    pace: Optional[Literal["relaxed", "balanced", "packed"]] = None
    add_must_visit: List[str] = Field(default_factory=list)
    remove_must_visit: List[str] = Field(default_factory=list)

    @field_validator("pace", mode="before")
    @classmethod
    def normalize_pace(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return {
            "轻松": "relaxed",
            "休闲": "relaxed",
            "均衡": "balanced",
            "适中": "balanced",
            "紧凑": "packed",
        }.get(value.strip().lower(), value.strip().lower())

    def is_empty(self) -> bool:
        return (
            self.budget_limit is None
            and self.pace is None
            and not self.add_must_visit
            and not self.remove_must_visit
        )


class ChatMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1000)


class ConversationMessage(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    created_at: str


class PlanVersionSummary(BaseModel):
    version: int
    change_summary: str
    created_at: str


class TripSessionSummary(BaseModel):
    id: str
    title: str
    city: str
    current_version: int
    created_at: str
    updated_at: str


class TripSessionDetail(TripSessionSummary):
    request: TripRequest
    plan: TripPlan
    messages: List[ConversationMessage] = Field(default_factory=list)
    versions: List[PlanVersionSummary] = Field(default_factory=list)


class TripSessionResponse(BaseModel):
    success: bool = True
    message: str = ""
    data: TripSessionDetail


class TripSessionListResponse(BaseModel):
    success: bool = True
    data: List[TripSessionSummary] = Field(default_factory=list)


class ChatMessageResponse(BaseModel):
    success: bool = True
    assistant_message: str
    applied_patch: TripRequestPatch
    data: TripSessionDetail
