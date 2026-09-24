"""Apply safe conversational patches and persist each resulting plan version."""

from __future__ import annotations

import re
from copy import deepcopy
import json
import logging
from threading import Lock
from typing import Callable, Iterable, Optional

from hello_agents import HelloAgentsLLM, SimpleAgent

from .planner_service import get_planner
from ..agents.agent_utils import parse_agent_result, run_stateless_agent
from ..agents.prompts import CONVERSATION_PATCH_PROMPT
from ..models.conversation import TripRequestPatch, TripSessionDetail
from ..models.schemas import ReplanRequest, TripPlan, TripRequest
from ..repositories.trip_session_repository import TripSessionRepository
from .llm_service import get_llm
from .place_name_service import place_names_match


logger = logging.getLogger("uvicorn.error")


_BUDGET_PATTERN = re.compile(
    r"预算(?:上限)?(?:改成|改为|调整为|设为|设置为|降到|提高到|增加到|控制在|不超过|为)?"
    r"\s*(\d{2,8})\s*(?:元|块)?",
    re.IGNORECASE,
)
_SYMBOLIC_BUDGET_PATTERN = re.compile(
    r"(?:钱|预算|budget)\s*(<|<=|≤|less\s+than|under|below)\s*(\d{2,8})",
    re.IGNORECASE,
)
_ADD_PATTERNS = (
    re.compile(r"(?:加入|加上|添加|新增|想去|必去)\s*([^，。,.；;]+)"),
    re.compile(r"\badd\s*([^，。,.；;]+)", re.IGNORECASE),
)
_REMOVE_PATTERNS = (
    re.compile(r"(?:删除|去掉|移除|取消|不要去|不去)\s*([^，。,.；;]+)"),
    re.compile(r"把\s*([^，。,.；;]+?)\s*(?:删除|去掉|移除|取消)"),
)
_PACE_WORDS = {
    "relaxed": ("轻松", "休闲", "慢一点", "宽松", "relaxed"),
    "balanced": ("均衡", "适中", "正常", "balanced"),
    "packed": ("紧凑", "充实", "多安排", "packed"),
}


def _clean_places(values: Iterable[str]) -> list[str]:
    places: list[str] = []
    for value in values:
        # Do not split on "和/与": they are also valid characters in place names
        # such as "颐和园". Multiple attractions can be separated with "、" or "/".
        for item in re.split(r"[、/]", value):
            cleaned = re.sub(
                r"(?:作为)?(?:必去)?(?:景点)?(?:吧|了|一下|进去|行程)$",
                "",
                item.strip(),
            ).strip()
            if cleaned and cleaned not in places:
                places.append(cleaned)
    return places


def parse_trip_request_patch(content: str) -> TripRequestPatch:
    """Parse the three mutation families supported by the first release."""

    text = content.strip()
    budget_match = _BUDGET_PATTERN.search(text)
    symbolic_budget_match = _SYMBOLIC_BUDGET_PATTERN.search(text)
    budget_limit = int(budget_match.group(1)) if budget_match else None
    if symbolic_budget_match:
        operator, raw_limit = symbolic_budget_match.groups()
        limit = int(raw_limit)
        budget_limit = max(0, limit - 1) if operator == "<" else limit
    pace: Optional[str] = None
    for candidate, words in _PACE_WORDS.items():
        if any(word.lower() in text.lower() for word in words):
            pace = candidate
            break

    additions = [
        match.group(1)
        for pattern in _ADD_PATTERNS
        for match in pattern.finditer(text)
    ]
    removals = [
        match.group(1)
        for pattern in _REMOVE_PATTERNS
        for match in pattern.finditer(text)
    ]
    return TripRequestPatch(
        budget_limit=budget_limit,
        pace=pace,
        add_must_visit=_clean_places(additions),
        remove_must_visit=_clean_places(removals),
    )


class ConversationPatchInterpreter:
    """Use the configured LLM to turn free text into a validated patch."""

    def __init__(self, llm: Optional[HelloAgentsLLM] = None):
        self._llm = llm
        self._agent: Optional[SimpleAgent] = None
        self._lock = Lock()

    def interpret(self, request: TripRequest, content: str) -> TripRequestPatch:
        with self._lock:
            if self._agent is None:
                self._agent = SimpleAgent(
                    name="ConversationPatchAgent",
                    llm=self._llm or get_llm(),
                    system_prompt=CONVERSATION_PATCH_PROMPT,
                    enable_tool_calling=False,
                )
            raw_result = run_stateless_agent(
                self._agent,
                json.dumps(
                    {
                        "current_request": request.model_dump(mode="json"),
                        "user_message": content,
                    },
                    ensure_ascii=False,
                ),
            )
        return parse_agent_result(raw_result, TripRequestPatch)


