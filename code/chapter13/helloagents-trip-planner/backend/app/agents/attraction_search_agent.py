"""LLM-backed attraction specialist with deterministic fallback."""

from __future__ import annotations

import json
from threading import Lock
from typing import Optional

from hello_agents import HelloAgentsLLM, SimpleAgent, ToolRegistry

from ..models.agent_outputs import AttractionSearchResult
from ..models.schemas import TripRequest
from ..tools.amap_tools import AttractionSearchTool
from .agent_utils import (
    parse_agent_result,
    run_stateless_agent,
    specialist_max_tool_iterations,
)
from .prompts import ATTRACTION_SEARCH_PROMPT


class AttractionSearchAgent:
    def __init__(
        self,
        llm: Optional[HelloAgentsLLM],
        search_tool: AttractionSearchTool,
    ):
        self.search_tool = search_tool
        self._run_lock = Lock()
        self.agent: Optional[SimpleAgent] = None
        if llm is not None:
            registry = ToolRegistry()
            registry.register_tool(search_tool)
            self.agent = SimpleAgent(
                name="AttractionSearchAgent",
                llm=llm,
                system_prompt=ATTRACTION_SEARCH_PROMPT,
                tool_registry=registry,
                enable_tool_calling=True,
            )

    def run(self, request: TripRequest) -> AttractionSearchResult:
        agent_input = {
            "city": request.city,
            "preferences": request.preferences,
            "must_visit": request.must_visit,
            "avoid_categories": request.avoid_categories,
            "pace": request.pace,
        }

        if self.agent is not None:
            try:
                with self._run_lock:
                    raw_result = run_stateless_agent(
                        self.agent,
                        json.dumps(agent_input, ensure_ascii=False),
                        max_tool_iterations=specialist_max_tool_iterations(),
                    )
                result = parse_agent_result(raw_result, AttractionSearchResult)
                if not result.attractions:
                    raise ValueError("景点 Agent 没有返回候选景点")
                result.used_fallback = False
                return result
            except Exception as exc:
                return self._fallback(request, f"景点 Agent 调用失败：{exc}")

        return self._fallback(request, "LLM 未启用，使用确定性景点搜索")

    def _fallback(self, request: TripRequest, warning: str) -> AttractionSearchResult:
        # Local import prevents the new Agent layer from changing the existing
        # planner import graph before the orchestrator migration is complete.
        from .trip_planner_agent import POICollector

        attractions = POICollector(self.search_tool.amap_service).collect_attractions(request)
        keywords = list(dict.fromkeys([*request.preferences, *request.must_visit]))
        return AttractionSearchResult(
            search_keywords=keywords,
            attractions=attractions,
            warnings=[warning],
            used_fallback=True,
        )
