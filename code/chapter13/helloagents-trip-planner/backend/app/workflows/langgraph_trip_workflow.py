"""LangGraph orchestration layer that reuses the existing trip-planning core."""

from __future__ import annotations

import json
import inspect
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
from ..models.schemas import Attraction, EvidenceSource, Hotel, TripPlan, TripRequest, WorkflowExecutionSummary
from ..models.quality import ExperienceEvaluation, ExperienceIssue, RepairStrategy
from ..constraints.extractor import ConstraintExtractor
from ..constraints.schema import ConstraintSet
from ..services.place_name_service import normalize_place_name
from ..services.accommodation_selector import AccommodationSelector
from ..services.user_warning_service import curate_user_warnings
from ..services.plan_mutation_sandbox import PlanMutationSandbox
from ..services.planning_observability import (
    build_planning_trace,
    emit_planning_trace,
    refresh_planning_trace,
)
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
    max_repair_attempts = 3

    def __init__(
        self,
        orchestrator: MultiAgentOrchestrator | None = None,
        *,
        checkpoint_db_path: str | Path | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.orchestrator = orchestrator or get_multi_agent_orchestrator()
        self.constraint_extractor = ConstraintExtractor()
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
        graph.add_node("constraint_extractor", self.extract_constraints)
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
        graph.add_edge("parse_intent", "constraint_extractor")
        graph.add_edge("constraint_extractor", "attraction")
        graph.add_edge("constraint_extractor", "weather")
        graph.add_edge("constraint_extractor", "hotel")
        graph.add_edge("constraint_extractor", "rag")
        graph.add_edge("attraction", "build_draft")
        graph.add_edge("weather", "build_draft")
        graph.add_edge("hotel", "build_draft")
        graph.add_edge("rag", "build_draft")
        graph.add_edge("build_draft", "deterministic_planning")
        graph.add_edge("deterministic_planning", "validate_constraints")
        graph.add_conditional_edges(
            "validate_constraints",
            self._next_after_validation,
            {
                "repair": "bounded_repair",
                "review": "soft_review",
                "finalize": "finalize",
            },
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
            {
                "raw_request": request.model_dump(mode="json"),
                "repair_count": 0,
                "run_id": run_id,
                "current_plan_version": 1,
                "plan_version_history": [],
                "repair_history_summary": [],
            },
            config=config,
        )
        plan, summary = self._result_from_state(state, run_id, resumed=False)
        if plan.observability_trace is None:
            request_for_trace = TripRequest.model_validate(state["request"])
            plan.observability_trace = build_planning_trace(
                request_for_trace,
                plan,
                candidates=[
                    Attraction.model_validate(item)
                    for item in state.get("planning_candidates", [])
                ],
                run_id=run_id,
                run_type="langgraph",
            )
        refresh_planning_trace(plan, run_id=run_id, run_type="langgraph")
        emit_planning_trace(plan.observability_trace)
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

        state = self.graph.invoke(
            None,
            config={"configurable": {"thread_id": thread_id}},
        )
        plan, summary = self._result_from_state(
            state,
            thread_id,
            resumed=True,
        )
        if plan.observability_trace is None:
            request_for_trace = TripRequest.model_validate(state["request"])
            plan.observability_trace = build_planning_trace(
                request_for_trace,
                plan,
                candidates=[
                    Attraction.model_validate(item)
                    for item in state.get("planning_candidates", [])
                ],
                run_id=thread_id,
                run_type="langgraph_resume",
            )
        refresh_planning_trace(
            plan,
            run_id=thread_id,
            run_type="langgraph_resume",
        )
        emit_planning_trace(plan.observability_trace)
        return plan, summary

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

    def extract_constraints(
        self, state: PlanningState
    ) -> dict[str, Any]:
        request = TripRequest.model_validate(state["request"])
        constraint_set = self.constraint_extractor.extract(request)
        self._log(
            state,
            "constraint_extractor",
            "completed",
            (
                f"constraints={len(constraint_set.constraints)} "
                f"presets={constraint_set.applied_presets}"
            ),
        )
        return {
            "normalized_constraints": [
                item.model_dump(mode="json")
                for item in constraint_set.constraints
            ],
            "trace": [
                self._trace(
                    "constraint_extractor",
                    "completed",
                    f"constraints={len(constraint_set.constraints)}",
                )
            ],
        }

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
            start_time=perf_counter()
            request = TripRequest.model_validate(state["request"])
            attractions = AttractionSearchResult.model_validate(state.get("attraction_result", {}))
            weather = WeatherQueryResult.model_validate(state.get("weather_result", {}))
            hotels = HotelSearchResult.model_validate(state.get("hotel_result", {}))
            evidence = [EvidenceSource.model_validate(item) for item in state.get("rag_results", [])]
            constraint_set = ConstraintSet(
                constraints=state.get("normalized_constraints", [])
            )
            build_method = (
                self.orchestrator.plan_builder.build_plan_from_inputs
            )
            kwargs = {}
            if "constraint_set" in inspect.signature(
                build_method
            ).parameters:
                kwargs["constraint_set"] = constraint_set
            planning_candidates = self._expand_candidate_pool(
                request, attractions.attractions
            )
            hotel_candidates = list(hotels.candidates)
            if not hotel_candidates and hotels.recommended_hotel is not None:
                hotel_candidates = [hotels.recommended_hotel]
            selector = getattr(
                self.orchestrator.plan_builder,
                "accommodation_selector",
                AccommodationSelector(),
            )
            hotel = selector.select(
                request,
                hotel_candidates,
                planning_candidates,
            )
            if "hotel_candidates" in inspect.signature(
                build_method
            ).parameters:
                kwargs["hotel_candidates"] = hotel_candidates
            plan = build_method(
                request,
                planning_candidates,
                hotel,
                weather.weather,
                evidence,
                **kwargs,
            )
            plan.risk_warnings = sorted(set([*plan.risk_warnings, *weather.risk_summary]))
            self._log(
                state,
                "deterministic_planning",
                "completed",
                f"days={len(plan.days)} score={plan.constraint_report.score:.2f}",
            )
            end_time=perf_counter()
            logger.info(f"end_time-start_time={end_time-start_time:.2f}")
            return {
                "deterministic_plan": plan.model_dump(mode="json"),
                "current_plan_version": plan.plan_version.version,
                "plan_version_history": [
                    plan.plan_version.model_dump(mode="json")
                ],
                "planning_candidates": [
                    item.model_dump(mode="json")
                    for item in planning_candidates
                ],
                "trace": [
                    self._trace(
                        "deterministic_planning",
                        "completed",
                        f"candidates={len(planning_candidates)}",
                    )
                ],
            }
        except Exception as exc:
            self._log(state, "deterministic_planning", "failed", str(exc), warning=True)
            end_time=perf_counter()
            logger.info(f"fail end_time-start_time={end_time-start_time:.2f}")
            return {"errors": [self._error("deterministic_planning", exc)], "degraded_services": ["deterministic_planner"], "trace": [self._trace("deterministic_planning", "failed", str(exc))]}

    def validate_constraints(self, state: PlanningState) -> dict[str, Any]:
        plan = TripPlan.model_validate(state["deterministic_plan"])
        valid = plan.validation_result.valid and (
            bool(plan.normalized_constraints)
            or plan.constraint_report.passed
        )
        event = "passed" if valid else "failed"
        detail = (
            f"score={plan.validation_result.score:.2f} "
            f"violations={len(plan.validation_result.violations)}"
        )
        self._log(state, "validate_constraints", event, detail)
        return {
            "validation_result": plan.validation_result.model_dump(
                mode="json"
            ),
            "trace": [
                self._trace("validate_constraints", event, detail)
            ],
        }

    def bounded_repair(self, state: PlanningState) -> dict[str, Any]:
        plan = TripPlan.model_validate(state["deterministic_plan"])
        attempt = state.get("repair_count", 0) + 1
        if attempt > self.max_repair_attempts:
            self._log(state, "bounded_repair", "limit_reached")
            return {"repair_count": attempt, "trace": [self._trace("bounded_repair", "limit_reached")]}
        request = TripRequest.model_validate(state["request"])
        attractions = [
            Attraction.model_validate(item)
            for item in state.get("planning_candidates", [])
        ] or AttractionSearchResult.model_validate(
            state.get("attraction_result", {})
        ).attractions
        builder = self.orchestrator.plan_builder
        issue = ExperienceIssue(
            issue_type="constraint_failure",
            severity="high",
            evidence="Deterministic constraint validation requires repair",
            repair_strategy=RepairStrategy.RUN_CONSTRAINT_REPAIR,
        )

        def hard_snapshot(candidate: TripPlan) -> dict[str, Any]:
            hard_pass_method = getattr(builder, "_hard_constraints_pass", None)
            hard_keys_method = getattr(builder, "_hard_violation_keys", None)
            hard_pass = (
                hard_pass_method(candidate)
                if hard_pass_method is not None
                else candidate.validation_result.valid
                and candidate.constraint_report.passed
            )
            return {
                "hard_pass": hard_pass,
                "hard_keys": (
                    sorted(hard_keys_method(candidate))
                    if hard_keys_method is not None
                    else ([] if hard_pass else ["constraint_report"])
                ),
            }

        before_hard = hard_snapshot(plan)

        def evaluate(candidate: TripPlan) -> ExperienceEvaluation:
            snapshot = hard_snapshot(candidate)
            return ExperienceEvaluation(
                **{"pass": snapshot["hard_pass"]},
                overall_score=10 if snapshot["hard_pass"] else 0,
                issues=[] if snapshot["hard_pass"] else [issue],
            )

        action_holder: list[tuple[str, str] | None] = [None]

        def mutate(candidate: TripPlan) -> bool:
            action_holder[0] = builder._apply_next_repair(candidate, request, attractions)
            return action_holder[0] is not None

        sandbox = PlanMutationSandbox(
            recalculate=builder._recalculate,
            evaluate=evaluate,
            hard_snapshot=hard_snapshot,
        )
        outcome = sandbox.execute(
            base_plan=plan,
            current_plan=plan,
            request=request,
            issue=issue,
            strategy=RepairStrategy.RUN_CONSTRAINT_REPAIR,
            mutate=mutate,
        )
        committed = outcome.plan
        committed.repair_attempts = [*committed.repair_attempts, outcome.attempt]
        version_history = list(state.get("plan_version_history", []))
        version_payload = committed.plan_version.model_dump(mode="json")
        if not version_history or version_history[-1].get("version") != version_payload["version"]:
            version_history.append(version_payload)
        action = action_holder[0]
        detail = (
            action[1] if action else outcome.decision.code.value
        )
        self._log(state, "bounded_repair", action[0] if action else outcome.decision.code.value, f"attempt={attempt}; {detail}")
        return {
            "repair_count": attempt,
            "current_plan_version": committed.plan_version.version,
            "plan_version_history": version_history,
            "active_repair_attempt": outcome.attempt.model_dump(mode="json"),
            "repair_history_summary": [{
                "attempt_id": outcome.attempt.attempt_id,
                "status": outcome.attempt.status.value,
                "base_plan_version": outcome.attempt.base_plan_version,
                "decision": outcome.decision.code.value,
            }],
            "deterministic_plan": committed.model_dump(mode="json"),
            "trace": [self._trace("bounded_repair", action[0] if action else outcome.decision.code.value, detail)],
        }

    def soft_review(self, state: PlanningState) -> dict[str, Any]:
        plan = TripPlan.model_validate(state["deterministic_plan"])
        try:
            request = TripRequest.model_validate(state["request"])
            weather = WeatherQueryResult.model_validate(state.get("weather_result", {}))
            evidence = [EvidenceSource.model_validate(item) for item in state.get("rag_results", [])]
            candidates = [
                Attraction.model_validate(item)
                for item in state.get("planning_candidates", [])
            ]
            hotels = HotelSearchResult.model_validate(
                state.get("hotel_result", {})
            )
            review_method = self.orchestrator.planner_agent.review_plan
            review_kwargs = {}
            review_parameters = inspect.signature(review_method).parameters
            if "available_attractions" in review_parameters:
                review_kwargs["available_attractions"] = candidates
            if "hotel_candidates" in review_parameters:
                review_kwargs["hotel_candidates"] = hotels.candidates
            reviewed = review_method(
                request,
                plan,
                weather,
                evidence,
                **review_kwargs,
            )
            if (
                getattr(self.orchestrator.planner_agent, "agent", None) is None
                and hasattr(
                    self.orchestrator.plan_builder,
                    "run_quality_loop",
                )
            ):
                reviewed = self.orchestrator.plan_builder.run_quality_loop(
                    reviewed,
                    request,
                    candidates,
                    hotel_candidates=hotels.candidates,
                )
            self._log(state, "soft_review", "completed")
            return {"deterministic_plan": reviewed.model_dump(mode="json"), "trace": [self._trace("soft_review", "completed")]}
        except Exception as exc:
            self._log(state, "soft_review", "skipped", str(exc), warning=True)
            return {"errors": [self._error("soft_review", exc)], "degraded_services": ["soft_review"], "trace": [self._trace("soft_review", "skipped", str(exc))]}

    def finalize(self, state: PlanningState) -> dict[str, Any]:
        plan = TripPlan.model_validate(state["deterministic_plan"])
        if not plan.validation_result.valid:
            hard_messages = [
                item.message
                for item in plan.validation_result.violations
                if item.severity == "hard"
            ]
            plan.failure_reason = plan.failure_reason or "; ".join(
                hard_messages
            )
        if not plan.quality_gate_passed:
            plan.risk_warnings = sorted(
                set(
                    [
                        *plan.risk_warnings,
                        "Quality Gate 未通过；返回 degraded result，并保留 unresolved issues。",
                    ]
                )
            )
            plan.risk_warnings = sorted(
                set(
                    [
                        *plan.risk_warnings,
                        "行程存在未满足的硬约束，不能视为可执行方案。",
                    ]
                )
            )
        curate_user_warnings(plan)
        self._log(
            state,
            "finalize",
            (
                "completed"
                if plan.validation_result.valid
                and plan.quality_gate_passed
                else "hard_constraints_failed"
                if not plan.validation_result.valid
                else "quality_gate_failed"
            ),
            f"days={len(plan.days)}",
        )
        result = {
            "final_plan": plan.model_dump(mode="json"),
            "trace": [
                self._trace(
                    "finalize",
                    (
                        "completed"
                        if plan.validation_result.valid
                        and plan.quality_gate_passed
                        else "degraded"
                    ),
                    plan.failure_reason or "",
                )
            ],
        }
        if not plan.quality_gate_passed:
            result["degraded_services"] = ["quality_gate"]
        return result

    def _next_after_validation(self, state: PlanningState) -> str:
        plan = TripPlan.model_validate(state["deterministic_plan"])
        valid = plan.validation_result.valid and (
            bool(plan.normalized_constraints)
            or plan.constraint_report.passed
        )
        if not valid and state.get("repair_count", 0) < self.max_repair_attempts:
            return "repair"
        return "review" if valid else "finalize"

    def _expand_candidate_pool(
        self,
        request: TripRequest,
        specialist_candidates: list[Attraction],
    ) -> list[Attraction]:
        """Prevent a thin LLM result from making multi-day coverage impossible."""

        coverage_method = getattr(
            self.orchestrator.plan_builder,
            "ensure_candidate_coverage",
            None,
        )
        if coverage_method is not None:
            return coverage_method(request, specialist_candidates)

        unique = {
            normalize_place_name(item.name): item
            for item in specialist_candidates
        }
        minimum = min(30, max(request.travel_days * 2, request.travel_days))
        if len(unique) >= minimum:
            return list(unique.values())
        collector = getattr(
            self.orchestrator.plan_builder, "poi_collector", None
        )
        if collector is None:
            return list(unique.values())
        for attraction in collector.collect_attractions(request):
            unique.setdefault(
                normalize_place_name(attraction.name), attraction
            )
        return sorted(
            unique.values(),
            key=lambda item: (
                -item.score,
                normalize_place_name(item.name),
                item.poi_id or "",
            ),
        )

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
        return TravelIntent(city=request.city, start_date=request.start_date, end_date=request.end_date, travel_days=request.travel_days, transportation=request.transportation, accommodation=request.accommodation, preferences=request.preferences, must_visit=request.must_visit, budget_limit=request.budget_limit, pace=request.pace if request.pace in {"relaxed", "balanced", "packed"} else "balanced", travelers=request.travelers, energy_preference=request.energy_preference, daily_start_time=request.daily_start_time or "09:00", daily_end_time=request.daily_end_time or "20:00", arrival_time=request.arrival_time, departure_time=request.departure_time, hard_constraints=request.hard_constraints, soft_preferences=request.soft_preferences, free_text=request.free_text_input or "", intent_source=source)

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
