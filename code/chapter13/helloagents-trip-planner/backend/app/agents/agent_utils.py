"""Small shared helpers for robust structured Agent output."""

from __future__ import annotations

import json
import re
from typing import Type, TypeVar

from pydantic import BaseModel


ModelT = TypeVar("ModelT", bound=BaseModel)


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
