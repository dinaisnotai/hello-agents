import unittest

from app.models.schemas import Attraction, Location, TripPlan, TripRequest
from app.services.attraction_scorer import AttractionScorer
from app.services.planning_observability import build_planning_trace
from app.services.poi_metadata_service import (
    build_preference_profile,
    enrich_attraction,
)
from app.services.spatial_planner import SpatialItineraryPlanner


def request(**updates):
    payload = {
        "city": "北京",
        "start_date": "2026-10-10",
        "end_date": "2026-10-12",
        "travel_days": 3,
        "transportation": "public transit",
        "accommodation": "comfortable hotel",
        "first_visit": True,
    }
    payload.update(updates)
    return TripRequest(**payload)


def raw_poi(
    name,
    index,
    categories,
    *,
    popularity=7,
    first_priority=6,
):
    return Attraction(
        name=name,
        poi_id=f"portfolio-{index}",
        visit_key=f"provider:portfolio-{index}",
        category=";".join(categories),
        categories=list(categories),
        popularity=popularity,
        first_visit_priority=first_priority,
        visit_duration=120,
        location=Location(
            longitude=116.35 + index * 0.003,
            latitude=39.90 + index * 0.002,
        ),
    )


def ranked(req, values):
    profile = build_preference_profile(req)
    enriched = [
        enrich_attraction(item, req, profile) for item in values
    ]
    return AttractionScorer().score_candidates(
        enriched,
        req,
        None,
        660,
    )


def select(req, values):
    candidates = ranked(req, values)
    selected = SpatialItineraryPlanner().select_attractions(
        candidates,
        req.travel_days,
        req.pace,
        req.must_visit,
        per_day_limit=3,
    )
    return candidates, selected


def base_candidates():
    return [
        raw_poi("故宫博物院", 1, ["historic", "culture"], popularity=10, first_priority=10),
        raw_poi("天坛公园", 2, ["historic", "culture", "park"], popularity=9, first_priority=10),
        raw_poi("颐和园", 3, ["historic", "culture", "park"], popularity=10, first_priority=10),
        raw_poi("北京动物园", 4, ["zoo", "family"], popularity=9, first_priority=8),
        raw_poi("中国科学技术馆", 5, ["museum", "family", "interactive_activity"], popularity=9, first_priority=8),
        raw_poi("太庙", 6, ["historic", "culture"], popularity=8, first_priority=8),
        raw_poi("国子监", 7, ["historic", "culture"], popularity=8, first_priority=7),
        raw_poi("首都博物馆", 8, ["historic", "museum"], popularity=8, first_priority=8),
        raw_poi("亮马河国际风情水岸公园", 9, ["park"], popularity=6, first_priority=5),
        raw_poi("念坛公园", 10, ["park"], popularity=5, first_priority=4),
        raw_poi("北京湖广会馆", 11, ["historic", "specialist"], popularity=6, first_priority=5),
        raw_poi("史家胡同博物馆", 12, ["historic", "museum", "specialist"], popularity=5, first_priority=4),
        raw_poi("正阳门箭楼", 13, ["historic"], popularity=8, first_priority=7),
    ]


class PortfolioPolicyTest(unittest.TestCase):
    def test_first_visit_family_balances_landmarks_and_family_fit(self):
        req = request(preferences=["亲子"], travelers=["child"])
        _, selected = select(req, base_candidates())

        roles = [item.selection_role for item in selected]
        ordinary_parks = [
            item for item in selected
            if item.name in {"亮马河国际风情水岸公园", "念坛公园"}
        ]
        family = [
            item for item in selected
            if {"family", "zoo", "interactive_activity"}
            & set(item.categories)
        ]
        self.assertGreaterEqual(roles.count("core_landmark"), 2)
        self.assertTrue(family)
        self.assertLess(
            len(ordinary_parks) / max(1, len(selected)),
            0.5,
        )

    def test_first_visit_history_has_core_major_limited_niche_layers(self):
        req = request(preferences=["亲子"], travelers=["child"])
        _, selected = select(req, base_candidates())
        roles = [item.selection_role for item in selected]

        self.assertGreaterEqual(roles.count("core_landmark"), 2)
        self.assertGreaterEqual(roles.count("major_attraction"), 1)
        self.assertLessEqual(roles.count("niche_attraction"), 2)
        self.assertFalse(
            all(
                item.name
                in {"国子监", "北京湖广会馆", "史家胡同博物馆", "正阳门箭楼"}
                for item in selected
            )
        )

    def test_explicit_deep_history_allows_high_niche_ratio_without_core_quota(self):
        req = request(
            preferences=["历史"],
            first_visit=None,
            free_text_input="多次到访，避开热门，小众历史深度游",
        )
        _, selected = select(req, base_candidates())
        niche = [
            item for item in selected
            if item.selection_role == "niche_attraction"
        ]

        self.assertGreaterEqual(len(niche), 2)
        self.assertFalse(
            any(
                item.selection_reason == "core_landmark_quota"
                for item in selected
            )
        )
        self.assertTrue(
            {"北京湖广会馆", "史家胡同博物馆"}
            & {item.name for item in selected}
        )

    def test_no_preference_preserves_core_coverage(self):
        req = request(preferences=[])
        _, selected = select(req, base_candidates())
        self.assertGreaterEqual(
            sum(item.selection_role == "core_landmark" for item in selected),
            2,
        )

    def test_sparse_preference_does_not_fill_low_value_same_category(self):
        req = request(preferences=["历史"])
        values = base_candidates()[:8] + [
            raw_poi(
                f"低价值专题历史馆{index}",
                30 + index,
                ["historic", "museum", "specialist"],
                popularity=2,
                first_priority=2,
            )
            for index in range(6)
        ]
        _, selected = select(req, values)
        self.assertFalse(
            any(item.name.startswith("低价值专题历史馆") for item in selected)
        )

    def test_core_preference_conflict_has_explainable_rejection(self):
        req = request(preferences=["亲子"], travel_days=1, end_date="2026-10-10")
        candidates, selected = select(req, base_candidates())
        plan = TripPlan(
            city=req.city,
            start_date=req.start_date,
            end_date=req.end_date,
            days=[],
            overall_suggestions="",
        )
        # Use a minimal day-free plan to inspect candidate portfolio reasons
        # independently of route feasibility.
        from app.models.schemas import DayPlan
        plan.days = [
            DayPlan(
                date=req.start_date,
                day_index=0,
                description="portfolio",
                transportation=req.transportation,
                accommodation=req.accommodation,
                attractions=selected,
            )
        ]
        trace = build_planning_trace(req, plan, candidates=candidates)
        rejected_core = [
            item for item in trace.candidate_pois
            if item.selection_role == "core_landmark"
            and item.rejection_reason is not None
        ]

        self.assertTrue(rejected_core)
        self.assertTrue(
            all(item.selection_reason for item in rejected_core)
        )
        self.assertTrue(trace.portfolio_metrics.displaced_core_major)

    def test_saturation_penalty_is_exposed(self):
        req = request(preferences=["亲子"], travelers=["child"])
        _, selected = select(req, base_candidates())
        self.assertTrue(
            any(
                item.score_breakdown.get("saturation_penalty", 0) > 0
                for item in selected
            )
        )


if __name__ == "__main__":
    unittest.main()
