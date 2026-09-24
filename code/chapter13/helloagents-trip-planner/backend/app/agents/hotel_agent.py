"""LLM-backed hotel specialist with deterministic fallback."""

from __future__ import annotations

import json
from threading import Lock
from typing import Optional

from hello_agents import HelloAgentsLLM, SimpleAgent, ToolRegistry

from ..models.agent_outputs import HotelSearchResult
from ..models.schemas import TripRequest
from ..tools.amap_tools import HotelSearchTool
from .agent_utils import (
    parse_agent_result,
    run_stateless_agent,
    specialist_max_tool_iterations,
)
from .prompts import HOTEL_SEARCH_PROMPT


class HotelAgent:
    def __init__(
        self,
        llm: Optional[HelloAgentsLLM],
        search_tool: HotelSearchTool,
    ):
        self.search_tool = search_tool
        self._run_lock = Lock()
        self.agent: Optional[SimpleAgent] = None
        if llm is not None:
            registry = ToolRegistry()
            registry.register_tool(search_tool)
            self.agent = SimpleAgent(
                name="HotelAgent",
                llm=llm,
                system_prompt=HOTEL_SEARCH_PROMPT,
                tool_registry=registry,
                enable_tool_calling=True,
            )

    def run(self, request: TripRequest, *, evidence=()) -> HotelSearchResult:
        agent_input = {
            "city": request.city,
            "hotel_area": request.hotel_area,
            "accommodation": request.accommodation,
            "budget_limit": request.budget_limit,
            "travel_days": request.travel_days,
            "must_visit": request.must_visit,
            "max_daily_walk_km": request.max_daily_walk_km,
            "requirements": request.free_text_input,
            "guide_evidence": [item.model_dump(mode="json") for item in evidence[:4]],
        }
        if self.agent is not None:
            try:
                with self._run_lock:
                    raw_result = run_stateless_agent(
                        self.agent,
                        json.dumps(agent_input, ensure_ascii=False),
                        max_tool_iterations=specialist_max_tool_iterations(),
                    )
                result = parse_agent_result(raw_result, HotelSearchResult)
                from .trip_planner_agent import POICollector

                queries = result.search_keywords or [item.name for item in result.candidates[:3]]
                result.candidates = POICollector(self.search_tool.amap_service).collect_hotels(
                    request, additional_keywords=queries,
                )
                result.recommended_hotel = result.candidates[0]
                result.used_fallback = False
                return result
            except Exception as exc:
                return self._fallback(request, f"酒店 Agent 调用失败：{exc}")

        return self._fallback(request, "LLM 未启用，使用确定性酒店搜索")

    def _fallback(self, request: TripRequest, warning: str) -> HotelSearchResult:
        from .trip_planner_agent import POICollector

        hotels = POICollector(self.search_tool.amap_service).collect_hotels(request)
        area = request.hotel_area or request.city
        keyword = f"{area} {request.accommodation} 酒店"
        return HotelSearchResult(
            search_keywords=[keyword],
            candidates=hotels,
            recommended_hotel=hotels[0],
            reason="保留住宿候选，交由行程可达性、交通与预算联合评分",
            warnings=[warning],
            used_fallback=True,
        )
