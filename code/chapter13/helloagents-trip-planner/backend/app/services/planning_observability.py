"""Build and emit the explanation trace for a travel-planning run."""

from __future__ import annotations

from collections import Counter
import json
import logging
from typing import Iterable, Sequence
from uuid import uuid4

from ..models.observability import (
    CandidatePOITrace,
    DailyScheduleTrace,
    PlanningRunTrace,
    RoutePlanningTrace,
    UserRequirementTrace,
    ValidationTrace,
)
from ..models.schemas import Attraction, TripPlan, TripRequest
from .attraction_scorer import AttractionScorer
from .place_name_service import normalize_place_name


logger = logging.getLogger("uvicorn.error")


def build_planning_trace(
    request: TripRequest,
    plan: TripPlan,
    *,
    candidates: Sequence[Attraction] = (),
    eligible_candidates: Sequence[Attraction] | None = None,
    run_id: str | None = None,
    run_type: str = "planning",
) -> PlanningRunTrace:
    selected_keys = {
        normalize_place_name(item.name)
        for day in plan.days
        for item in day.attractions
    }
    eligible_keys = (
        {
            normalize_place_name(item.name)
            for item in eligible_candidates
        }
        if eligible_candidates is not None
        else None
    )
    candidate_traces = [
        _candidate_trace(
            candidate,
            selected_keys=selected_keys,
            eligible_keys=eligible_keys,
        )
        for candidate in sorted(
            candidates,
            key=lambda item: (-item.score, normalize_place_name(item.name)),
        )
    ]
    trace = PlanningRunTrace(
        run_id=run_id or uuid4().hex,
        run_type=run_type,
        user_requirement=_requirement_trace(request),
        candidate_pois=candidate_traces,
        route_planning=_route_trace(plan),
        validation_result=_validation_trace(plan),
    )
    return trace


def refresh_planning_trace(
    plan: TripPlan,
    *,
    run_id: str | None = None,
    run_type: str | None = None,
) -> PlanningRunTrace | None:
    """Refresh mutable route/review outcomes while preserving candidate evidence."""

    trace = plan.observability_trace
    if trace is None:
        return None
    if run_id:
        trace.run_id = run_id
    if run_type:
        trace.run_type = run_type
    selected_keys = {
        normalize_place_name(item.name)
        for day in plan.days
        for item in day.attractions
    }
    for candidate in trace.candidate_pois:
        key = normalize_place_name(candidate.name)
        if key in selected_keys:
            candidate.rejection_reason = None
        elif candidate.rejection_reason is None:
            candidate.rejection_reason = "not_selected_by_route_planner"
    trace.route_planning = _route_trace(plan)
    trace.validation_result = _validation_trace(plan)
    return trace


def emit_planning_trace(trace: PlanningRunTrace | None) -> None:
    """Emit one compact JSON log record suitable for log aggregation."""

    if trace is None:
        return
    logger.info(
        json.dumps(
            trace.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _requirement_trace(request: TripRequest) -> UserRequirementTrace:
    return UserRequirementTrace(
        age_group=_age_group(request.travelers),
        budget=request.budget_limit,
        duration=request.travel_days,
        location=request.city,
        preferences=list(request.preferences),
        hard_constraints=list(request.hard_constraints),
        soft_preferences=list(request.soft_preferences),
    )


def _age_group(travelers: Iterable[str]) -> str:
    text = " ".join(travelers).lower()
    if any(term in text for term in ("elderly", "senior", "老人", "老年")):
        return "elderly"
    if any(term in text for term in ("child", "children", "kid", "儿童", "孩子")):
        return "children"
    if text:
        return "adult_or_mixed"
    return "unspecified"


def _candidate_trace(
    candidate: Attraction,
    *,
    selected_keys: set[str],
    eligible_keys: set[str] | None,
) -> CandidatePOITrace:
    key = normalize_place_name(candidate.name)
    if key in selected_keys:
        rejection_reason = None
    elif eligible_keys is not None and key not in eligible_keys:
        rejection_reason = "planning_constraint_filter"
    else:
        rejection_reason = "not_selected_by_route_planner"
    breakdown = candidate.score_breakdown
    base_score = breakdown.get(
        "selection_base_score",
        breakdown.get("base_quality", candidate.score),
    )
    return CandidatePOITrace(
        name=candidate.name,
        category=AttractionScorer.category_bucket(candidate),
        location={
            "longitude": candidate.location.longitude,
            "latitude": candidate.location.latitude,
        },
        base_score=round(float(base_score), 2),
        preference_score=round(
            float(
                breakdown.get(
                    "preference_score",
                    breakdown.get("pref_bonus", 0),
                )
            ),
            2,
        ),
        final_score=round(candidate.score, 2),
        rejection_reason=rejection_reason,
    )


def _route_trace(plan: TripPlan) -> RoutePlanningTrace:
    attractions = [
        item
        for day in plan.days
        for item in day.attractions
    ]
    categories = Counter(
        AttractionScorer.category_bucket(item)
        for item in attractions
    )
    return RoutePlanningTrace(
        selected_pois=[item.name for item in attractions],
        travel_time=sum(day.daily_travel_minutes for day in plan.days),
        walking_distance=round(
            sum(day.daily_walking_distance_km for day in plan.days),
            2,
        ),
        daily_schedule=[
            DailyScheduleTrace(
                day=day.day_index + 1,
                date=day.date,
                pois=[item.name for item in day.attractions],
                planned_start_time=day.planned_start_time,
                planned_end_time=day.planned_end_time,
                travel_time=day.daily_travel_minutes,
                walking_distance=day.daily_walking_distance_km,
            )
            for day in plan.days
        ],
        category_distribution=dict(sorted(categories.items())),
    )


def _validation_trace(plan: TripPlan) -> ValidationTrace:
    violations = [
        item.model_dump(mode="json")
        for item in plan.validation_result.violations
    ]
    if not violations:
        violations = [
            item.model_dump(mode="json")
            for item in plan.constraint_report.items
            if not item.passed
        ]
    warning_items = [
        item.message or item.name
        for item in plan.constraint_report.items
        if item.severity == "warning"
    ]
    return ValidationTrace(
        constraint_pass=(
            plan.constraint_report.passed
            and plan.validation_result.valid
        ),
        violations=violations,
        warnings=sorted(
            set(
                [
                    *plan.risk_warnings,
                    *plan.review_scores.warnings,
                    *warning_items,
                ]
            )
        ),
    )
