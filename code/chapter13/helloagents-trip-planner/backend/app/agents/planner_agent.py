"""Hybrid planner Agent: deterministic schedule plus LLM soft review."""

from __future__ import annotations

import json
import logging
from threading import Lock
from time import perf_counter
from typing import List, Optional

from hello_agents import HelloAgentsLLM, SimpleAgent

from ..models.agent_outputs import (
    AttractionSearchResult,
    HotelSearchResult,
    PlannerReviewResult,
    WeatherQueryResult,
)
from ..models.schemas import EvidenceSource, Hotel, TripPlan, TripRequest
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
        hotel = self._select_hotel(request, hotel_result)
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
        return self.review_plan(request, plan, weather_result, evidence)

    def review_plan(
        self,
        request: TripRequest,
        plan: TripPlan,
        weather_result: WeatherQueryResult,
        evidence: List[EvidenceSource],
    ) -> TripPlan:
        """Apply the optional LLM review to an already-built deterministic plan."""

        if self.agent is None:
            self.last_warning = "LLM 未启用，已跳过 PlannerAgent 软审查"
            return plan

        try:
            review_payload = self._build_review_payload(
                request,
                plan,
                weather_result,
                evidence,
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
            review = parse_agent_result(raw_result, PlannerReviewResult)
            logger.info(
                "[PlannerAgent] LLM review output summary=%s soft_warnings=%d",
                review.summary.strip()[:300] or "(empty)",
                len(review.soft_warnings),
            )
            self._apply_review(plan, review, evidence)
            self.last_warning = ""
        except Exception as exc:
            # The deterministic plan is already complete. LLM review failure
            # must not turn a valid itinerary into an API failure.
            self.last_warning = f"PlannerAgent 软审查失败：{exc}"
        return plan

    def _build_review_payload(
        self,
        request: TripRequest,
        plan: TripPlan,
        weather_result: WeatherQueryResult,
        evidence: List[EvidenceSource],
    ) -> dict:
        """Send only review-relevant facts, not coordinates/photos/full route steps."""

        return {
            "request": request.model_dump(mode="json"),
            "plan": {
                "city": plan.city,
                "start_date": plan.start_date,
                "end_date": plan.end_date,
                "days": [
                    {
                        "date": day.date,
                        "transportation": day.transportation,
                        "attractions": [
                            {
                                "name": attraction.name,
                                "category": attraction.category,
                                "visit_duration": attraction.visit_duration,
                            }
                            for attraction in day.attractions
                        ],
                        "routes": [
                            {
                                "origin": segment.origin,
                                "destination": segment.destination,
                                "route_type": segment.route_type,
                                "duration_minutes": segment.duration_minutes,
                                "walking_distance_meters": segment.walking_distance_meters,
                                "walking_duration_minutes": segment.walking_duration_minutes,
                                "transit_duration_minutes": segment.transit_duration_minutes,
                            }
                            for segment in day.route_segments
                        ],
                        "daily_distance_km": day.daily_distance_km,
                        "daily_walking_distance_km": day.daily_walking_distance_km,
                        "daily_visit_minutes": day.daily_visit_minutes,
                        "daily_travel_minutes": day.daily_travel_minutes,
                        "daily_duration_minutes": day.daily_duration_minutes,
                    }
                    for day in plan.days
                ],
                "budget": plan.budget.model_dump(mode="json") if plan.budget else None,
                "constraint_report": plan.constraint_report.model_dump(mode="json"),
                "risk_warnings": plan.risk_warnings,
            },
            "weather_risks": weather_result.risk_summary,
            "evidence": [
                {
                    "title": item.title,
                    "source": item.source,
                    "snippet": item.snippet,
                }
                for item in evidence
            ],
        }

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
