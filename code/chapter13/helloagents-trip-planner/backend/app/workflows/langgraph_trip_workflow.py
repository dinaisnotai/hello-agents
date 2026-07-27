"""LangGraph orchestration layer that reuses the existing trip-planning core."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Optional
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from hello_agents import SimpleAgent

from ..agents.agent_utils import parse_agent_result, run_stateless_agent
from ..agents.multi_agent_orchestrator import MultiAgentOrchestrator, get_multi_agent_orchestrator
from ..config import settings
from ..models.agent_outputs import AttractionSearchResult, HotelSearchResult, WeatherQueryResult
from ..models.schemas import EvidenceSource, Hotel, TripPlan, TripRequest, WorkflowExecutionSummary
from .planning_state import (
    ParseIntentInput,
    PlanningDraft,
    PlanningState,
    FreeTextIntentSupplement,
    SpecialistNodeInput,
    TravelIntent,
    WorkflowError,
    WorkflowTrace,
)


logger = logging.getLogger("uvicorn.error")


class LangGraphTripWorkflow:
    """Stateful workflow that owns orchestration, not planning algorithms."""

    # Opening-hours repairs can precede a separate route/time-window repair.
    max_repair_attempts = 5

    def __init__(
        self,
        orchestrator: MultiAgentOrchestrator | None = None,
        *,
        checkpoint_db_path: str | Path | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.orchestrator = orchestrator or get_multi_agent_orchestrator()
        self._connection: sqlite3.Connection | None = None
        if checkpointer is None:
            path = Path(checkpoint_db_path) if checkpoint_db_path else self._default_checkpoint_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(str(path), check_same_thread=False)
            checkpointer = SqliteSaver(self._connection)
        self.checkpointer = checkpointer
        self.graph = self._build_graph()

    @staticmethod
    def _default_checkpoint_path() -> Path:
        return Path(__file__).resolve().parents[2] / "data" / "langgraph_checkpoints.db"

    def _build_graph(self):
        graph = StateGraph(PlanningState)#每个node就是一个处理步骤 
        graph.add_node("parse_intent", self.parse_intent) #解析用户需求
        graph.add_node("attraction", self.attraction)   #查景点
        graph.add_node("weather", self.weather) 
        graph.add_node("hotel", self.hotel)
        graph.add_node("rag", self.rag)
        graph.add_node("build_draft", self.build_draft)
        graph.add_node("deterministic_planning", self.deterministic_planning)
        graph.add_node("validate_constraints", self.validate_constraints)
        graph.add_node("bounded_repair", self.bounded_repair)
        graph.add_node("soft_review", self.soft_review)
        graph.add_node("finalize", self.finalize)

        graph.add_edge(START, "parse_intent")   #edge可以把node连起来，这里一个parse_INTENT连了好几个node，并行
        graph.add_edge("parse_intent", "attraction")
        graph.add_edge("parse_intent", "weather")
        graph.add_edge("parse_intent", "hotel")
        graph.add_edge("parse_intent", "rag")
        graph.add_edge("attraction", "build_draft")
        graph.add_edge("weather", "build_draft")
        graph.add_edge("hotel", "build_draft")
        graph.add_edge("rag", "build_draft")
        graph.add_edge("build_draft", "deterministic_planning")
        graph.add_edge("deterministic_planning", "validate_constraints")
        graph.add_conditional_edges(
            "validate_constraints",
            self._next_after_validation,
            {"repair": "bounded_repair", "review": "soft_review"},
        )
        graph.add_edge("bounded_repair", "validate_constraints")
        graph.add_edge("soft_review", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=self.checkpointer)

    def run(self, request: TripRequest, *, thread_id: str | None = None) -> tuple[TripPlan, WorkflowExecutionSummary]:
        run_id = thread_id or uuid4().hex
        config = {"configurable": {"thread_id": run_id}}
        started_at = perf_counter()
        logger.info("[langgraph:%s] START trip workflow", run_id)
        state = self.graph.invoke(
            {"raw_request": request.model_dump(mode="json"), "repair_count": 0, "run_id": run_id},
            config=config,
        )
        plan, summary = self._result_from_state(state, run_id, resumed=False)
        logger.info(
            "[langgraph:%s] END status=%s degraded=%s errors=%d elapsed_ms=%.1f",
            run_id,
            "executable" if plan.constraint_report.passed else "partial",
            summary.degraded_services or "none",
            len(summary.errors),
            (perf_counter() - started_at) * 1000,
        )
        return plan, summary

    def resume(self, thread_id: str) -> tuple[TripPlan, WorkflowExecutionSummary]:
        """Continue a checkpointed graph after an external interruption."""

        state = self.graph.invoke(None, config={"configurable": {"thread_id": thread_id}})
        return self._result_from_state(state, thread_id, resumed=True)

    @staticmethod
    def _result_from_state(
        state: PlanningState, run_id: str, *, resumed: bool
    ) -> tuple[TripPlan, WorkflowExecutionSummary]:
        plan = TripPlan.model_validate(state["final_plan"])
        summary = WorkflowExecutionSummary(
            run_id=run_id,
            workflow_mode="langgraph",
            degraded_services=sorted(set(state.get("degraded_services", []))),
            errors=[item.get("message", str(item)) for item in state.get("errors", [])],
            resumed=resumed,
        )
        return plan, summary

    def close(self) -> None:
        """Release the SQLite checkpoint handle (primarily useful in tests)."""

        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def parse_intent(self, state: PlanningState) -> dict[str, Any]:
        raw = ParseIntentInput(raw_request=state["raw_request"]).raw_request
        try:
            request = TripRequest.model_validate(raw)
        except Exception as exc:
            request = self._fallback_request(raw)
            intent = self._intent_from_request(request, source="fallback")
            self._log(state, "parse_intent", "fallback", str(exc))
            return {
                "request": request.model_dump(mode="json"),
                "intent": intent.model_dump(mode="json"),
                "errors": [self._error("parse_intent", exc)],
                "degraded_services": ["intent_parser"],
                "trace": [self._trace("parse_intent", "fallback", str(exc))],
            }
        try:
            supplement = self._extract_free_text(request.free_text_input or "")
            request = self._merge_supplement(request, supplement)
        except Exception as exc:
            intent = self._intent_from_request(request)
            self._log(state, "parse_intent", "llm_supplement_failed", str(exc))
            return {
                "request": request.model_dump(mode="json"),
                "intent": intent.model_dump(mode="json"),
                "errors": [self._error("parse_intent", exc)],
                "degraded_services": ["intent_parser"],
                "trace": [self._trace("parse_intent", "llm_supplement_failed", str(exc))],
            }
        intent = self._intent_from_request(request)
        self._log(state, "parse_intent", "completed", intent.intent_source)
        return {
            "request": request.model_dump(mode="json"),
            "intent": intent.model_dump(mode="json"),
            "trace": [self._trace("parse_intent", "completed", intent.intent_source)],
        }

    def attraction(self, state: PlanningState) -> dict[str, Any]:
        return self._run_specialist("attraction", state, self.orchestrator.attraction_agent.run, AttractionSearchResult)

    def weather(self, state: PlanningState) -> dict[str, Any]:
        return self._run_specialist("weather", state, self.orchestrator.weather_agent.run, WeatherQueryResult)

    def hotel(self, state: PlanningState) -> dict[str, Any]:
        return self._run_specialist("hotel", state, self.orchestrator.hotel_agent.run, HotelSearchResult)

    def rag(self, state: PlanningState) -> dict[str, Any]:
        try:
            node_input = SpecialistNodeInput(request=state["request"], intent=TravelIntent.model_validate(state["intent"]))
            request = TripRequest.model_validate(node_input.request)
            evidence = self.orchestrator.plan_builder.rag.search(
                request.city, self.orchestrator.plan_builder.build_rag_query(request), top_k=5
            )
            self._log(state, "rag", "completed", f"evidence={len(evidence)}")
            return {"rag_results": [item.model_dump(mode="json") for item in evidence], "trace": [self._trace("rag", "completed")]}
        except Exception as exc:
            self._log(state, "rag", "fallback", str(exc), warning=True)
            return {"rag_results": [], "errors": [self._error("rag", exc)], "degraded_services": ["rag"], "trace": [self._trace("rag", "fallback", str(exc))]}

    def build_draft(self, state: PlanningState) -> dict[str, Any]:
        intent = TravelIntent.model_validate(state["intent"])
        draft = PlanningDraft(
            summary=f"{intent.city} {intent.travel_days} day trip, pace={intent.pace}",
            suggested_themes=list(dict.fromkeys([*intent.preferences, *intent.soft_preferences])),
            hard_constraints=[*intent.hard_constraints, *intent.must_visit],
        )
        self._log(state, "build_draft", "completed", f"themes={len(draft.suggested_themes)}")
        return {"llm_draft": draft.model_dump(mode="json"), "trace": [self._trace("build_draft", "completed", draft.source)]}

    def deterministic_planning(self, state: PlanningState) -> dict[str, Any]:
        try:
            request = TripRequest.model_validate(state["request"])
            attractions = AttractionSearchResult.model_validate(state.get("attraction_result", {}))
            weather = WeatherQueryResult.model_validate(state.get("weather_result", {}))
            hotels = HotelSearchResult.model_validate(state.get("hotel_result", {}))
            hotel = hotels.recommended_hotel or (hotels.candidates[0] if hotels.candidates else Hotel(name=f"{request.city} hotel", type=request.accommodation))
            evidence = [EvidenceSource.model_validate(item) for item in state.get("rag_results", [])]
            plan = self.orchestrator.plan_builder.build_plan_from_inputs(request, attractions.attractions, hotel, weather.weather, evidence)
            plan.risk_warnings = sorted(set([*plan.risk_warnings, *weather.risk_summary]))
            self._log(
                state,
                "deterministic_planning",
                "completed",
                f"days={len(plan.days)} score={plan.constraint_report.score:.2f}",
            )
            return {"deterministic_plan": plan.model_dump(mode="json"), "trace": [self._trace("deterministic_planning", "completed")]}
        except Exception as exc:
            self._log(state, "deterministic_planning", "failed", str(exc), warning=True)
            return {"errors": [self._error("deterministic_planning", exc)], "degraded_services": ["deterministic_planner"], "trace": [self._trace("deterministic_planning", "failed", str(exc))]}

    def validate_constraints(self, state: PlanningState) -> dict[str, Any]:
        plan = TripPlan.model_validate(state["deterministic_plan"])
        event = "passed" if plan.constraint_report.passed else "failed"
        self._log(state, "validate_constraints", event, f"score={plan.constraint_report.score:.2f}")
        return {"trace": [self._trace("validate_constraints", event, f"score={plan.constraint_report.score}")]}

    def bounded_repair(self, state: PlanningState) -> dict[str, Any]:
        plan = TripPlan.model_validate(state["deterministic_plan"])
        attempt = state.get("repair_count", 0) + 1
        if attempt > self.max_repair_attempts:
            self._log(state, "bounded_repair", "limit_reached")
            return {"repair_count": attempt, "trace": [self._trace("bounded_repair", "limit_reached")]}
        request = TripRequest.model_validate(state["request"])
        attractions = AttractionSearchResult.model_validate(state.get("attraction_result", {})).attractions
        action = self.orchestrator.plan_builder._apply_next_repair(plan, request, attractions)
        if action is not None:
            self.orchestrator.plan_builder._recalculate(plan, request)
        self._log(state, "bounded_repair", action[0] if action else "no_action", f"attempt={attempt}")
        return {"repair_count": attempt, "deterministic_plan": plan.model_dump(mode="json"), "trace": [self._trace("bounded_repair", action[0] if action else "no_action", action[1] if action else "")]}

    def soft_review(self, state: PlanningState) -> dict[str, Any]:
        plan = TripPlan.model_validate(state["deterministic_plan"])
        try:
            request = TripRequest.model_validate(state["request"])
            weather = WeatherQueryResult.model_validate(state.get("weather_result", {}))
            evidence = [EvidenceSource.model_validate(item) for item in state.get("rag_results", [])]
            reviewed = self.orchestrator.planner_agent.review_plan(request, plan, weather, evidence)
            self._log(state, "soft_review", "completed")
            return {"deterministic_plan": reviewed.model_dump(mode="json"), "trace": [self._trace("soft_review", "completed")]}
        except Exception as exc:
            self._log(state, "soft_review", "skipped", str(exc), warning=True)
            return {"errors": [self._error("soft_review", exc)], "degraded_services": ["soft_review"], "trace": [self._trace("soft_review", "skipped", str(exc))]}

    def finalize(self, state: PlanningState) -> dict[str, Any]:
        plan = TripPlan.model_validate(state["deterministic_plan"])
        self._log(state, "finalize", "completed", f"days={len(plan.days)}")
        return {"final_plan": plan.model_dump(mode="json"), "trace": [self._trace("finalize", "completed")]}

    def _next_after_validation(self, state: PlanningState) -> str:
        plan = TripPlan.model_validate(state["deterministic_plan"])
        if not plan.constraint_report.passed and state.get("repair_count", 0) < self.max_repair_attempts:
            return "repair"
        return "review"

    def _run_specialist(self, name: str, state: PlanningState, runner: Callable[[TripRequest], Any], result_type: type[Any]) -> dict[str, Any]:
        key = f"{name}_result"
        try:
            request = TripRequest.model_validate(SpecialistNodeInput(request=state["request"], intent=TravelIntent.model_validate(state["intent"])).request)
            result = result_type.model_validate(runner(request))
            update: dict[str, Any] = {key: result.model_dump(mode="json"), "trace": [self._trace(name, "completed")]}
            if result.used_fallback:
                update["degraded_services"] = [name]
            item_count = len(getattr(result, "attractions", getattr(result, "weather", getattr(result, "candidates", []))))
            self._log(state, name, "fallback" if result.used_fallback else "completed", f"items={item_count}")
            return update
        except Exception as exc:
            fallback = result_type(used_fallback=True, warnings=[f"{name} failed: {exc}"])
            self._log(state, name, "fallback", str(exc), warning=True)
            return {key: fallback.model_dump(mode="json"), "errors": [self._error(name, exc)], "degraded_services": [name], "trace": [self._trace(name, "fallback", str(exc))]}

    @staticmethod
    def _fallback_request(raw: dict[str, Any]) -> TripRequest:
        return TripRequest(
            city=str(raw.get("city") or "Beijing"), start_date=str(raw.get("start_date") or "2026-01-01"), end_date=str(raw.get("end_date") or "2026-01-01"), travel_days=max(1, min(30, int(raw.get("travel_days") or 1))), transportation=str(raw.get("transportation") or "public transit"), accommodation=str(raw.get("accommodation") or "budget hotel")
        )

    @staticmethod
    def _intent_from_request(request: TripRequest, source: str | None = None) -> TravelIntent:
        source = source or ("form_with_free_text" if request.free_text_input else "form")
        return TravelIntent(city=request.city, start_date=request.start_date, end_date=request.end_date, travel_days=request.travel_days, transportation=request.transportation, accommodation=request.accommodation, preferences=request.preferences, must_visit=request.must_visit, budget_limit=request.budget_limit, pace=request.pace if request.pace in {"relaxed", "balanced", "packed"} else "balanced", travelers=request.travelers, energy_preference=request.energy_preference, daily_start_time=request.daily_start_time or "09:00", daily_end_time=request.daily_end_time or "20:00", hard_constraints=request.hard_constraints, soft_preferences=request.soft_preferences, free_text=request.free_text_input or "", intent_source=source)

    @staticmethod
    def _merge_supplement(request: TripRequest, supplement: FreeTextIntentSupplement) -> TripRequest:
        """Form values win; text can add list items or fill absent scalar values."""

        data = request.model_dump(mode="json")
        for field in ("travelers", "hard_constraints", "soft_preferences", "preferences", "must_visit"):
            data[field] = list(dict.fromkeys([*data[field], *getattr(supplement, field)]))
        for field in ("energy_preference", "daily_start_time", "daily_end_time"):
            if data.get(field) is None and getattr(supplement, field) is not None:
                data[field] = getattr(supplement, field)
        return TripRequest.model_validate(data)

    @staticmethod
    def _extract_free_text(text: str) -> FreeTextIntentSupplement:
        if not text.strip():
            return FreeTextIntentSupplement()
        if not settings.enable_llm_intent_parsing:
            return LangGraphTripWorkflow._heuristic_time_window(text)
        from ..services.llm_service import get_llm

        agent = SimpleAgent(
            name="TravelIntentParser",
            llm=get_llm(),
            system_prompt=(
                "Extract only travel intent facts from the user's text. Return JSON with "
                "travelers, energy_preference(low|normal|high), daily_start_time, "
                "daily_end_time, hard_constraints, soft_preferences, preferences, and must_visit. "
                "Use empty values for unknown fields; never invent facts."
            ),
            enable_tool_calling=False,
        )
        raw = run_stateless_agent(agent, json.dumps({"free_text": text}, ensure_ascii=False))
        return parse_agent_result(raw, FreeTextIntentSupplement)

    @staticmethod
    def _heuristic_time_window(text: str) -> FreeTextIntentSupplement:
        """Safe no-LLM fallback for common explicit Chinese departure windows."""

        normalized = text.replace(" ", "")
        if any(term in normalized for term in ("下午出发", "下午才出门", "中午才起床", "午后出发")):
            return FreeTextIntentSupplement(
                daily_start_time="13:00",
                daily_end_time="20:00",
                hard_constraints=["Daily activities must stay within 13:00-20:00"],
            )
        if any(term in normalized for term in ("上午出发", "早上出发")):
            return FreeTextIntentSupplement(daily_start_time="09:00", daily_end_time="20:00")
        return FreeTextIntentSupplement()

    @staticmethod
    def _error(node: str, exc: Exception) -> dict[str, Any]:
        return WorkflowError(node=node, message=str(exc)).model_dump(mode="json")

    @staticmethod
    def _trace(node: str, event: str, detail: str = "") -> dict[str, Any]:
        return WorkflowTrace(node=node, event=event, detail=detail).model_dump(mode="json")

    @staticmethod
    def _log(
        state: PlanningState,
        node: str,
        event: str,
        detail: str = "",
        *,
        warning: bool = False,
    ) -> None:
        if os.getenv("LANGGRAPH_TRACE_LOGS", "true").strip().lower() in {"0", "false", "no", "off"}:
            return
        log = logger.warning if warning else logger.info
        suffix = f" {detail[:300]}" if detail else ""
        log("[langgraph:%s] %s %s%s", state.get("run_id", "unknown"), node, event, suffix)


_workflow: Optional[LangGraphTripWorkflow] = None


def get_langgraph_trip_workflow() -> LangGraphTripWorkflow:
    global _workflow
    if _workflow is None:
        _workflow = LangGraphTripWorkflow(
            checkpoint_db_path=settings.langgraph_checkpoint_db_path or None
        )
    return _workflow
