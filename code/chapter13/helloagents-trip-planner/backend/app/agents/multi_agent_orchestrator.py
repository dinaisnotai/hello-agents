"""Orchestrate the four specialist Agents into one trip-planning workflow."""

from __future__ import annotations

from typing import Dict, Optional

from hello_agents import HelloAgentsLLM

from ..models.schemas import ReplanRequest, TripPlan, TripRequest
from ..services.amap_service import AmapService, get_amap_service
from ..services.llm_service import get_llm
from .attraction_search_agent import AttractionSearchAgent
from .hotel_agent import HotelAgent
from .planner_agent import PlannerAgent
from .trip_planner_agent import MultiAgentTripPlanner
from .weather_query_agent import WeatherQueryAgent
from ..tools.amap_tools import AttractionSearchTool, HotelSearchTool, WeatherQueryTool


class MultiAgentOrchestrator:
    """Run three tool Agents followed by the hybrid PlannerAgent."""

    def __init__(
        self,
        llm: Optional[HelloAgentsLLM] = None,
        *,
        enable_llm: bool = True,
        amap_service: Optional[AmapService] = None,
        plan_builder: Optional[MultiAgentTripPlanner] = None,
    ):
        self.initialization_warning = ""
        if enable_llm and llm is None:
            try:
                llm = get_llm()
            except Exception as exc:
                self.initialization_warning = f"LLM 初始化失败，使用确定性降级：{exc}"
        if not enable_llm:
            llm = None

        self.amap_service = amap_service or get_amap_service()
        self.plan_builder = plan_builder or MultiAgentTripPlanner()
        self.attraction_agent = AttractionSearchAgent(
            llm,
            AttractionSearchTool(self.amap_service),
        )
        self.weather_agent = WeatherQueryAgent(
            llm,
            WeatherQueryTool(self.amap_service),
        )
        self.hotel_agent = HotelAgent(
            llm,
            HotelSearchTool(self.amap_service),
        )
        self.planner_agent = PlannerAgent(llm, self.plan_builder)
        self.last_run_status: Dict[str, str] = {}

    def plan_trip(self, request: TripRequest) -> TripPlan:
        attractions = self.attraction_agent.run(request)
        weather = self.weather_agent.run(request)
        hotels = self.hotel_agent.run(request)
        evidence = self.plan_builder.rag.search(
            request.city,
            self.plan_builder.build_rag_query(request),
            top_k=5,
        )
        plan = self.planner_agent.run(
            request=request,
            attraction_result=attractions,
            weather_result=weather,
            hotel_result=hotels,
            evidence=evidence,
        )
        self.last_run_status = {
            "AttractionSearchAgent": _mode(attractions.used_fallback),
            "WeatherQueryAgent": _mode(weather.used_fallback),
            "HotelAgent": _mode(hotels.used_fallback),
            "PlannerAgent": "fallback" if self.planner_agent.last_warning else "llm",
        }
        return plan

    def replan(self, request: ReplanRequest) -> TripPlan:
        """User edits require deterministic recalculation, not new discovery."""

        return self.plan_builder.replan(request)

    def health_snapshot(self) -> Dict[str, object]:
        return {
            "status": "healthy",
            "service": "multi-agent-trip-planner",
            "roles": [
                self.attraction_agent.__class__.__name__,
                self.weather_agent.__class__.__name__,
                self.hotel_agent.__class__.__name__,
                self.planner_agent.__class__.__name__,
            ],
            "initialization_warning": self.initialization_warning,
            "last_run_status": self.last_run_status,
        }


def _mode(used_fallback: bool) -> str:
    return "fallback" if used_fallback else "llm"


_orchestrator: Optional[MultiAgentOrchestrator] = None


def get_multi_agent_orchestrator() -> MultiAgentOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = MultiAgentOrchestrator()
    return _orchestrator
