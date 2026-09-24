"""Deterministic itinerary completeness and experience quality gates."""

from __future__ import annotations

from collections import Counter
from typing import Sequence

from ..models.quality import (
    ExperienceEvaluation,
    ExperienceIssue,
    RepairStrategy,
)
from ..models.schemas import Attraction, TripPlan, TripRequest
from .attraction_scorer import AttractionScorer
from .poi_identity_resolver import POIIdentityResolver
from .poi_metadata_service import build_preference_profile
from .spatial_planner import SpatialItineraryPlanner
from .venue_policy import venue_kind
from .poi_metadata_service import preference_matches_categories
from .accommodation_selector import AccommodationSelector


class ItineraryCompletenessGate:
    """Evaluate structural quality without weakening hard validators."""

    minimum_useful_window_minutes = 180
    severe_transport_minutes = 240
    high_transport_minutes = 180

    def __init__(self, identity_resolver: POIIdentityResolver | None = None):
        self.identity_resolver = identity_resolver or POIIdentityResolver()

    def evaluate(
        self,
        request: TripRequest,
        plan: TripPlan,
        candidates: Sequence[Attraction] = (),
    ) -> ExperienceEvaluation:
        issues: list[ExperienceIssue] = []
        self._evaluate_product_contract(request, plan, issues)
        self._evaluate_days(request, plan, issues)
        self._evaluate_duplicates(plan, issues)
        self._evaluate_constraints(plan, issues)
        self._evaluate_required_visits(request, plan, candidates, issues)
        self._evaluate_confirmed_closures(plan, issues)
        self._evaluate_weather(plan, candidates, issues)
        self._evaluate_landmarks(request, plan, candidates, issues)
        self._evaluate_diversity(plan, issues)
        issues = self._unique_issues(issues)
        score = self._score(issues)
        return ExperienceEvaluation(
            **{
                "pass": not any(issue.is_blocking for issue in issues),
                "overall_score": score,
                "issues": issues,
                "source": "deterministic",
            }
        )

    @staticmethod
    def _evaluate_product_contract(request, plan, issues):
        categories = set()
        for day in plan.days:
            for attraction in day.attractions:
                categories.update(attraction.categories)
                if venue_kind(attraction.name, attraction.category or "") != "attraction":
                    issues.append(ExperienceIssue(issue_type="safety_risk", severity="critical", day=day.day_index + 1,
                        evidence=f"{attraction.name}不是可游览景点，不能占用景点名额", repair_strategy=RepairStrategy.RESOLVE_SAFETY_RISK))
            if day.meals and any(meal.source == "unconfirmed" for meal in day.meals):
                issues.append(ExperienceIssue(issue_type="experience_quality", severity="info", day=day.day_index + 1,
                    evidence="用餐已预留时间，但餐厅尚未确认；饮食限制与营业需核实", repair_strategy=RepairStrategy.REPLACE_LOW_VALUE_CATEGORY))
        missing = [pref for pref in request.preferences if not (
            any(meal.source == "map_poi" for day in plan.days for meal in day.meals) if "美食" in pref
            else preference_matches_categories([pref], categories))]
        if missing:
            issues.append(ExperienceIssue(issue_type="preference_alignment", severity="warning",
                evidence="尚未覆盖偏好：" + "、".join(missing), repair_strategy=RepairStrategy.REPLACE_LOW_VALUE_CATEGORY))
        requested = AccommodationSelector._tier(request.accommodation)
        if any(day.hotel and requested != "unspecified" and AccommodationSelector._tier(day.hotel.type) != requested for day in plan.days):
            issues.append(ExperienceIssue(issue_type="experience_quality", severity="info",
                evidence="酒店档次尚未确认符合住宿偏好", repair_strategy=RepairStrategy.RESELECT_HOTEL))

    def merge(
        self,
        deterministic: ExperienceEvaluation,
        external: ExperienceEvaluation | None,
    ) -> ExperienceEvaluation:
        if external is None:
            return deterministic
        for issue in external.issues:
            if issue.is_blocking:
                issue.resolution_status = "open"
        issues = self._unique_issues(
            [*deterministic.issues, *external.issues]
        )
        score = min(
            deterministic.overall_score,
            external.overall_score,
            self._score(issues),
        )
        return ExperienceEvaluation(
            **{
                "pass": (
                    not any(item.is_blocking for item in issues)
                ),
                "overall_score": score,
                "issues": issues,
                "source": "merged",
                "contract_errors": [
                    *deterministic.contract_errors,
                    *external.contract_errors,
                ],
            }
        )

    def _evaluate_days(
        self,
        request: TripRequest,
        plan: TripPlan,
        issues: list[ExperienceIssue],
    ) -> None:
        for day in plan.days:
            if not day.attractions:
                if self._allows_empty_day(request, day.day_index, len(plan.days)):
                    continue
                issues.append(
                    ExperienceIssue(
                        issue_type="empty_day",
                        severity="critical",
                        day=day.day_index + 1,
                        evidence=(
                            f"Day {day.day_index + 1} has no scheduled activities "
                            "during a usable destination-day window"
                        ),
                        repair_strategy=RepairStrategy.ADD_UNUSED_CANDIDATE,
                    )
                )
                continue

            if self._is_underfilled(day):
                attraction = day.attractions[0]
                issues.append(
                    ExperienceIssue(
                        issue_type="underfilled_day",
                        severity="warning",
                        day=day.day_index + 1,
                        evidence=(
                            f"Day {day.day_index + 1} contains only "
                            f"{attraction.name} ({attraction.visit_duration} visit minutes, "
                            f"{day.day_utilization_score:.0f}% utilization)"
                        ),
                        repair_strategy=RepairStrategy.ADD_NEARBY_COMPLEMENTARY_POI,
                    )
                )

            if day.daily_travel_minutes > self.severe_transport_minutes:
                severity = "high"
            elif day.daily_travel_minutes > self.high_transport_minutes:
                severity = "warning"
            else:
                severity = None
            if severity:
                issues.append(
                    ExperienceIssue(
                        issue_type="long_transport",
                        severity=severity,
                        day=day.day_index + 1,
                        evidence=(
                            f"Day {day.day_index + 1} requires "
                            f"{day.daily_travel_minutes} transport minutes"
                        ),
                        repair_strategy=(
                            RepairStrategy.RESELECT_HOTEL
                            if len(day.attractions) == 1
                            else RepairStrategy.RECLUSTER_ROUTE
                        ),
                    )
                )

    def _evaluate_duplicates(
        self,
        plan: TripPlan,
        issues: list[ExperienceIssue],
    ) -> None:
        attractions = [
            item for day in plan.days for item in day.attractions
        ]
        self.identity_resolver.assign_visit_keys(attractions)
        seen: dict[str, int] = {}
        for day in plan.days:
            for item in day.attractions:
                if item.visit_key in seen:
                    issues.append(
                        ExperienceIssue(
                            issue_type="duplicate_visit",
                            severity="critical",
                            day=day.day_index + 1,
                            evidence=(
                                f"{item.name} repeats canonical visit "
                                f"{item.visit_key} from Day {seen[item.visit_key]}"
                            ),
                            repair_strategy=RepairStrategy.REMOVE_DUPLICATE,
                            affected_visit_keys=[item.visit_key],
                        )
                    )
                else:
                    seen[item.visit_key] = day.day_index + 1

    @staticmethod
    def _evaluate_required_visits(
        request: TripRequest,
        plan: TripPlan,
        candidates: Sequence[Attraction],
        issues: list[ExperienceIssue],
    ) -> None:
        if not request.must_visit:
            return
        planned_names = {
            item.name.strip().lower()
            for day in plan.days
            for item in day.attractions
        }
        for required in request.must_visit:
            normalized = required.strip().lower()
            if any(
                normalized in name or name in normalized
                for name in planned_names
            ):
                continue
            matching = [
                item for item in candidates
                if normalized in item.name.strip().lower()
                or item.name.strip().lower() in normalized
            ]
            issues.append(
                ExperienceIssue(
                    issue_type="missing_must_visit",
                    severity="critical",
                    evidence=(
                        f"Required attraction {required} is absent; "
                        f"matching candidates={len(matching)}"
                    ),
                    repair_strategy=RepairStrategy.ADD_MUST_VISIT,
                    affected_visit_keys=[
                        item.visit_key or item.poi_id or item.name
                        for item in matching
                    ],
                )
            )

    @staticmethod
    def _evaluate_confirmed_closures(
        plan: TripPlan,
        issues: list[ExperienceIssue],
    ) -> None:
        for day in plan.days:
            for attraction in day.attractions:
                if attraction.opening_hours_status != "closed":
                    continue
                issues.append(
                    ExperienceIssue(
                        issue_type="attraction_closed",
                        severity="critical",
                        day=day.day_index + 1,
                        evidence=(
                            f"{attraction.name} is confirmed closed in "
                            "structured opening-hours data"
                        ),
                        repair_strategy=RepairStrategy.REMOVE_CLOSED_ATTRACTION,
                        affected_visit_keys=[
                            attraction.visit_key
                            or attraction.poi_id
                            or attraction.name
                        ],
                    )
                )

    @staticmethod
    def _evaluate_constraints(
        plan: TripPlan,
        issues: list[ExperienceIssue],
    ) -> None:
        failures = [
            item.message or item.name
            for item in plan.constraint_report.items
            if not item.passed
            and item.severity.lower() in {"blocker", "hard", "critical"}
        ]
        failures.extend(
            item.message
            for item in plan.validation_result.violations
            if item.severity == "hard"
        )
        if failures or not plan.validation_result.valid:
            issues.append(
                ExperienceIssue(
                    issue_type="constraint_failure",
                    severity="critical",
                    evidence="; ".join(failures) or "Hard constraint validation failed",
                    repair_strategy=RepairStrategy.RUN_CONSTRAINT_REPAIR,
                )
            )
        if plan.budget and plan.budget.budget_limit is not None:
            if plan.budget.total > plan.budget.budget_limit:
                issues.append(
                    ExperienceIssue(
                        issue_type="budget_violation",
                        severity="critical",
                        evidence=(
                            f"Plan cost {plan.budget.total} exceeds "
                            f"budget {plan.budget.budget_limit}"
                        ),
                        repair_strategy=RepairStrategy.REDUCE_COST,
                    )
                )
        if any(
            ("返程时间" in item.name or "DAILY_END_TIME" in item.name)
            and not item.passed
            and item.severity.lower() in {"blocker", "hard", "critical"}
            for item in plan.constraint_report.items
        ):
            issues.append(
                ExperienceIssue(
                    issue_type="time_violation",
                    severity="critical",
                    evidence="At least one day exceeds the allowed return time",
                    repair_strategy=RepairStrategy.RUN_CONSTRAINT_REPAIR,
                )
            )

    @staticmethod
    def _evaluate_landmarks(
        request: TripRequest,
        plan: TripPlan,
        candidates: Sequence[Attraction],
        issues: list[ExperienceIssue],
    ) -> None:
        profile = build_preference_profile(request)
        if not (profile.first_visit or profile.prefer_classic):
            return
        selected = [
            item
            for day in plan.days
            for item in day.attractions
            if item.is_core_landmark
        ]
        available = [item for item in candidates if item.is_core_landmark]
        if not available:
            return
        required = min(len(available), max(1, min(2, request.travel_days)))
        if len({item.visit_key or item.poi_id or item.name for item in selected}) < required:
            issues.append(
                ExperienceIssue(
                    issue_type="low_landmark_coverage",
                    severity="warning",
                    evidence=(
                        f"First-visit plan has {len(selected)} core landmarks; "
                        f"{required} feasible core candidates are required"
                    ),
                    repair_strategy=RepairStrategy.ADD_MUST_VISIT,
                )
            )

    @staticmethod
    def _evaluate_diversity(
        plan: TripPlan,
        issues: list[ExperienceIssue],
    ) -> None:
        attractions = [
            item for day in plan.days for item in day.attractions
        ]
        if len(attractions) < 3:
            return
        counts = Counter(
            AttractionScorer.category_bucket(item)
            for item in attractions
        )
        dominant_category, dominant_count = counts.most_common(1)[0]
        if dominant_count < 3 or len(counts) > 1:
            return
        issues.append(
            ExperienceIssue(
                issue_type="poor_diversity",
                severity="warning",
                evidence=(
                    f"All {dominant_count} planned attractions are "
                    f"in category {dominant_category}"
                ),
                repair_strategy=RepairStrategy.REPLACE_LOW_VALUE_CATEGORY,
            )
        )

    @staticmethod
    def _is_indoor(attraction: Attraction) -> bool:
        text = " ".join(
            [
                attraction.name,
                attraction.category or "",
                *attraction.categories,
                *attraction.tags,
            ]
        ).lower()
        indoor_markers = (
            "museum", "gallery", "theater", "aquarium", "indoor",
            "博物馆", "美术馆", "展览", "剧院", "室内", "水族馆",
            "科技馆", "纪念馆",
        )
        return any(marker in text for marker in indoor_markers)

    def _evaluate_weather(
        self,
        plan: TripPlan,
        candidates: Sequence[Attraction],
        issues: list[ExperienceIssue],
    ) -> None:
        risky_words = ("雨", "雷", "高温", "炎热", "暴晒", "hot", "rain")
        extreme_words = ("台风", "暴雨红色", "极端", "龙卷风", "冰雹红色")
        weather_by_date = {item.date: item for item in plan.weather_info}
        for day in plan.days:
            weather = weather_by_date.get(day.date)
            if weather is None:
                continue
            condition = f"{weather.day_weather} {weather.night_weather}"
            if not any(word.lower() in condition.lower() for word in risky_words):
                continue
            exposed = [
                item for item in day.attractions if not self._is_indoor(item)
            ]
            if not exposed:
                continue
            extreme = any(word in condition for word in extreme_words)
            issues.append(
                ExperienceIssue(
                    issue_type="safety_risk" if extreme else "weather_risk",
                    severity="critical" if extreme else (
                        "info"
                        if day.weather_backup or day.weather_warning
                        else "warning"
                    ),
                    day=day.day_index + 1,
                    evidence=(
                        f"{condition.strip()} affects outdoor primary visits: "
                        + ", ".join(item.name for item in exposed)
                    ),
                    repair_strategy=(
                        RepairStrategy.RESOLVE_SAFETY_RISK
                        if extreme
                        else RepairStrategy.ADD_WEATHER_BACKUP
                    ),
                    affected_visit_keys=[
                        item.visit_key or item.poi_id or item.name
                        for item in exposed
                    ],
                    resolution_status=(
                        "mitigated"
                        if day.weather_backup and not extreme
                        else "open"
                    ),
                )
            )

    def _allows_empty_day(
        self,
        request: TripRequest,
        day_index: int,
        day_count: int,
    ) -> bool:
        start = self._minutes(request.daily_start_time, 9 * 60)
        end = self._minutes(request.daily_end_time, 20 * 60)
        if day_index == 0 and request.arrival_time:
            start = max(start, self._minutes(request.arrival_time, start))
        if day_index == day_count - 1 and request.departure_time:
            end = min(end, self._minutes(request.departure_time, end))
        return max(0, end - start) < self.minimum_useful_window_minutes

    @staticmethod
    def _is_underfilled(day) -> bool:
        if len(day.attractions) != 1:
            return False
        attraction = day.attractions[0]
        if attraction.visit_duration >= 240:
            return False
        if (
            day.daily_travel_minutes
            >= SpatialItineraryPlanner.remote_travel_threshold_minutes
        ):
            return False
        return (
            day.day_utilization_score < 70
            and day.daily_duration_minutes < 420
        )

    @staticmethod
    def _minutes(value: str | None, default: int) -> int:
        if not value:
            return default
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)

    @staticmethod
    def _unique_issues(
        issues: Sequence[ExperienceIssue],
    ) -> list[ExperienceIssue]:
        by_identity: dict[tuple[str, int | None], ExperienceIssue] = {}
        severity_rank = {"info": 0, "warning": 1, "high": 2, "critical": 3}
        issue_priority = {
            "duplicate_visit": 0,
            "empty_day": 1,
            "underfilled_day": 2,
            "budget_violation": 3,
            "time_violation": 4,
            "long_transport": 5,
            "low_landmark_coverage": 6,
            "poor_diversity": 7,
            "constraint_failure": 8,
            "experience_quality": 9,
        }
        for issue in issues:
            existing = by_identity.get(issue.identity)
            if (
                existing is None
                or severity_rank[issue.severity]
                > severity_rank[existing.severity]
            ):
                by_identity[issue.identity] = issue
        return sorted(
            by_identity.values(),
            key=lambda item: (
                -severity_rank[item.severity],
                issue_priority.get(item.issue_type, 99),
                item.day or 0,
            ),
        )

    @staticmethod
    def _score(issues: Sequence[ExperienceIssue]) -> float:
        penalty = {
            "critical": 2.5,
            "high": 1.5,
            "warning": 0.5,
            "info": 0.0,
        }
        return round(
            max(0.0, 10.0 - sum(penalty[item.severity] for item in issues)),
            1,
        )
