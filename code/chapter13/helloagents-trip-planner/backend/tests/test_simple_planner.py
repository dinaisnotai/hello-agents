"""Regressions for crowded, repeated and parent/child itineraries."""

import unittest
from unittest.mock import patch

from app.agents.trip_planner_agent import MultiAgentTripPlanner, POICollector
from app.config import settings
from app.models.schemas import Attraction, Location, POIInfo, TripRequest
from app.services.planner_service import get_planner
from app.services.spatial_planner import SpatialItineraryPlanner


def request(**updates):
    return TripRequest(**{
        "city": "北京", "start_date": "2026-10-10", "end_date": "2026-10-12",
        "travel_days": 3, "transportation": "公共交通", "accommodation": "经济型酒店",
        **updates,
    })


class SimplePlannerTest(unittest.TestCase):
    def test_same_area_split_never_assigns_one_poi_to_two_days(self):
        # All days share one bucket: the old primary-area loop reused its
        # unchanged candidate list on the next day, duplicating attractions.
        values = [Attraction(
            name=f"景点{i}", poi_id=str(i), area="东城区", score=90 - i,
            visit_duration=60,
            location=Location(longitude=116.40 + i * .001, latitude=39.90),
        ) for i in range(7)]
        for pace, limit in (("relaxed", 2), ("balanced", 3), ("packed", 4)):
            with self.subTest(pace=pace):
                groups = SpatialItineraryPlanner()._cluster_by_area(
                    values, 3, [], pace, None, "公共交通", 660
                )
                ids = [item.poi_id for group in groups for item in group]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(all(len(group) <= limit for group in groups))

    def test_recall_keeps_parent_even_when_child_is_first_and_better_rated(self):
        child = POIInfo(name="故宫博物院-文华殿", id="child", parent_poi_id="parent",
                        rating=5, location=Location(longitude=116.40, latitude=39.918))
        parent = POIInfo(name="故宫博物院", id="parent", rating=4,
                         location=Location(longitude=116.397, latitude=39.918))

        class Map:
            def search_poi(self, keyword, city):
                return [child, parent] if keyword in {"故宫", "文华殿"} else []

        collector = POICollector(Map())
        self.assertEqual([p.name for p in collector.collect_attractions(request())], [parent.name])
        self.assertEqual(
            [p.name for p in collector.collect_attractions(request(must_visit=["故宫"]))],
            [parent.name],
        )
        self.assertEqual(
            [p.name for p in collector.collect_attractions(request(must_visit=["文华殿"]))],
            [child.name],
        )

    def test_simple_mode_uses_no_rag_or_quality_mutation_and_keeps_constraints(self):
        get_planner.cache_clear()
        self.addCleanup(get_planner.cache_clear)
        with patch.object(settings, "workflow_mode", "simple"), \
             patch.object(settings, "amap_api_key", ""), \
             patch("app.agents.trip_planner_agent.get_travel_guide_rag", side_effect=AssertionError("RAG called")), \
             patch.object(MultiAgentTripPlanner, "_repair_low_soft_score", side_effect=AssertionError("fill called")):
            planner = get_planner()
            planner.amap_service.cache._data.clear()
            with patch.object(planner.repair_controller, "propose", side_effect=AssertionError("repair called")):
                plan = planner.plan_trip(request(pace="relaxed", must_visit=["故宫"]), emit_observability=False)
            names = [p.name for d in plan.days for p in d.attractions]
            keys = [p.visit_key for d in plan.days for p in d.attractions]
            self.assertTrue(any("故宫" in name for name in names))
            self.assertEqual(len(keys), len(set(keys)))
            self.assertTrue(all(len(d.attractions) <= 2 for d in plan.days))
            self.assertEqual(plan.repair_history, [])
            self.assertEqual(plan.evidence_sources, [])
            self.assertTrue(plan.normalized_constraints)

    def test_impossible_must_visit_is_reported_instead_of_silently_passed(self):
        with patch.object(settings, "amap_api_key", ""):
            planner = MultiAgentTripPlanner(lightweight=True)
            planner.amap_service.cache._data.clear()
            plan = planner.plan_trip(request(must_visit=["不存在的必去景点XYZ"]), emit_observability=False)
        self.assertFalse(plan.quality_gate_passed)
        self.assertTrue(plan.unresolved_blocking_issues)


if __name__ == "__main__":
    unittest.main()
