"""Hybrid planner Agent: deterministic schedule plus LLM soft review."""

from __future__ import annotations

import json
import logging
from threading import Lock
from time import perf_counter
from typing import List, Optional

from hello_agents import HelloAgentsLLM, SimpleAgent
from pydantic import ValidationError

from ..models.agent_outputs import (
    AttractionSearchResult,
    HotelSearchResult,
    PlannerReviewResult,
    WeatherQueryResult,
)
from ..models.schemas import EvidenceSource, Hotel, TripPlan, TripRequest
from ..models.quality import ExperienceEvaluation
from ..models.context_governance import ContextRole
from ..config import settings
from ..services.accommodation_selector import AccommodationSelector
from ..services.itinerary_quality import ItineraryCompletenessGate
from ..services.planning_observability import refresh_planning_trace
from ..services.user_warning_service import curate_user_warnings
from ..services.context_governance import RoleContextBuilder
from ..services.travel_knowledge_service import get_travel_knowledge_service
from .agent_utils import parse_agent_result, run_stateless_agent
from .prompts import PLANNER_PROMPT
from .trip_planner_agent import MultiAgentTripPlanner


logger = logging.getLogger("uvicorn.error")


class PlannerAgent:
    """Integrate specialist outputs without delegating arithmetic to the LLM."""

    def __init__(
        self,
        llm: Optional[HelloAgentsLLM],
        plan_builder: Optional[MultiAgentTripPlanner] = None,
    ):
        self.plan_builder = plan_builder or MultiAgentTripPlanner()
        self._run_lock = Lock()
        self.agent: Optional[SimpleAgent] = None
        self.last_warning = ""
        self.context_builder = RoleContextBuilder()
        self.travel_knowledge = get_travel_knowledge_service()
        if llm is not None:
            self.agent = SimpleAgent(
                name="PlannerAgent",
                llm=llm,
                system_prompt=PLANNER_PROMPT,
                enable_tool_calling=False,
            )

    def run(
        self,
        request: TripRequest,
        attraction_result: AttractionSearchResult,
        weather_result: WeatherQueryResult,
        hotel_result: HotelSearchResult,
        evidence: List[EvidenceSource],
    ) -> TripPlan:
        """Build the executable plan first, then request a non-mutating review."""

        deterministic_started_at = perf_counter()
        candidates = list(hotel_result.candidates)
        if not candidates and hotel_result.recommended_hotel is not None:
            candidates = [hotel_result.recommended_hotel]
        hotel = AccommodationSelector().select(
            request,
            candidates,
            attraction_result.attractions,
        )
        plan = self.plan_builder.build_plan_from_inputs(
            request=request,
            attractions=attraction_result.attractions,
            hotel=hotel,
            weather=weather_result.weather,
            evidence=evidence,
        )
        plan.risk_warnings = sorted(
            set([*plan.risk_warnings, *weather_result.risk_summary])
        )
        logger.debug(
            "[PlannerAgent] deterministic planning elapsed_ms=%.1f",
            (perf_counter() - deterministic_started_at) * 1000,
        )
        return self.review_plan(
            request,
            plan,
            weather_result,
            evidence,
            available_attractions=attraction_result.attractions,
            hotel_candidates=candidates,
        )

    def review_plan(
        self,
        request: TripRequest,
        plan: TripPlan,
        weather_result: WeatherQueryResult,
        evidence: List[EvidenceSource],
        *,
        available_attractions=None,
        hotel_candidates=None,
    ) -> TripPlan:
        """Apply the optional LLM review to an already-built deterministic plan."""

        if self.agent is None:
            self.last_warning = "LLM 未启用，已跳过 PlannerAgent 软审查"
            curate_user_warnings(plan)
            return plan

        try:
            review_payload = self._build_review_payload(
                request,
                plan,
                weather_result,
                evidence,
                available_attractions=available_attractions or [],
            )
            review_input = json.dumps(review_payload, ensure_ascii=False)
            logger.info(
                "[PlannerAgent] LLM review input_chars=%d",
                len(review_input),
            )
            review_started_at = perf_counter()
            with self._run_lock:
                raw_result = run_stateless_agent(
                    self.agent,
                    review_input,
                )
            logger.info(
                "[PlannerAgent] LLM review elapsed_ms=%.1f",
                (perf_counter() - review_started_at) * 1000,
            )
            try:
                evaluation = parse_agent_result(
                    raw_result,
                    ExperienceEvaluation,
                )
                for issue in evaluation.issues:
                    issue.source = "llm"
                evaluation.source = "llm"
                logger.info(
                    "[PlannerAgent] structured evaluation pass=%s score=%.1f issues=%d",
                    evaluation.passed,
                    evaluation.overall_score,
                    len(evaluation.issues),
                )
                if hasattr(self.plan_builder, "run_quality_loop"):
                    def evaluator(candidate_plan):
                        return self.evaluate_experience(
                            request,
                            candidate_plan,
                            weather_result,
                            evidence,
                            available_attractions=available_attractions or [],
                        )

                    plan = self.plan_builder.run_quality_loop(
                        plan,
                        request,
                        available_attractions or [],
                        hotel_candidates=hotel_candidates or [],
                        external_evaluator=evaluator,
                        initial_external_evaluation=evaluation,
                    )
                else:
                    plan.quality_evaluation = evaluation
                    plan.quality_gate_passed = evaluation.passed
                    plan.unresolved_quality_issues = [
                        item
                        for item in evaluation.issues
                        if item.severity in {"critical", "high"}
                    ]
                plan.risk_warnings = sorted(
                    set(
                        [
                            *plan.risk_warnings,
                            *[
                                f"ExperienceEvaluator: {item.evidence}"
                                for item in evaluation.issues
                            ],
                        ]
                    )
                )
            except ValidationError as exc:
                if self._has_unknown_strategy(exc):
                    evaluation = ExperienceEvaluation(
                        **{
                            "pass": True,
                            "overall_score": (
                                plan.quality_evaluation.overall_score
                                if plan.quality_evaluation
                                else 10
                            ),
                            "issues": [],
                            "source": "llm",
                            "contract_errors": [
                                "Experience reviewer contract error: "
                                + self._contract_error_message(exc)
                            ],
                        }
                    )
                    plan.quality_evaluation = evaluation
                    plan.risk_warnings = sorted(
                        set([*plan.risk_warnings, *evaluation.contract_errors])
                    )
                    logger.error(evaluation.contract_errors[0])
                else:
                    review = parse_agent_result(raw_result, PlannerReviewResult)
                    self._apply_review(plan, review, evidence)
            except Exception:
                # Backward compatibility for previously configured reviewer
                # prompts. New production prompts always use the structured
                # ExperienceEvaluation contract above.
                review = parse_agent_result(raw_result, PlannerReviewResult)
                self._apply_review(plan, review, evidence)
            self.last_warning = ""
        except Exception as exc:
            # The deterministic plan is already complete. LLM review failure
            # must not turn a valid itinerary into an API failure.
            self.last_warning = f"PlannerAgent 软审查失败：{exc}"
        if settings.enable_context_governance:
            # Final output is deterministic today; retain its governed view so
            # an optional future explainer cannot receive L2 internals.
            final_context = self.context_builder.build(
                role=ContextRole.FINAL_EXPLAINER,
                request=request,
                plan=plan,
                evidence=evidence,
                weather_risks=weather_result.risk_summary,
                run_id=(plan.observability_trace.run_id if plan.observability_trace else ""),
            )
            plan.context_traces.append(
                self.context_builder.trace(
                    final_context,
                    node_name="final_explainer",
                    run_id=(plan.observability_trace.run_id if plan.observability_trace else ""),
                    model="deterministic",
                )
            )
        curate_user_warnings(plan)
        refresh_planning_trace(plan)
        return plan

    def evaluate_experience(
        self,
        request: TripRequest,
        plan: TripPlan,
        weather_result: WeatherQueryResult,
        evidence: List[EvidenceSource],
        *,
        available_attractions=None,
    ) -> ExperienceEvaluation | None:
        if self.agent is None:
            return None
        payload = self._build_review_payload(
            request,
            plan,
            weather_result,
            evidence,
            available_attractions=available_attractions or [],
        )
        with self._run_lock:
            raw_result = run_stateless_agent(
                self.agent,
                json.dumps(payload, ensure_ascii=False),
            )
        try:
            evaluation = parse_agent_result(raw_result, ExperienceEvaluation)
        except ValidationError as exc:
            return ExperienceEvaluation(
                **{
                    "pass": True,
                    "overall_score": plan.quality_evaluation.overall_score
                    if plan.quality_evaluation
                    else 10,
                    "issues": [],
                    "source": "llm",
                    "contract_errors": [
                        "Experience reviewer contract error: "
                        + self._contract_error_message(exc)
                    ],
                }
            )
        evaluation.source = "llm"
        for issue in evaluation.issues:
            issue.source = "llm"
        return evaluation

    def _build_review_payload(
        self,
        request: TripRequest,
        plan: TripPlan,
        weather_result: WeatherQueryResult,
        evidence: List[EvidenceSource],
        *,
        available_attractions,
    ) -> dict:
        """Build role-governed Experience Evaluator input from a single view."""

        if settings.enable_context_governance:
            return self._build_governed_review_payload(
                request, plan, weather_result, evidence,
                available_attractions=available_attractions,
            )

        """Legacy payload retained only for governance A/B comparison."""
        assigned = {
            item.visit_key or item.poi_id or item.name
            for day in plan.days
            for item in day.attractions
        }
        remaining = [item for item in available_attractions if (item.visit_key or item.poi_id or item.name) not in assigned]
        gate = getattr(self.plan_builder, "completeness_gate", ItineraryCompletenessGate())
        return {
            "request": request.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json", exclude={"observability_trace"}),
            "hotels": [day.hotel.model_dump(mode="json") for day in plan.days if day.hotel is not None],
            "observability": {}, "weather_risks": weather_result.risk_summary,
            "review_context": {"previous_issues": [], "previous_attempts": [], "available_candidate_categories": sorted({item.category or "general" for item in remaining}), "indoor_candidates_count": sum(gate._is_indoor(item) for item in remaining), "remaining_candidate_count": len(remaining)},
            "evidence": [{"title": item.title, "source": item.source, "snippet": item.snippet} for item in evidence],
        }

    def _build_governed_review_payload(
        self,
        request: TripRequest,
        plan: TripPlan,
        weather_result: WeatherQueryResult,
        evidence: List[EvidenceSource],
        *,
        available_attractions,
    ) -> dict:
        knowledge_summary = []
        if settings.enable_travel_knowledge:
            for item in available_attractions:
                knowledge = self.travel_knowledge.resolve_poi(request.city, item)
                if knowledge is None:
                    continue
                knowledge_summary.append(
                    {
                        "poi_key": item.visit_key or item.poi_id or item.name,
                        "name": item.name,
                        "environment": knowledge.environment_type.value,
                        "walking_intensity": knowledge.walking_intensity.value,
                        "planning_zone": knowledge.planning_zone,
                        "traveler_tags": knowledge.traveler_tags,
                        "weather_fit": knowledge.weather_fit,
                        "confidence": knowledge.confidence,
                        "knowledge_id": knowledge.knowledge_id,
                    }
                )
            for item in self.travel_knowledge.query_scope_for_request(
                request, weather_risks=weather_result.risk_summary
            )[:3]:
                knowledge_summary.append(
                    {
                        "knowledge_id": item.knowledge_id,
                        "claim_type": item.claim_type.value,
                        "statement": item.statement,
                        "confidence": item.confidence,
                        "source": item.evidence_ids,
                    }
                )
        governed = self.context_builder.build(
            role=ContextRole.EXPERIENCE_EVALUATOR,
            request=request,
            plan=plan,
            candidates=available_attractions,
            evidence=evidence,
            weather_risks=weather_result.risk_summary,
            knowledge_summary=knowledge_summary,
            run_id=(plan.observability_trace.run_id if plan.observability_trace else ""),
        )
        plan.context_traces.append(
            self.context_builder.trace(
                governed,
                node_name="experience_evaluator",
                run_id=(plan.observability_trace.run_id if plan.observability_trace else ""),
                model=getattr(getattr(self.agent, "llm", None), "model", ""),
            )
        )
        # The compatibility envelope preserves the established prompt field
        # names, while every value originates in the governed context view.
        payload = governed.payload
        return {
            "request": {**payload["request"], "hard_constraints": payload["hard_constraints"]},
            "plan": {
                "days": payload["plan_summary"]["days"],
                "constraint_report": payload["validation"],
                "budget": {"total": payload["plan_summary"]["budget_total"]},
                "plan_version": payload["plan_summary"]["version"],
            },
            "hotels": [
                {"name": day.hotel.name, "type": day.hotel.type}
                for day in plan.days if day.hotel is not None
            ],
            "observability": {
                "candidate_pois": payload.get("candidates", []),
                "validation_result": payload["validation"],
            },
            "weather_risks": payload.get("weather", []),
            "review_context": {
                "previous_attempts": payload.get("repair_history", []),
                "available_candidate_categories": sorted({item.category or "general" for item in available_attractions}),
                "remaining_candidate_count": len(available_attractions),
                "instruction": "Do not repeat a failed issue fingerprint and repair strategy without new evidence.",
            },
            "evidence": payload.get("evidence", []),
            "knowledge_summary": payload.get("knowledge_summary", []),
            "context_governance": governed.model_dump(mode="json", exclude={"payload"}),
        }

        assigned = {
            item.visit_key or item.poi_id or item.name
            for day in plan.days
            for item in day.attractions
        }
        remaining = [
            item
            for item in available_attractions
            if (item.visit_key or item.poi_id or item.name) not in assigned
        ]
        gate = getattr(
            self.plan_builder,
            "completeness_gate",
            ItineraryCompletenessGate(),
        )
        return {
            "request": request.model_dump(mode="json"),
            "plan": plan.model_dump(
                mode="json",
                exclude={"observability_trace"},
            ),
            "hotels": [
                day.hotel.model_dump(mode="json")
                for day in plan.days
                if day.hotel is not None
            ],
            "observability": {
                "candidate_pois": (
                    [
                        item.model_dump(mode="json")
                        for item in plan.observability_trace.candidate_pois
                    ]
                    if plan.observability_trace
                    else []
                ),
                "validation_result": (
                    plan.observability_trace.validation_result.model_dump(
                        mode="json"
                    )
                    if plan.observability_trace
                    else {}
                ),
            },
            "weather_risks": weather_result.risk_summary,
            "review_context": {
                "previous_issues": [
                    item.model_dump(mode="json")
                    for repair in plan.repair_history
                    for item in repair.issues_before
                ],
                "previous_attempts": [
                    {
                        "issue_fingerprint": repair.issue_fingerprint,
                        "repair_action": repair.selected_action,
                        "repair_result": repair.action_result,
                        "accepted": repair.accepted,
                        "failure_reason": repair.rejection_reason,
                    }
                    for repair in plan.repair_history
                ],
                "available_candidate_categories": sorted(
                    {
                        category
                        for item in remaining
                        for category in (
                            item.categories or [item.category or "general"]
                        )
                    }
                ),
                "indoor_candidates_count": sum(
                    gate._is_indoor(item) for item in remaining
                ),
                "remaining_candidate_count": len(remaining),
                "instruction": (
                    "Do not repeat a failed issue fingerprint and repair "
                    "strategy unless the candidate evidence changed."
                ),
            },
            "evidence": [
                {
                    "title": item.title,
                    "source": item.source,
                    "snippet": item.snippet,
                }
                for item in evidence
            ],
        }

    @staticmethod
    def _contract_error_message(exc: ValidationError) -> str:
        errors = exc.errors(include_url=False)
        unknown = [
            item for item in errors
            if tuple(item.get("loc", ()))[-1:] == ("repair_strategy",)
        ]
        if unknown:
            value = unknown[0].get("input")
            return f"unsupported repair_strategy={value!r}"
        return str(exc)

    @staticmethod
    def _has_unknown_strategy(exc: ValidationError) -> bool:
        return any(
            tuple(item.get("loc", ()))[-1:] == ("repair_strategy",)
            for item in exc.errors(include_url=False)
        )

    def _select_hotel(
        self,
        request: TripRequest,
        result: HotelSearchResult,
    ) -> Hotel:
        if result.recommended_hotel is not None:
            return result.recommended_hotel
        if result.candidates:
            return result.candidates[0]
        return Hotel(
            name=f"{request.hotel_area or request.city}待确认酒店",
            type=request.accommodation,
        )

    def _apply_review(
        self,
        plan: TripPlan,
        review: PlannerReviewResult,
        evidence: List[EvidenceSource],
    ) -> None:
        if review.summary.strip():
            plan.overall_suggestions = review.summary.strip()

        valid_sources = {item.source for item in evidence if item.source}
        warnings = list(plan.risk_warnings)
        for warning in review.soft_warnings:
            if warning.basis == "rag":
                if not warning.evidence_source or warning.evidence_source not in valid_sources:
                    continue
                warnings.append(f"{warning.message}（来源：{warning.evidence_source}）")
            else:
                warnings.append(warning.message)
        plan.risk_warnings = sorted(set(warnings))
