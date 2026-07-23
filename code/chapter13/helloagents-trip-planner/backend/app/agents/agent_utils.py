"""Small shared helpers for robust structured Agent output."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel


ModelT = TypeVar("ModelT", bound=BaseModel)


def run_stateless_agent(
    agent: Any,
    input_text: str,
    *,
    max_tool_iterations: Optional[int] = None,
) -> str:
    """Run one request without carrying conversation history into the next one."""

    clear_history = getattr(agent, "clear_history", None)
    if callable(clear_history):
        clear_history()
    try:
        if max_tool_iterations is None:
            return agent.run(input_text)
        return agent.run(
            input_text,
            max_tool_iterations=max(1, max_tool_iterations),
        )
    finally:
        if callable(clear_history):
            clear_history()


def specialist_max_tool_iterations() -> int:
    """One iteration allows one tool-call round plus one final LLM response."""

    try:
        return max(1, int(os.getenv("LLM_MAX_TOOL_ITERATIONS", "1")))
    except ValueError:
        return 1


def parse_agent_result(raw_result: str, model_type: Type[ModelT]) -> ModelT:
    """Accept plain JSON or a fenced JSON object and validate it."""

    if not isinstance(raw_result, str) or not raw_result.strip():
        raise ValueError("Agent 返回了空结果")

    text = raw_result.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])

    return model_type.model_validate(payload)
