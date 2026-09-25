"""Canonical POI identity and duplicate-visit enforcement."""

from __future__ import annotations

from copy import deepcopy
from typing import Iterable, Sequence, TypeVar

from ..models.schemas import Attraction, POIInfo, TripPlan
from .place_name_service import normalize_place_name, place_names_match


POI = TypeVar("POI", Attraction, POIInfo)


class POIIdentityResolver:
    """Resolve aliases and provider parent relationships to one visit entity."""

    coordinate_precision = 4

    def assign_visit_keys(self, pois: Sequence[POI]) -> list[POI]:
        items = list(pois)
        ids = {
            self._provider_id(item)
            for item in items
            if self._provider_id(item)
        }
        for item in items:
            item.visit_key = self.visit_key(item, known_ids=ids)

        # Alias variants can have different provider IDs. Merge their keys only
        # when names match and coordinates describe the same physical venue.
        for index, item in enumerate(items):
            for existing in items[:index]:
                if self.same_visit_entity(existing, item):
                    item.visit_key = existing.visit_key
                    break
        return items

    def visit_key(
        self,
        poi: Attraction | POIInfo,
        *,
        known_ids: set[str] | None = None,
    ) -> str:
        parent_id = (getattr(poi, "parent_poi_id", "") or "").strip()
        provider_id = self._provider_id(poi)
        if parent_id:
            return f"provider:{parent_id}"
        if provider_id:
            return f"provider:{provider_id}"
        normalized = normalize_place_name(poi.name)
        location = poi.location
        return (
            f"name:{normalized}:"
            f"{location.longitude:.{self.coordinate_precision}f}:"
            f"{location.latitude:.{self.coordinate_precision}f}"
        )

    def same_visit_entity(
        self,
        left: Attraction | POIInfo,
        right: Attraction | POIInfo,
    ) -> bool:
        left_id = self._provider_id(left)
        right_id = self._provider_id(right)
        left_parent = (getattr(left, "parent_poi_id", "") or "").strip()
        right_parent = (getattr(right, "parent_poi_id", "") or "").strip()
        if left_id and right_id and left_id == right_id:
            return True
        if left_parent and left_parent == right_id:
            return True
        if right_parent and right_parent == left_id:
            return True
        if left_parent and right_parent and left_parent == right_parent:
            return True
        if getattr(left, "visit_key", "") and left.visit_key == getattr(
            right, "visit_key", ""
        ):
            return True
        return place_names_match(left.name, right.name) and self._nearby(left, right)

    def same_requested_visit_entity(
        self,
        left: Attraction | POIInfo,
        right: Attraction | POIInfo,
        must_visit: Iterable[str],
    ) -> bool:
        """Treat branches of one user-requested landmark as one visit.

        Map providers commonly return a parent attraction alongside internal
        scenic areas. Their parent IDs are often incomplete or form several
        levels, so provider identity alone cannot answer the user-level
        question: "did we already schedule Yuanmingyuan?"  This rule is only
        active for explicit must-visits; it does not collapse arbitrary nearby
        attractions that merely happen to share a word in their names.
        """
        left_roots = self._requested_visit_roots(left, must_visit)
        return bool(left_roots and left_roots.intersection(
            self._requested_visit_roots(right, must_visit)
        ))

    @staticmethod
    def _requested_visit_roots(
        poi: Attraction | POIInfo,
        must_visit: Iterable[str],
    ) -> set[str]:
        return {
            normalize_place_name(requested)
            for requested in must_visit
            if len(normalize_place_name(requested)) >= 2
            and place_names_match(requested, poi.name)
        }

    def deduplicate(
        self,
        pois: Sequence[POI],
        *,
        must_visit: Iterable[str] = (),
    ) -> list[POI]:
        items = self.assign_visit_keys([deepcopy(item) for item in pois])
        groups: list[list[POI]] = []
        for item in items:
            group = next(
                (
                    candidate_group
                    for candidate_group in groups
                    if (
                        self.same_visit_entity(candidate_group[0], item)
                        or self.same_requested_visit_entity(
                            candidate_group[0], item, must_visit
                        )
                    )
                ),
                None,
            )
            if group is None:
                groups.append([item])
            else:
                group.append(item)

        return [
            self._representative(group, tuple(must_visit))
            for group in groups
        ]

    def assigned_visit_keys(
        self,
        plan: TripPlan,
        *,
        excluding_day_index: int | None = None,
    ) -> set[str]:
        attractions = [
            item
            for day in plan.days
            if day.day_index != excluding_day_index
            for item in day.attractions
        ]
        self.assign_visit_keys(attractions)
        return {item.visit_key for item in attractions if item.visit_key}

    def deduplicate_plan(
        self,
        plan: TripPlan,
        *,
        must_visit: Iterable[str] = (),
    ) -> list[str]:
        """Remove repeated visit entities across all days, preserving first use."""

        must_visit = tuple(must_visit)
        all_items = [item for day in plan.days for item in day.attractions]
        self.assign_visit_keys(all_items)
        kept: list[Attraction] = []
        removed: list[str] = []
        for day in sorted(plan.days, key=lambda item: item.day_index):
            unique: list[Attraction] = []
            for attraction in day.attractions:
                duplicate = next(
                    (
                        existing
                        for existing in kept
                        if (
                            self.same_visit_entity(existing, attraction)
                            or self.same_requested_visit_entity(
                                existing, attraction, must_visit
                            )
                        )
                    ),
                    None,
                )
                if duplicate is None:
                    kept.append(attraction)
                    unique.append(attraction)
                    continue

                # If the later alias is the explicitly requested representation,
                # replace the earlier display entity without scheduling twice.
                later_required = any(
                    place_names_match(name, attraction.name)
                    for name in must_visit
                )
                earlier_required = any(
                    place_names_match(name, duplicate.name)
                    for name in must_visit
                )
                representative = self._representative(
                    [duplicate, attraction], must_visit
                )
                replace_with_later = representative is attraction
                if later_required and (not earlier_required or replace_with_later):
                    for previous_day in plan.days:
                        if duplicate in previous_day.attractions:
                            position = previous_day.attractions.index(duplicate)
                            previous_day.attractions[position] = attraction
                            if previous_day is day and duplicate in unique:
                                unique[unique.index(duplicate)] = attraction
                            kept[kept.index(duplicate)] = attraction
                            removed.append(duplicate.name)
                            break
                else:
                    removed.append(attraction.name)
            day.attractions = unique
        return removed

    @staticmethod
    def _provider_id(poi: Attraction | POIInfo) -> str:
        return (
            getattr(poi, "poi_id", None)
            or getattr(poi, "id", None)
            or ""
        ).strip()

    @staticmethod
    def _nearby(left: Attraction | POIInfo, right: Attraction | POIInfo) -> bool:
        # Roughly 1.1 km at Beijing's latitude. This prevents same-name chains
        # in different districts from being collapsed.
        return (
            abs(left.location.longitude - right.location.longitude) <= 0.012
            and abs(left.location.latitude - right.location.latitude) <= 0.010
        )

    def _representative(
        self,
        group: Sequence[POI],
        must_visit: Sequence[str],
    ) -> POI:
        def is_display_parent(item: POI) -> bool:
            if getattr(item, "parent_poi_id", ""):
                return False
            item_name = normalize_place_name(item.name)
            # Providers sometimes omit parent_poi_id but return an internal
            # venue as "故宫博物院-文华殿" next to its parent.  Prefer the
            # shorter containing venue unless the user explicitly requested
            # the child below.
            return any(
                item is not other
                and item_name
                and item_name in normalize_place_name(other.name)
                and item_name != normalize_place_name(other.name)
                for other in group
            )

        def key(item: POI) -> tuple:
            required = any(
                place_names_match(name, item.name)
                for name in must_visit
            )
            is_parent = is_display_parent(item)
            completeness = sum(
                bool(getattr(item, field, None))
                for field in ("address", "rating", "opening_time", "closing_time")
            )
            return (
                required,
                is_parent,
                completeness,
                len(normalize_place_name(item.name)),
            )

        representative = max(group, key=key)
        child_highlights = [
            item.name for item in group if item is not representative
        ]
        if child_highlights and isinstance(representative, Attraction):
            highlight = "；内部亮点：" + "、".join(child_highlights)
            if highlight not in representative.description:
                representative.description = (
                    (representative.description or "") + highlight
                ).strip("；")
        requested_roots = set().union(
            *(self._requested_visit_roots(item, must_visit) for item in group)
        )
        shared_key = (
            f"requested:{sorted(requested_roots)[0]}"
            if requested_roots
            else group[0].visit_key
        )
        representative.visit_key = shared_key
        if isinstance(representative, Attraction):
            representative.recall_sources = sorted(
                {
                    source
                    for item in group
                    for source in getattr(item, "recall_sources", [])
                }
            )
        return representative


_resolver = POIIdentityResolver()


def get_poi_identity_resolver() -> POIIdentityResolver:
    return _resolver
