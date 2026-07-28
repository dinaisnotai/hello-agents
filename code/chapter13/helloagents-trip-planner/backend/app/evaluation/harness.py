"""Evaluation runner and deterministic criteria for generated itineraries."""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterable

from ..models.schemas import Attraction, TripPlan
from ..services.attraction_scorer import AttractionScorer
from ..services.place_name_service import place_names_match
from .schemas import (
    CriterionResult,
    EvaluationCase,
    EvaluationReport,
    EvaluationResult,
)


def load_evaluation_cases(path: str | Path) -> list[EvaluationCase]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Evaluation dataset must be a JSON array")
    return [EvaluationCase.model_validate(item) for item in payload]


class EvaluationHarness:
    """Run a planner callable against cases and evaluate every output."""

    def __init__(self, planner: Callable):
        self.planner = planner

    def run(self, cases: Iterable[EvaluationCase]) -> EvaluationReport:
        results = []
        for case in cases:
            started = perf_counter()
            plan = self.planner(case.input_request)
            latency = perf_counter() - started
            result = evaluate_plan(case, plan)
            result.latency_seconds = round(latency, 4)
            results.append(result)
        passed = sum(item.passed for item in results)
        return EvaluationReport(
            total_cases=len(results),
            passed_cases=passed,
            pass_rate=round(passed / max(1, len(results)), 4),
            results=results,
        )


def evaluate_plan(case: EvaluationCase, plan: TripPlan) -> EvaluationResult:
    criteria: list[CriterionResult] = []
    request = case.input_request
    attractions = [
        item
        for day in plan.days
        for item in day.attractions
    ]

    constraints = case.constraints
    if constraints.walking_limit_km is not None:
        maximum = max(
            (day.daily_walking_distance_km for day in plan.days),
            default=0,
        )
        criteria.append(
            _criterion(
                "walking_limit",
                maximum <= constraints.walking_limit_km,
                round(maximum, 2),
                constraints.walking_limit_km,
                severity="constraint",
            )
        )
    if constraints.avoid_hiking:
        offenders = [
            item.name
            for item in attractions
            if item.intensity_level == "high"
            or _contains_any(item, {"hiking", "climbing", "徒步", "登山"})
        ]
        criteria.append(
            _criterion(
                "avoid_hiking",
                not offenders,
                offenders,
                [],
                severity="constraint",
            )
        )
    if constraints.end_time:
        late_days = [
            day.day_index + 1
            for day in plan.days
            if not day.planned_end_time
            or _clock_minutes(day.planned_end_time)
            > _clock_minutes(constraints.end_time)
        ]
        criteria.append(
            _criterion(
                "daily_end_time",
                not late_days,
                late_days,
                constraints.end_time,
                severity="constraint",
            )
        )
    budget_limit = constraints.budget_limit
    if budget_limit is not None:
        actual = plan.budget.total if plan.budget else None
        criteria.append(
            _criterion(
                "budget_limit",
                actual is not None and actual <= budget_limit,
                actual,
                budget_limit,
                severity="constraint",
            )
        )
    for required in constraints.must_visit:
        found = any(place_names_match(required, item.name) for item in attractions)
        criteria.append(
            _criterion(
                f"must_visit:{required}",
                found,
                found,
                required,
                severity="constraint",
            )
        )

    quality = case.quality_expectations
    if quality.low_transport_time:
        average = (
            sum(day.daily_travel_minutes for day in plan.days)
            / max(1, len(plan.days))
        )
        criteria.append(
            _criterion(
                "low_transport_time",
                average <= quality.max_average_transport_minutes,
                round(average, 1),
                quality.max_average_transport_minutes,
            )
        )
    if quality.relaxed_pace:
        maximum_pois = max(
            (len(day.attractions) for day in plan.days),
            default=0,
        )
        maximum_utilization = max(
            (day.day_utilization_score for day in plan.days),
            default=0,
        )
        criteria.append(
            _criterion(
                "relaxed_pace",
                request.pace == "relaxed"
                and maximum_pois <= 3
                and maximum_utilization <= 100,
                {
                    "pace": request.pace,
                    "max_pois_per_day": maximum_pois,
                    "max_utilization": maximum_utilization,
                },
                {
                    "pace": "relaxed",
                    "max_pois_per_day": 3,
                    "max_utilization": 100,
                },
            )
        )
    categories = {
        AttractionScorer.category_bucket(item)
        for item in attractions
    }
    if quality.category_diversity:
        required_categories = min(
            quality.min_categories,
            max(1, len(attractions)),
        )
        criteria.append(
            _criterion(
                "category_diversity",
                len(categories) >= required_categories,
                sorted(categories),
                required_categories,
            )
        )
    if quality.preference_alignment:
        aligned = [
            item.name
            for item in attractions
            if item.score_breakdown.get("preference_match", 0) > 0
            or any(
                preference.lower()
                in " ".join(
                    [
                        item.category or "",
                        *item.categories,
                        *item.tags,
                    ]
                ).lower()
                for preference in request.preferences
            )
        ]
        criteria.append(
            _criterion(
                "preference_alignment",
                bool(aligned),
                aligned,
                request.preferences,
            )
        )
    if quality.family_friendly:
        friendly = [
            item.name
            for item in attractions
            if item.intensity_level != "high"
            and _contains_any(
                item,
                {
                    "amusement",
                    "zoo",
                    "park",
                    "interactive_activity",
                    "family",
                },
            )
        ]
        criteria.append(
            _criterion(
                "family_friendly",
                bool(friendly),
                friendly,
                "at least one low/medium intensity family POI",
            )
        )
    if quality.within_budget:
        limit = constraints.budget_limit or request.budget_limit
        actual = plan.budget.total if plan.budget else None
        criteria.append(
            _criterion(
                "within_budget",
                limit is not None and actual is not None and actual <= limit,
                actual,
                limit,
            )
        )

    constraint_criteria = [
        item for item in criteria if item.severity == "constraint"
    ]
    quality_criteria = [
        item for item in criteria if item.severity == "quality"
    ]
    constraint_passed = all(item.passed for item in constraint_criteria)
    quality_passed = all(item.passed for item in quality_criteria)
    metrics = {
        "selected_pois": len(attractions),
        "categories": len(categories),
        "total_travel_minutes": sum(
            day.daily_travel_minutes for day in plan.days
        ),
        "max_daily_walking_km": round(
            max(
                (day.daily_walking_distance_km for day in plan.days),
                default=0,
            ),
            2,
        ),
        "planner_constraint_score": plan.constraint_report.score,
    }
    return EvaluationResult(
        case_name=case.name,
        passed=(
            constraint_passed
            and quality_passed
            and plan.constraint_report.passed
        ),
        constraint_passed=constraint_passed,
        quality_passed=quality_passed,
        planner_constraint_passed=plan.constraint_report.passed,
        criteria=criteria,
        metrics=metrics,
    )


def _criterion(
    name: str,
    passed: bool,
    actual,
    expected,
    *,
    severity: str = "quality",
) -> CriterionResult:
    return CriterionResult(
        name=name,
        passed=passed,
        actual=actual,
        expected=expected,
        severity=severity,
    )


def _contains_any(attraction: Attraction, terms: set[str]) -> bool:
    text = " ".join(
        [
            attraction.name,
            attraction.category or "",
            *attraction.categories,
            *attraction.tags,
        ]
    ).lower()
    return any(term.lower() in text for term in terms)


def _clock_minutes(value: str) -> int:
    value = value.replace("次日", "")
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)