def _merge_patches(
    llm_patch: TripRequestPatch,
    rule_patch: TripRequestPatch,
) -> TripRequestPatch:
    """Keep exact symbolic constraints while filling LLM omissions safely."""

    return TripRequestPatch(
        budget_limit=(
            rule_patch.budget_limit
            if rule_patch.budget_limit is not None
            else llm_patch.budget_limit
        ),
        pace=rule_patch.pace or llm_patch.pace,
        add_must_visit=list(
            dict.fromkeys([*llm_patch.add_must_visit, *rule_patch.add_must_visit])
        ),
        remove_must_visit=list(
            dict.fromkeys([*llm_patch.remove_must_visit, *rule_patch.remove_must_visit])
        ),
    )


def apply_trip_request_patch(
    request: TripRequest,
    patch: TripRequestPatch,
) -> TripRequest:
    updated = request.model_copy(deep=True)
    if patch.budget_limit is not None:
        updated.budget_limit = patch.budget_limit
    if patch.pace is not None:
        updated.pace = patch.pace

    updated.must_visit = [
        existing
        for existing in updated.must_visit
        if not any(
            place_names_match(to_remove, existing)
            for to_remove in patch.remove_must_visit
        )
    ]
    for addition in patch.add_must_visit:
        if not any(place_names_match(addition, existing) for existing in updated.must_visit):
            updated.must_visit.append(addition)
    return TripRequest.model_validate(updated.model_dump())


def describe_patch(patch: TripRequestPatch) -> str:
    changes: list[str] = []
    if patch.budget_limit is not None:
        changes.append(f"预算上限调整为 {patch.budget_limit} 元")
    if patch.pace is not None:
        pace_label = {"relaxed": "轻松", "balanced": "均衡", "packed": "紧凑"}[patch.pace]
        changes.append(f"行程节奏调整为{pace_label}")
    if patch.add_must_visit:
        changes.append(f"加入必去景点：{'、'.join(patch.add_must_visit)}")
    if patch.remove_must_visit:
        changes.append(f"移除必去景点：{'、'.join(patch.remove_must_visit)}")
    return "；".join(changes)


class TripConversationService:
    def __init__(
        self,
        repository: Optional[TripSessionRepository] = None,
        planner=None,
        patch_interpreter: Optional[
            Callable[[TripRequest, str], TripRequestPatch]
        ] = None,
    ):
        self.repository = repository or TripSessionRepository()
        self.planner = planner or get_planner()
        interpreter = patch_interpreter or ConversationPatchInterpreter().interpret
        self.patch_interpreter = interpreter

    def create_session(self, request: TripRequest) -> TripSessionDetail:
        plan = self.planner.plan_trip(request)
        return self.repository.create(request, plan)

    def save_existing_plan(
        self,
        request: TripRequest,
        plan: TripPlan,
    ) -> TripSessionDetail:
        return self.repository.create(request, plan)

    def send_message(self, session_id: str, content: str) -> tuple[
        TripSessionDetail,
        TripRequestPatch,
        str,
    ]:
        current = self.repository.get(session_id)
        if current is None:
            raise KeyError(session_id)

        rule_patch = parse_trip_request_patch(content)
        try:
            patch = _merge_patches(
                self.patch_interpreter(current.request, content),
                rule_patch,
            )
        except Exception as exc:
            logger.warning("Conversation LLM extraction failed; using rules: %s", exc)
            patch = rule_patch
        if patch.is_empty():
            raise ValueError("目前支持修改预算、行程节奏，以及增加或删除必去景点")

        updated_request = apply_trip_request_patch(current.request, patch)

        must_visits_changed = bool(patch.add_must_visit or patch.remove_must_visit)
        pace_changed = patch.pace is not None and patch.pace != current.request.pace
        if must_visits_changed or pace_changed:
            updated_plan = self.planner.plan_trip(updated_request)
        else:
            updated_plan = self.planner.replan(
                ReplanRequest(
                    plan=deepcopy(current.plan),
                    request=updated_request,
                    notes="根据对话修改预算",
                )
            )

        summary = describe_patch(patch)
        assistant_message = f"已完成修改：{summary}。"
        self.repository.save_turn(
            session_id,
            user_message=content,
            assistant_message=assistant_message,
            request=updated_request,
            plan=updated_plan,
            change_summary=summary,
        )
        detail = self.repository.get(session_id)
        if detail is None:
            raise RuntimeError("Updated session could not be loaded")
        return detail, patch, assistant_message

    def replan_existing(
        self,
        session_id: str,
        request: ReplanRequest,
    ) -> TripSessionDetail:
        current = self.repository.get(session_id)
        if current is None:
            raise KeyError(session_id)
        effective_request = request.request or current.request
        updated_plan = self.planner.replan(
            ReplanRequest(
                plan=deepcopy(request.plan),
                request=effective_request,
                notes=request.notes or "用户在结果页调整后重新计算",
            )
        )
        self.repository.add_version(
            session_id,
            effective_request,
            updated_plan,
            request.notes or "手动调整后重新计算",
        )
        detail = self.repository.get(session_id)
        if detail is None:
            raise RuntimeError("Replanned session could not be loaded")
        return detail


_conversation_service: Optional[TripConversationService] = None


def get_trip_conversation_service() -> TripConversationService:
    global _conversation_service
    if _conversation_service is None:
        _conversation_service = TripConversationService()
    return _conversation_service
