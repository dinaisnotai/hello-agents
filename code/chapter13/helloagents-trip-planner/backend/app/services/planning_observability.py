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
    PortfolioMetricsTrace,
    RoutePlanningTrace,
    UserRequirementTrace,
    ValidationTrace,
)
from ..models.schemas import Attraction, TripPlan, TripRequest
from .attraction_scorer import AttractionScorer
from .place_name_service import normalize_place_name
from .poi_identity_resolver import get_poi_identity_resolver


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
    reconcile_portfolio_state(plan, candidates, request)
    selected_keys = {
        item.visit_key or normalize_place_name(item.name)
        for day in plan.days
        for item in day.attractions
    }
    eligible_keys = (
        {
            item.visit_key or normalize_place_name(item.name)
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
        quality_gate=_quality_gate_trace(
            plan,
            request=request,
            candidate_count=len(candidates),
        ),
        portfolio_metrics=calculate_portfolio_metrics(
            plan,
            candidates,
            request,
        ),
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
        item.visit_key or normalize_place_name(item.name)
        for day in plan.days
        for item in day.attractions
    }
    for candidate in trace.candidate_pois:
        key = candidate.visit_key or normalize_place_name(candidate.name)
        if key in selected_keys:
            candidate.rejection_reason = None
        elif candidate.rejection_reason is None:
            candidate.rejection_reason = "not_selected_by_route_planner"
    trace.route_planning = _route_trace(plan)
    trace.validation_result = _validation_trace(plan)
    refreshed_quality = _quality_gate_trace(plan)
    refreshed_quality.input = {
        **trace.quality_gate.input,
        **{
            key: value
            for key, value in refreshed_quality.input.items()
            if value is not None
        },
    }
    trace.quality_gate = refreshed_quality
    trace.portfolio_metrics = calculate_portfolio_metrics(
        plan,
        [
            _trace_candidate_as_attraction(item)
            for item in trace.candidate_pois
        ],
        None,
    )
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
    key = candidate.visit_key or normalize_place_name(candidate.name)
    if key in selected_keys:
        rejection_reason = None
    elif eligible_keys is not None and key not in eligible_keys:
        rejection_reason = "planning_constraint_filter"
    else:
        rejection_reason = "not_selected_by_route_planner"
    if (
        key not in selected_keys
        and candidate.selection_reason.startswith("not_selected")
    ):
        rejection_reason = candidate.selection_reason
    breakdown = candidate.score_breakdown
    base_score = breakdown.get(
        "selection_base_score",
        breakdown.get("base_quality", candidate.score),
    )
    return CandidatePOITrace(
        name=candidate.name,
        visit_key=candidate.visit_key,
        landmark_tier=candidate.selection_role,
        final_selected=key in selected_keys,
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
        rejection_reason=(
            rejection_reason
            if rejection_reason is not None
            else None
        ),
        selection_role=candidate.selection_role,
        selection_reason=(
            candidate.selection_reason
            or (
                "selected_by_portfolio_and_route_planner"
                if key in selected_keys
                else rejection_reason or ""
            )
        ),
        selection_trace=list(candidate.selection_trace),
        score_breakdown=dict(candidate.score_breakdown),
    )


def reconcile_portfolio_state(
    plan: TripPlan,
    candidates: Sequence[Attraction],
    request: TripRequest | None,
) -> None:
    """Make roles, quota outcomes and trace reasons one final-state snapshot."""

    resolver = get_poi_identity_resolver()
    all_items = [*candidates, *(item for day in plan.days for item in day.attractions)]
    resolver.assign_visit_keys(all_items)
    candidate_by_key = {
        item.visit_key: item for item in candidates if item.visit_key
    }
    selected_keys = {
        item.visit_key for day in plan.days for item in day.attractions if item.visit_key
    }
    for day in plan.days:
        for item in day.attractions:
            source = candidate_by_key.get(item.visit_key)
            if source is not None:
                explicit_child = bool(request) and any(
                    normalize_place_name(required)
                    == normalize_place_name(item.name)
                    for required in request.must_visit
                )
                if not explicit_child and source.name != item.name:
                    # The candidate pool's canonical representative is the
                    # public itinerary entity; keep any child detail as a
                    # highlight in its description rather than a separate
                    # destination label.
                    child_name = item.name
                    item.name = source.name
                    if child_name not in item.description:
                        item.description = (
                            (item.description or "")
                            + f"；内部亮点：{child_name}"
                        ).strip("；")
                item.selection_role = source.selection_role
                item.is_core_landmark = source.is_core_landmark
                item.selection_trace = list(source.selection_trace)
            item.selection_reason = f"selected_final_{item.selection_role}"
            item.selection_trace.append(
                {"entry_point": "finalize", "accepted": True,
                 "reason": item.selection_reason}
            )
    for candidate in candidates:
        if candidate.visit_key in selected_keys:
            candidate.selection_reason = f"selected_final_{candidate.selection_role}"
        elif candidate.selection_reason.startswith((
            "core_landmark_quota", "major_attraction_quota",
        )):
            # Quota was an initial selection stage, not a final rejection.
            candidate.selection_reason = "rejected_final_route_or_repair"
        candidate.selection_trace.append(
            {
                "entry_point": "finalize",
                "accepted": candidate.visit_key in selected_keys,
                "reason": candidate.selection_reason
                or "rejected_final_route_or_repair",
            }
        )


def calculate_portfolio_metrics(
    plan: TripPlan,
    candidates: Sequence[Attraction],
    request: TripRequest | None,
) -> PortfolioMetricsTrace:
    selected = [item for day in plan.days for item in day.attractions]
    counts = Counter(item.selection_role for item in selected)
    total = len(selected)
    subcategories = Counter(
        AttractionScorer.category_bucket(item) for item in selected
    )
    repeated = sum(max(0, count - 1) for count in subcategories.values())
    preference_selected = sum(
        item.score_breakdown.get("preference_match", 0) > 0
        for item in selected
    )
    candidate_core = [item for item in candidates if item.is_core_landmark]
    selected_keys = {normalize_place_name(item.name) for item in selected}
    selected_core = sum(item.is_core_landmark for item in selected)
    niche = counts.get("niche_attraction", 0)
    landmark_coverage = (
        selected_core / len(candidate_core) if candidate_core else 1.0
    )
    niche_ratio = niche / total if total else 0
    repeated_ratio = repeated / total if total else 0
    preference_coverage = preference_selected / total if total else 0
    balance = max(
        0.0,
        min(
            1.0,
            0.45 * min(1.0, landmark_coverage)
            + 0.25 * (1.0 - min(1.0, niche_ratio))
            + 0.20 * (1.0 - min(1.0, repeated_ratio))
            + 0.10 * min(1.0, preference_coverage * 2),
        ),
    )
    displaced = [
        {
            "name": item.name,
            "selection_role": item.selection_role,
            "score": item.score,
            "reason": item.selection_reason
            or "not_selected_after_capacity_route_and_feasibility_tradeoff",
        }
        for item in candidates
        if item.selection_role in {"core_landmark", "major_attraction"}
        and normalize_place_name(item.name) not in selected_keys
    ]
    return PortfolioMetricsTrace(
        core_landmark_count=counts.get("core_landmark", 0),
        major_attraction_count=counts.get("major_attraction", 0),
        complementary_attraction_count=counts.get(
            "complementary_attraction", 0
        ),
        niche_attraction_count=niche,
        niche_ratio=round(niche_ratio, 3),
        repeated_subcategory_ratio=round(repeated_ratio, 3),
        preference_coverage=round(preference_coverage, 3),
        landmark_coverage=round(landmark_coverage, 3),
        portfolio_balance_score=round(balance * 100, 1),
        displaced_core_major=displaced,
    )


def _trace_candidate_as_attraction(item: CandidatePOITrace) -> Attraction:
    """Rehydrate the fields required to refresh portfolio metrics."""
    from ..models.schemas import Location

    return Attraction(
        name=item.name,
        location=Location(
            longitude=item.location.get("longitude", 0),
            latitude=item.location.get("latitude", 0),
        ),
        score=item.final_score,
        visit_key=item.visit_key,
        score_breakdown={
            key: value
            for key, value in item.score_breakdown.items()
            if isinstance(value, (int, float))
        },
        selection_role=item.selection_role,
        selection_reason=item.selection_reason,
        selection_trace=list(item.selection_trace),
        is_core_landmark=item.selection_role == "core_landmark",
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


def _quality_gate_trace(
    plan: TripPlan,
    *,
    request: TripRequest | None = None,
    candidate_count: int | None = None,
):
    from ..models.quality import QualityGateTrace

    evaluation = plan.quality_evaluation
    gate_input = {
        "days": len(plan.days),
        "daily_pois": [
            [item.name for item in day.attractions]
            for day in plan.days
        ],
        "constraint_pass": plan.constraint_report.passed,
        "candidate_count": candidate_count,
    }
    if request is not None:
        gate_input.update(
            {
                "arrival_time": request.arrival_time,
                "departure_time": request.departure_time,
                "first_visit": request.first_visit,
                "prefer_classic": request.prefer_classic,
            }
        )
    result = (
        evaluation.model_dump(mode="json", by_alias=True)
        if evaluation is not None
        else {
            "pass": plan.quality_gate_passed,
            "overall_score": None,
            "issues": [],
        }
    )
    reviewer_issue_models = [
        item
        for item in (evaluation.issues if evaluation else [])
        if item.source == "llm"
    ]
    for repair in plan.repair_history:
        reviewer_issue_models.extend(
            item
            for item in [
                repair.trigger_issue,
                *repair.issues_before,
                *repair.issues_after,
            ]
            if item.source == "llm"
        )
    reviewer_issues = []
    seen_reviewer_issues = set()
    for item in reviewer_issue_models:
        key = (
            item.issue_type,
            item.day,
            item.evidence,
        )
        if key in seen_reviewer_issues:
            continue
        seen_reviewer_issues.add(key)
        reviewer_issues.append(item.model_dump(mode="json"))
    return QualityGateTrace(
        input=gate_input,
        result=result,
        reviewer_issues=reviewer_issues,
        contract_errors=(
            list(evaluation.contract_errors)
            if evaluation is not None
            else []
        ),
        repair_iterations=[
            item.model_dump(mode="json")
            for item in plan.repair_history
        ],
        final_reason=(
            "quality_gate_passed"
            if plan.quality_gate_passed
            else plan.failure_reason or "quality_gate_failed"
        ),
    )
