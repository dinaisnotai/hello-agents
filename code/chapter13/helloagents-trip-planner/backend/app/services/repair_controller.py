"""Issue-directed itinerary repairs using existing deterministic components."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Sequence

from ..models.quality import ExperienceIssue, RepairStrategy
from ..models.schemas import Attraction, Hotel, TripPlan, TripRequest
from .attraction_scorer import AttractionScorer


@dataclass
class RepairProposal:
    plan: TripPlan
    action: str
    mutation_scope: tuple[str, ...] = ()


class RepairController:
    """Map structured issue types to bounded, deterministic plan mutations."""

    def __init__(self, planner: Any):
        self.planner = planner

    def propose(
        self,
        plan: TripPlan,
        request: TripRequest,
        candidates: Sequence[Attraction],
        hotel_candidates: Sequence[Hotel],
        issue: ExperienceIssue,
    ) -> RepairProposal | None:
        candidate_plan = deepcopy(plan)
        handlers = {
            RepairStrategy.ADD_UNUSED_CANDIDATE: self._repair_empty_day,
            RepairStrategy.ADD_NEARBY_COMPLEMENTARY_POI: self._repair_underfilled_day,
            RepairStrategy.ADD_WEATHER_BACKUP: self._repair_weather_backup,
            RepairStrategy.SWAP_WITH_INDOOR_CANDIDATE: self._repair_weather_backup,
            RepairStrategy.REMOVE_DUPLICATE: self._repair_duplicate_visit,
            RepairStrategy.RECLUSTER_ROUTE: self._repair_long_transport,
            RepairStrategy.RESELECT_HOTEL: self._repair_long_transport,
            RepairStrategy.REPLACE_LOW_VALUE_CATEGORY: self._repair_diversity,
            RepairStrategy.REDUCE_COST: self._repair_budget,
            RepairStrategy.RUN_CONSTRAINT_REPAIR: self._repair_constraint,
            RepairStrategy.ADD_MUST_VISIT: self._repair_landmark_coverage,
            RepairStrategy.REMOVE_CLOSED_ATTRACTION: self._repair_closed,
            RepairStrategy.RESOLVE_SAFETY_RISK: self._repair_weather_backup,
        }
        handler = handlers.get(issue.repair_strategy)
        if handler is None:
            return None
        action = handler(
            candidate_plan,
            request,
            candidates,
            hotel_candidates,
            issue,
        )
        return (
            RepairProposal(
                plan=candidate_plan,
                action=action,
                mutation_scope=self.mutation_scope(
                    issue.repair_strategy,
                    issue.day,
                ),
            )
            if action
            else None
        )

    @staticmethod
    def mutation_scope(
        strategy: RepairStrategy,
        day: int | None,
    ) -> tuple[str, ...]:
        target = f"days[{day - 1}]" if day else "days[*]"
        scopes = {
            RepairStrategy.ADD_WEATHER_BACKUP: (
                f"{target}.weather_backup",
                f"{target}.weather_warning",
            ),
            RepairStrategy.SWAP_WITH_INDOOR_CANDIDATE: (
                f"{target}.attractions",
            ),
            RepairStrategy.ADD_UNUSED_CANDIDATE: (
                f"{target}.attractions",
            ),
            RepairStrategy.ADD_NEARBY_COMPLEMENTARY_POI: (
                f"{target}.attractions",
            ),
            RepairStrategy.REMOVE_DUPLICATE: ("days[*].attractions",),
            RepairStrategy.RECLUSTER_ROUTE: ("days[*].attractions",),
            RepairStrategy.RESELECT_HOTEL: (
                "days[*].hotel",
                "days[*].accommodation",
            ),
            RepairStrategy.REPLACE_LOW_VALUE_CATEGORY: (
                f"{target}.attractions",
            ),
            RepairStrategy.RUN_CONSTRAINT_REPAIR: (
                "days[one].attractions",
                "days[one].schedule_blocks",
            ),
            RepairStrategy.REDUCE_COST: (
                "days[*].hotel",
                "days[*].accommodation",
                "days[one].attractions",
            ),
            RepairStrategy.ADD_MUST_VISIT: ("days[one].attractions",),
            RepairStrategy.REMOVE_CLOSED_ATTRACTION: (
                f"{target}.attractions",
            ),
            RepairStrategy.RESOLVE_SAFETY_RISK: (
                f"{target}.attractions",
                f"{target}.weather_backup",
                f"{target}.weather_warning",
            ),
        }
        return scopes.get(strategy, ())

    def _repair_weather_backup(
        self,
        plan,
        request,
        candidates,
        hotel_candidates,
        issue,
    ) -> str | None:
        target = self._target_day(plan, issue)
        if target is None:
            return None
        used = self.planner._identity().assigned_visit_keys(plan)
        indoor = [
            item
            for item in self._unused(plan, candidates)
            if self.planner.completeness_gate._is_indoor(item)
            and (item.visit_key or item.poi_id or item.name) not in used
        ]
        indoor.sort(
            key=lambda item: (
                -item.score,
                -item.popularity,
                item.name,
            )
        )
        target.weather_backup = deepcopy(indoor[: max(1, min(2, len(indoor)))])
        condition = next(
            (
                f"{item.day_weather} {item.night_weather}".strip()
                for item in plan.weather_info
                if item.date == target.date
            ),
            "天气存在不确定性",
        )
        if target.weather_backup:
            names = "、".join(item.name for item in target.weather_backup)
            target.weather_warning = (
                f"{condition}：保留主计划，并在临近出发时按实时天气切换至"
                f"室内备选（{names}）。"
            )
            return f"{RepairStrategy.ADD_WEATHER_BACKUP.value}:{names}"
        target.weather_warning = (
            f"{condition}：当前候选池没有可靠室内备选；保留主计划，"
            "请在出发前查看实时天气并准备雨具、防晒或缩短户外停留。"
        )
        return f"{RepairStrategy.ADD_WEATHER_BACKUP.value}:warning_only"

    def _repair_closed(
        self,
        plan,
        request,
        candidates,
        hotel_candidates,
        issue,
    ) -> str | None:
        target = self._target_day(plan, issue)
        if target is None:
            return None
        removed = [
            item for item in target.attractions
            if item.opening_hours_status == "closed"
        ]
        if not removed:
            return None
        target.attractions = [
            item for item in target.attractions
            if item.opening_hours_status != "closed"
        ]
        self._fill_day(plan, target, request, candidates, minimum=2)
        return (
            f"{RepairStrategy.REMOVE_CLOSED_ATTRACTION.value}:"
            + ",".join(item.name for item in removed)
        )

    def _repair_empty_day(
        self,
        plan,
        request,
        candidates,
        hotel_candidates,
        issue,
    ) -> str | None:
        target = self._target_day(plan, issue)
        if target is None:
            return None
        added = self._fill_day(plan, target, request, candidates, minimum=2)
        if added:
            return f"{RepairStrategy.ADD_UNUSED_CANDIDATE.value}:" + ",".join(added)

        return None

    def _repair_underfilled_day(
        self,
        plan,
        request,
        candidates,
        hotel_candidates,
        issue,
    ) -> str | None:
        target = self._target_day(plan, issue)
        if target is None:
            target = min(
                plan.days,
                key=lambda day: (
                    day.day_utilization_score,
                    len(day.attractions),
                    day.day_index,
                ),
                default=None,
            )
        if target is None:
            return None
        added = self._fill_day(plan, target, request, candidates, minimum=2)
        return (
            f"{RepairStrategy.ADD_NEARBY_COMPLEMENTARY_POI.value}:" + ",".join(added)
            if added
            else None
        )

    def _repair_duplicate_visit(
        self,
        plan,
        request,
        candidates,
        hotel_candidates,
        issue,
    ) -> str | None:
        removed = self.planner._identity().deduplicate_plan(
            plan,
            must_visit=request.must_visit,
        )
        if not removed:
            return None
        target = self._target_day(plan, issue)
        if target is not None:
            self._fill_day(plan, target, request, candidates, minimum=2)
        return f"{RepairStrategy.REMOVE_DUPLICATE.value}:" + ",".join(sorted(set(removed)))

    def _repair_long_transport(
        self,
        plan,
        request,
        candidates,
        hotel_candidates,
        issue,
    ) -> str | None:
        attractions = [
            item for day in plan.days for item in day.attractions
        ]
        current_hotel = next(
            (day.hotel for day in plan.days if day.hotel),
            None,
        )
        if (
            issue.repair_strategy == RepairStrategy.RESELECT_HOTEL
            and hotel_candidates
        ):
            selected = self.planner.accommodation_selector.select(
                request,
                hotel_candidates,
                attractions,
            )
            if current_hotel is None or selected.name != current_hotel.name:
                for day in plan.days:
                    day.hotel = deepcopy(selected)
                    day.accommodation = selected.type or request.accommodation
                return f"reselect_hotel:{selected.name}"

        if (
            issue.repair_strategy == RepairStrategy.RECLUSTER_ROUTE
            and current_hotel is not None
        ):
            before = [
                [item.visit_key or item.name for item in day.attractions]
                for day in plan.days
            ]
            self._regroup(plan, request, attractions, current_hotel)
            after = [
                [item.visit_key or item.name for item in day.attractions]
                for day in plan.days
            ]
            if after != before:
                return "recluster_existing_attractions_by_area"
        return None

    def _repair_landmark_coverage(
        self,
        plan,
        request,
        candidates,
        hotel_candidates,
        issue,
    ) -> str | None:
        unused = self._unused(plan, candidates)
        required = [
            item
            for item in unused
            if any(
                name.strip().lower() in item.name.strip().lower()
                or item.name.strip().lower() in name.strip().lower()
                for name in request.must_visit
            )
        ]
        core = required or [item for item in unused if item.is_core_landmark]
        for attraction in core:
            for target in sorted(
                plan.days,
                key=lambda day: (
                    len(day.attractions),
                    day.daily_duration_minutes,
                    day.day_index,
                ),
            ):
                if self.planner._try_add_attraction(
                    target, attraction, request, candidates
                ):
                    return (
                        f"add_core_landmark:{attraction.name}:"
                        f"day{target.day_index + 1}"
                    )
        return None

    def _repair_diversity(
        self,
        plan,
        request,
        candidates,
        hotel_candidates,
        issue,
    ) -> str | None:
        target = self._target_day(plan, issue)
        planned = [
            (day, attraction)
            for day in plan.days
            if target is None or day is target
            for attraction in day.attractions
        ]
        if not planned:
            return None
        counts = {}
        for _, item in planned:
            bucket = AttractionScorer.category_bucket(item)
            counts[bucket] = counts.get(bucket, 0) + 1
        repeated = max(counts, key=counts.get)
        unused = [
            item
            for item in self._unused(plan, candidates)
            if AttractionScorer.category_bucket(item) != repeated
        ]
        removable = sorted(
            (
                pair
                for pair in planned
                if AttractionScorer.category_bucket(pair[1]) == repeated
                and not self.planner.spatial_planner._is_must_visit(
                    pair[1], request.must_visit
                )
            ),
            key=lambda pair: (
                pair[1].score,
                pair[1].first_visit_priority,
                pair[1].name,
            ),
        )
        for day, old in removable:
            for replacement in unused:
                if (
                    old.selection_role
                    in {"core_landmark", "major_attraction"}
                    and replacement.selection_role
                    not in {"core_landmark", "major_attraction"}
                ):
                    continue
                if (
                    replacement.score < old.score
                    and replacement.selection_role == old.selection_role
                ):
                    continue
                candidate_day = deepcopy(day)
                candidate_day.attractions = [
                    item
                    for item in candidate_day.attractions
                    if item.visit_key != old.visit_key
                ]
                if self.planner._try_add_attraction(
                    candidate_day, replacement, request, candidates
                ):
                    day.attractions = candidate_day.attractions
                    return (
                        f"replace_repeated_category:{old.name}"
                        f"->{replacement.name}"
                    )
        return None

    def _repair_budget(
        self,
        plan,
        request,
        candidates,
        hotel_candidates,
        issue,
    ) -> str | None:
        current = next((day.hotel for day in plan.days if day.hotel), None)
        affordable = sorted(
            (
                hotel
                for hotel in hotel_candidates
                if current is None
                or hotel.estimated_cost < current.estimated_cost
            ),
            key=lambda item: (item.estimated_cost, item.name),
        )
        if affordable:
            selected = self.planner.accommodation_selector.select(
                request,
                affordable,
                [item for day in plan.days for item in day.attractions],
            )
            for day in plan.days:
                day.hotel = deepcopy(selected)
                day.accommodation = selected.type or request.accommodation
            return f"select_more_affordable_hotel:{selected.name}"

        optional = sorted(
            (
                (day, item)
                for day in plan.days
                for item in day.attractions
                if not self.planner.spatial_planner._is_must_visit(
                    item, request.must_visit
                )
            ),
            key=lambda pair: (
                -pair[1].ticket_price,
                pair[1].score,
                pair[1].name,
            ),
        )
        if optional and optional[0][1].ticket_price > 0:
            day, item = optional[0]
            day.attractions.remove(item)
            return f"remove_high_cost_optional_poi:{item.name}"
        return None

    def _repair_constraint(
        self,
        plan,
        request,
        candidates,
        hotel_candidates,
        issue,
    ) -> str | None:
        action = self.planner._apply_next_repair(
            plan,
            request,
            list(candidates),
        )
        return action[0] if action else None

    def _fill_day(
        self,
        plan: TripPlan,
        target,
        request: TripRequest,
        candidates: Sequence[Attraction],
        *,
        minimum: int,
    ) -> list[str]:
        added: list[str] = []
        used = self.planner._identity().assigned_visit_keys(plan)
        self.planner._identity().assign_visit_keys(list(candidates))
        current_categories = {
            AttractionScorer.category_bucket(item)
            for item in target.attractions
        }
        available = [
            item
            for item in candidates
            if item.visit_key not in used
        ]
        available.sort(
            key=lambda item: (
                AttractionScorer.category_bucket(item) in current_categories,
                self.planner.spatial_planner._next_poi_rank(
                    target.attractions,
                    item,
                    target.hotel.location
                    if target.hotel and target.hotel.location
                    else None,
                    target.transportation,
                    self.planner._daily_time_budget(request),
                ),
            )
        )
        for attraction in available:
            if self.planner._try_add_attraction(
                target,
                attraction,
                request,
                candidates,
            ):
                added.append(attraction.name)
                used.add(attraction.visit_key)
                current_categories.add(
                    AttractionScorer.category_bucket(attraction)
                )
                self.planner._recalculate_day(target, request)
                if (
                    len(target.attractions) >= minimum
                    and target.day_utilization_score >= 60
                ):
                    break
        return added

    def _unused(
        self,
        plan: TripPlan,
        candidates: Sequence[Attraction],
    ) -> list[Attraction]:
        self.planner._identity().assign_visit_keys(list(candidates))
        used = self.planner._identity().assigned_visit_keys(plan)
        return [item for item in candidates if item.visit_key not in used]

    def _regroup(
        self,
        plan: TripPlan,
        request: TripRequest,
        attractions: Sequence[Attraction],
        hotel: Hotel,
    ) -> None:
        groups = self.planner.spatial_planner.plan(
            attractions=attractions,
            travel_days=request.travel_days,
            pace=request.pace,
            must_visit=request.must_visit,
            hotel_location=hotel.location,
            transportation=request.transportation,
            daily_time_budget_minutes=self.planner._daily_time_budget(
                request
            ),
        )
        for day, group in zip(plan.days, groups):
            day.attractions = group
            day.hotel = deepcopy(hotel)

    @staticmethod
    def _target_day(plan: TripPlan, issue: ExperienceIssue):
        if issue.day is None or issue.day > len(plan.days):
            return None
        return plan.days[issue.day - 1]
