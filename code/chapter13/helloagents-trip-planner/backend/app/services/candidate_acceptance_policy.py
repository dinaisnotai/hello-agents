"""Shared portfolio-quality gate for every itinerary candidate entry point."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..models.schemas import Attraction, TripRequest
from .attraction_scorer import AttractionScorer
from .poi_metadata_service import build_preference_profile


@dataclass(frozen=True)
class CandidateAcceptanceDecision:
    accepted: bool
    reason: str
    quality_floor: float
    higher_value_alternatives: tuple[str, ...] = ()


class CandidateAcceptancePolicy:
    """Keep route-feasible additions from degrading the selected portfolio.

    This policy deliberately does not alter AttractionScorer.  It consumes
    its existing score and metadata, and is used by initial selection as well
    as every repair/fill path.
    """

    def evaluate(
        self,
        candidate: Attraction,
        *,
        pool: Sequence[Attraction],
        selected: Sequence[Attraction],
        request: TripRequest | None,
        entry_point: str,
    ) -> CandidateAcceptanceDecision:
        deep_exploration = bool(
            (build_preference_profile(request).deep_exploration if request else False)
            or candidate.score_breakdown.get("deep_exploration", 0) > 0
        )
        role = candidate.selection_role
        scores = sorted(
            item.score for item in pool if item.score > 0
        )
        # A dynamic floor: lower quartile of this request's candidate pool,
        # bounded by the candidate's role availability rather than a global
        # magic number.
        quality_floor = (
            scores[max(0, math.floor((len(scores) - 1) * 0.25))]
            if scores
            else 0.0
        )
        selected_keys = {item.visit_key or item.name for item in selected}
        alternatives = [
            item
            for item in pool
            if (item.visit_key or item.name) not in selected_keys
            and item is not candidate
        ]
        higher_core_major = sorted(
            (
                item
                for item in alternatives
                if item.selection_role in {"core_landmark", "major_attraction"}
                and item.score >= candidate.score + 12
            ),
            key=lambda item: (-item.score, item.name),
        )
        niche_count = sum(
            item.selection_role == "niche_attraction" for item in selected
        )
        niche_cap = (
            max(1, math.ceil(max(1, len(selected) + 1) * 0.7))
            if deep_exploration
            else max(1, math.floor(max(1, len(selected) + 1) * 0.25))
        )
        bucket = AttractionScorer.category_bucket(candidate)
        repeated_bucket = sum(
            AttractionScorer.category_bucket(item) == bucket for item in selected
        )

        if candidate.selection_role == "niche_attraction" and not deep_exploration:
            if higher_core_major:
                return self._record(
                    candidate, entry_point, False,
                    "higher_value_core_or_major_feasible",
                    quality_floor, higher_core_major,
                )
            if candidate.score < quality_floor:
                return self._record(
                    candidate, entry_point, False,
                    "below_dynamic_niche_quality_floor",
                    quality_floor, (),
                )
            if niche_count >= niche_cap:
                return self._record(
                    candidate, entry_point, False,
                    "niche_ratio_cap",
                    quality_floor, (),
                )
        if repeated_bucket >= 2 and candidate.score < quality_floor:
            return self._record(
                candidate, entry_point, False,
                "repeated_subcategory_low_quality",
                quality_floor, (),
            )
        return self._record(
            candidate, entry_point, True, "accepted", quality_floor, ())

    @staticmethod
    def _record(
        candidate: Attraction,
        entry_point: str,
        accepted: bool,
        reason: str,
        quality_floor: float,
        alternatives: Sequence[Attraction],
    ) -> CandidateAcceptanceDecision:
        candidate.selection_trace.append(
            {
                "entry_point": entry_point,
                "accepted": accepted,
                "reason": reason,
                "candidate_score": round(candidate.score, 2),
                "quality_floor": round(quality_floor, 2),
                "role": candidate.selection_role,
                "higher_value_alternatives": [item.name for item in alternatives[:3]],
            }
        )
        return CandidateAcceptanceDecision(
            accepted=accepted,
            reason=reason,
            quality_floor=quality_floor,
            higher_value_alternatives=tuple(item.name for item in alternatives[:3]),
        )
