"""Evaluation runner and deterministic criteria for generated itineraries."""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterable

from ..models.schemas import Attraction, TripPlan
from ..services.attraction_scorer import AttractionScorer
from ..services.place_name_service import place_names_match
from ..services.poi_identity_resolver import POIIdentityResolver
from ..services.travel_knowledge_service import get_travel_knowledge_service
from ..config import settings
from ..services.planning_observability import calculate_portfolio_metrics
from .schemas import (
    ABEvaluationReport,
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
            context_traces = plan.context_traces
            if context_traces:
                result.metrics.update(
                    {
                        "context_governance_enabled": True,
                        "estimated_input_tokens": sum(item.estimated_tokens_after for item in context_traces),
                        "context_section_count": sum(len(item.included_sections) for item in context_traces),
                        "truncated_section_count": sum(len(item.truncated_sections) for item in context_traces),
                    }
                )
            else:
                result.metrics["context_governance_enabled"] = False
            results.append(result)
        passed = sum(item.passed for item in results)
        return EvaluationReport(
            total_cases=len(results),
            passed_cases=passed,
            pass_rate=round(passed / max(1, len(results)), 4),
            results=results,
        )

    def run_ab(
        self,
        cases: Iterable[EvaluationCase],
        enabled_planner: Callable,
    ) -> ABEvaluationReport:
        """Run the same cases with the baseline and a knowledge-enabled planner."""
        frozen_cases = list(cases)
        baseline = self.run(frozen_cases)
        enabled = EvaluationHarness(enabled_planner).run(frozen_cases)
        metric_deltas: dict[str, float] = {}
        for name in ("planner_constraint_score", "final_experience_score", "repair_count", "rollback_rate"):
            left = [float(item.metrics[name]) for item in baseline.results if name in item.metrics]
            right = [float(item.metrics[name]) for item in enabled.results if name in item.metrics]
            if left and right:
                metric_deltas[name] = round(sum(right) / len(right) - sum(left) / len(left), 4)
        hard_regression = enabled.passed_cases < baseline.passed_cases and any(
            item.constraint_passed is False for item in enabled.results
        )
        return ABEvaluationReport(
            baseline=baseline,
            enabled=enabled,
            metric_deltas=metric_deltas,
            hard_regression=hard_regression,
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
    if quality.no_duplicate_visits:
        resolver = POIIdentityResolver()
        resolver.assign_visit_keys(attractions)
        keys = [item.visit_key for item in attractions]
        duplicates = sorted(
            {key for key in keys if key and keys.count(key) > 1}
        )
        criteria.append(
            _criterion(
                "no_duplicate_visits",
                not duplicates,
                duplicates,
                [],
            )
        )
    for group in quality.exclusive_poi_groups:
        matches = [
            item.name
            for item in attractions
            if any(place_names_match(name, item.name) for name in group)
        ]
        criteria.append(
            _criterion(
                "exclusive_poi_group:" + "|".join(group),
                len(matches) == 1,
                matches,
                "exactly one canonical visit",
            )
        )
    if quality.core_landmark_coverage:
        core = [item.name for item in attractions if item.is_core_landmark]
        criteria.append(
            _criterion(
                "core_landmark_coverage",
                len(core) >= quality.min_core_landmarks,
                core,
                quality.min_core_landmarks,
            )
        )

    if quality.elderly_friendliness:
        elderly = [
            item.name
            for item in attractions
            if item.intensity_level != "high"
            and (item.accessible is True or item.estimated_internal_walking_km <= 1.5)
        ]
        criteria.append(
            _criterion(
                "elderly_friendliness",
                bool(elderly),
                elderly,
                "at least one low/medium intensity accessible or low-walk POI",
            )
        )
    if quality.weather_suitability:
        risky_days = []
        for day in plan.days:
            weather = next((item for item in plan.weather_info if item.date == day.date), None)
            condition = f"{weather.day_weather} {weather.night_weather}" if weather else ""
            if any(token in condition for token in ("雨", "雷", "高温", "炎热", "rain", "hot")):
                risky_days.append(day)
        weather_ok = all(day.weather_backup or day.weather_warning or not day.attractions for day in risky_days)
        criteria.append(_criterion("weather_suitability", weather_ok, len(risky_days), "risky days have backup or warning"))
    if quality.traveler_fit:
        traveler_text = " ".join(request.travelers).lower()
        family = any(token in traveler_text for token in ("child", "family", "亲子", "儿童"))
        elderly = any(token in traveler_text for token in ("elderly", "senior", "老人", "老年"))
        traveler_ok = bool(attractions) and (
            not family or any("family" in item.tags or item.intensity_level != "high" for item in attractions)
        ) and (
            not elderly or all(item.intensity_level != "high" for item in attractions)
        )
        criteria.append(_criterion("traveler_fit", traveler_ok, traveler_text, "traveler-compatible selected POIs"))
    if quality.planning_zone_coherence:
        max_zones = max((len({item.area for item in day.attractions if item.area}) for day in plan.days), default=0)
        criteria.append(_criterion("planning_zone_coherence", max_zones <= 2, max_zones, 2))

    if quality.min_experience_score is not None:
        actual_experience = (
            plan.quality_evaluation.overall_score
            if plan.quality_evaluation is not None else 0.0
        )
        criteria.append(_criterion(
            "min_experience_score",
            actual_experience >= quality.min_experience_score,
            actual_experience,
            quality.min_experience_score,
        ))

    if quality.pair_compatibility:
        knowledge = get_travel_knowledge_service()
        pair_values = [
            knowledge.pair_delta_any(left, right)
            for day in plan.days
            for left, right in zip(day.attractions, day.attractions[1:])
        ]
        positive_pairs = sum(value >= 0 for value in pair_values)
        criteria.append(_criterion(
            "pair_compatibility",
            all(value >= 0 for value in pair_values),
            pair_values,
            "no explicitly discouraged adjacent pair",
        ))

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
        "repair_count": len(plan.repair_attempts),
        "repair_commit_rate": (
            sum(item.status.value == "committed" for item in plan.repair_attempts)
            / max(1, len(plan.repair_attempts))
        ),
        "rollback_rate": (
            sum(item.status.value == "rolled_back" for item in plan.repair_attempts)
            / max(1, len(plan.repair_attempts))
        ),
        "final_experience_score": (
            plan.quality_evaluation.overall_score
            if plan.quality_evaluation is not None
            else 0.0
        ),
    }
    if settings.enable_travel_knowledge:
        service = get_travel_knowledge_service()
        known = [service.resolve_poi(request.city, item) for item in attractions]
        known = [item for item in known if item is not None]
        metrics["knowledge_coverage"] = round(len(known) / max(1, len(attractions)), 4)
        metrics["knowledge_feature_delta"] = round(
            sum(float(item.score_breakdown.get("knowledge_total_delta", 0.0)) for item in attractions), 2
        )
    else:
        metrics["knowledge_coverage"] = 0.0
        metrics["knowledge_feature_delta"] = 0.0
    portfolio = (
        plan.observability_trace.portfolio_metrics
        if plan.observability_trace is not None
        else calculate_portfolio_metrics(plan, attractions, request)
    )
    metrics.update(
        portfolio.model_dump(mode="json", exclude={"displaced_core_major"})
    )
    if quality.min_major_attractions:
        criteria.append(
            _criterion(
                "major_attraction_coverage",
                portfolio.major_attraction_count
                >= quality.min_major_attractions,
                portfolio.major_attraction_count,
                quality.min_major_attractions,
            )
        )
    if quality.max_niche_ratio is not None:
        criteria.append(
            _criterion(
                "max_niche_ratio",
                portfolio.niche_ratio <= quality.max_niche_ratio,
                portfolio.niche_ratio,
                quality.max_niche_ratio,
            )
        )
    if quality.min_niche_ratio is not None:
        criteria.append(
            _criterion(
                "min_niche_ratio",
                portfolio.niche_ratio >= quality.min_niche_ratio,
                portfolio.niche_ratio,
                quality.min_niche_ratio,
            )
        )
    if quality.max_repeated_subcategory_ratio is not None:
        criteria.append(
            _criterion(
                "subcategory_saturation",
                portfolio.repeated_subcategory_ratio
                <= quality.max_repeated_subcategory_ratio,
                portfolio.repeated_subcategory_ratio,
                quality.max_repeated_subcategory_ratio,
            )
        )
    if quality.min_portfolio_balance_score is not None:
        criteria.append(
            _criterion(
                "portfolio_balance",
                portfolio.portfolio_balance_score
                >= quality.min_portfolio_balance_score,
                portfolio.portfolio_balance_score,
                quality.min_portfolio_balance_score,
            )
        )
    if quality.require_selection_explanations:
        candidate_traces = (
            plan.observability_trace.candidate_pois
            if plan.observability_trace
            else []
        )
        criteria.append(
            _criterion(
                "portfolio_selection_explanations",
                bool(candidate_traces)
                and all(
                    item.selection_reason
                    and (
                        item.rejection_reason is not None
                        or item.name in {
                            attraction.name for attraction in attractions
                        }
                    )
                    for item in candidate_traces
                ),
                [
                    {
                        "name": item.name,
                        "role": item.selection_role,
                        "reason": item.selection_reason,
                        "rejected": item.rejection_reason,
                    }
                    for item in candidate_traces
                ],
                "every candidate has a selection role and decision reason",
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
