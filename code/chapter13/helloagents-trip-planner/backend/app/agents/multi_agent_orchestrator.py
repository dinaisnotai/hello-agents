"""Orchestrate the four specialist Agents into one trip-planning workflow."""

from __future__ import annotations

import json
import inspect
import logging
import os
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from time import perf_counter
from typing import Any, Dict, Optional
from uuid import uuid4

from hello_agents import HelloAgentsLLM
from pydantic import BaseModel

from ..models.schemas import ReplanRequest, TripPlan, TripRequest
from ..services.amap_service import AmapService, get_amap_service
from ..services.llm_service import get_llm
from ..services.planning_observability import (
    build_planning_trace,
    emit_planning_trace,
    refresh_planning_trace,
)
from .attraction_search_agent import AttractionSearchAgent
from .hotel_agent import HotelAgent
from .planner_agent import PlannerAgent
from .trip_planner_agent import MultiAgentTripPlanner
from .weather_query_agent import WeatherQueryAgent
from ..tools.amap_tools import AttractionSearchTool, HotelSearchTool, WeatherQueryTool


logger = logging.getLogger("uvicorn.error")


def _agent_output_logging_enabled() -> bool:
    """Detailed specialist payloads are opt-in because map results are large."""

    return os.getenv("AGENT_LOG_OUTPUTS", "false").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _serialize_log_value(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = repr(value)

    try:
        max_chars = max(1000, int(os.getenv("AGENT_LOG_MAX_CHARS", "50000")))
    except ValueError:
        max_chars = 50000
    if len(text) > max_chars:
        omitted = len(text) - max_chars
        return f"{text[:max_chars]}\n... [日志已截断，省略 {omitted} 个字符]"
    return text


def _log_output(
    run_id: str,
    agent_name: str,
    output: Any,
    started_at: float,
    *,
    mode: str,
    warning: str = "",
) -> None:
    if not _agent_output_logging_enabled():
        return
    elapsed_ms = (perf_counter() - started_at) * 1000
    warning_line = f"\nwarning={warning}" if warning else ""
    logger.info(
        "[agent-run:%s] [%s] OUTPUT mode=%s elapsed_ms=%.1f%s\n%s",
        run_id,
        agent_name,
        mode,
        elapsed_ms,
        warning_line,
        _serialize_log_value(output),
    )


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
        run_id = uuid4().hex[:8]
        workflow_started_at = perf_counter()
        if _agent_output_logging_enabled():
            logger.info(
                "[agent-run:%s] START MultiAgentOrchestrator\n%s",
                run_id,
                _serialize_log_value(request),
            )

        specialist_results: Dict[str, Any] = {}
        specialist_started_at: Dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="trip-agent") as executor:
            futures: Dict[Future[Any], str] = {}
            for agent_name, run_agent in (
                ("AttractionSearchAgent", self.attraction_agent.run),
                ("WeatherQueryAgent", self.weather_agent.run),
                ("HotelAgent", self.hotel_agent.run),
            ):
                specialist_started_at[agent_name] = perf_counter()
                futures[executor.submit(run_agent, request)] = agent_name

            for future in as_completed(futures):
                agent_name = futures[future]
                result = future.result()
                specialist_results[agent_name] = result
                _log_output(
                    run_id,
                    agent_name,
                    result,
                    specialist_started_at[agent_name],
                    mode=_mode(result.used_fallback),
                )

        attractions = specialist_results["AttractionSearchAgent"]
        weather = specialist_results["WeatherQueryAgent"]
        hotels = specialist_results["HotelAgent"]

        query = self.plan_builder.build_rag_query(request)
        metadata_builder = getattr(self.plan_builder, "build_rag_metadata", None)
        if callable(metadata_builder):
            evidence = self.plan_builder.rag.search(
                request.city, query, top_k=5, metadata=metadata_builder(request)
            )
        else:
            # Preserve compatibility with lightweight test/durable builders
            # that implement the legacy RAG boundary only.
            evidence = self.plan_builder.rag.search(request.city, query, top_k=5)

        started_at = perf_counter()
        plan = self.planner_agent.run(
            request=request,
            attraction_result=attractions,
            weather_result=weather,
            hotel_result=hotels,
            evidence=evidence,
        )
        planner_mode = "fallback" if self.planner_agent.last_warning else "llm"
        _log_output(
            run_id,
            "PlannerAgent",
            plan,
            started_at,
            mode=planner_mode,
            warning=self.planner_agent.last_warning,
        )
        used_hard_constraint_fallback = False
        if not plan.constraint_report.passed:
            # The LLM specialists are allowed to improve recall, but never to
            # become the source of truth for mandatory constraints. Rebuild from
            # POICollector/map data, which has dedicated must-visit recall and
            # deterministic filtering before scheduling.
            logger.warning(
                "[agent-run:%s] hard constraints failed after LLM workflow; "
                "rebuilding with deterministic POICollector",
                run_id,
            )
            fallback_method = self.plan_builder.plan_trip
            fallback_kwargs = {}
            if "emit_observability" in inspect.signature(
                fallback_method
            ).parameters:
                fallback_kwargs["emit_observability"] = False
            plan = fallback_method(request, **fallback_kwargs)
            used_hard_constraint_fallback = True
        self.last_run_status = {
            "AttractionSearchAgent": _mode(attractions.used_fallback),
            "WeatherQueryAgent": _mode(weather.used_fallback),
            "HotelAgent": _mode(hotels.used_fallback),
            "PlannerAgent": planner_mode,
            "HardConstraintFallback": "used" if used_hard_constraint_fallback else "not_needed",
        }
        if _agent_output_logging_enabled():
            logger.info(
                "[agent-run:%s] END MultiAgentOrchestrator elapsed_ms=%.1f status=%s",
                run_id,
                (perf_counter() - workflow_started_at) * 1000,
                self.last_run_status,
            )
        if plan.observability_trace is None:
            plan.observability_trace = build_planning_trace(
                request,
                plan,
                candidates=attractions.attractions,
                eligible_candidates=attractions.attractions,
                run_id=run_id,
                run_type="legacy",
            )
        refresh_planning_trace(plan, run_id=run_id, run_type="legacy")
        emit_planning_trace(plan.observability_trace)
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
