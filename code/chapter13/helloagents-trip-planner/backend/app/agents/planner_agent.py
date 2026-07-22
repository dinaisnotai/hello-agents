"""Hybrid planner Agent: deterministic schedule plus LLM soft review."""

from __future__ import annotations

import json
from typing import List, Optional

from hello_agents import HelloAgentsLLM, SimpleAgent

from ..models.agent_outputs import (
    AttractionSearchResult,
    HotelSearchResult,
    PlannerReviewResult,
    WeatherQueryResult,
)
from ..models.schemas import EvidenceSource, Hotel, TripPlan, TripRequest
from .agent_utils import parse_agent_result
from .prompts import PLANNER_PROMPT
from .trip_planner_agent import MultiAgentTripPlanner


class PlannerAgent:
    """Integrate specialist outputs without delegating arithmetic to the LLM."""

    def __init__(
        self,
        llm: Optional[HelloAgentsLLM],
        plan_builder: Optional[MultiAgentTripPlanner] = None,
    ):
        self.plan_builder = plan_builder or MultiAgentTripPlanner()
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

        if self.agent is None:
            self.last_warning = "LLM 未启用，已跳过 PlannerAgent 软审查"
            return plan

        try:
            raw_result = self.agent.run(
                json.dumps(
                    {
                        "request": request.model_dump(mode="json"),
                        "plan": plan.model_dump(mode="json"),
                        "weather_risks": weather_result.risk_summary,
                        "evidence": [item.model_dump(mode="json") for item in evidence],
                    },
                    ensure_ascii=False,
                )
            )
            review = parse_agent_result(raw_result, PlannerReviewResult)
            self._apply_review(plan, review, evidence)
            self.last_warning = ""
        except Exception as exc:
            # The deterministic plan is already complete. LLM review failure
            # must not turn a valid itinerary into an API failure.
            self.last_warning = f"PlannerAgent 软审查失败：{exc}"
        return plan

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
