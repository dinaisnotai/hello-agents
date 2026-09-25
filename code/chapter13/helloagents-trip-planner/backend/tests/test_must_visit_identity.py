import unittest
from types import SimpleNamespace

from app.agents.trip_planner_agent import POICollector
from app.models.schemas import Attraction, DayPlan, Location, POIInfo, TripPlan, TripRequest
from app.services.itinerary_quality import ItineraryCompletenessGate
from app.services.poi_identity_resolver import POIIdentityResolver


def location(longitude=116.30, latitude=40.00):
    return Location(longitude=longitude, latitude=latitude)


def poi(name, poi_id, *, parent="", longitude=116.30):
    return POIInfo(
        id=poi_id,
        name=name,
        type="风景名胜;风景名胜;国家级景点",
        parent_poi_id=parent,
        location=location(longitude),
    )


def attraction(source):
    return Attraction(
        name=source.name,
        poi_id=source.id,
        parent_poi_id=source.parent_poi_id,
        category=source.type,
        location=source.location,
    )


def request():
    return TripRequest(
        city="北京",
        start_date="2026-10-01",
        end_date="2026-10-01",
        travel_days=1,
        transportation="混合",
        accommodation="舒适酒店",
        must_visit=["圆明园"],
    )


class MustVisitIdentityTest(unittest.TestCase):
    def setUp(self):
        self.parent = poi("圆明园遗址公园", "parent")
        self.branch = poi("圆明园遗址公园-多稼轩", "branch", parent="parent")
        # Some provider branches do not carry the parent ID.
        self.entrance = poi("圆明园遗址公园-西北景区", "entrance", longitude=116.287)

    def test_explicit_parent_request_collapses_all_branches_to_parent(self):
        items = POIIdentityResolver().deduplicate(
            [self.branch, self.entrance, self.parent], must_visit=["圆明园"]
        )
        self.assertEqual([item.name for item in items], ["圆明园遗址公园"])
        self.assertEqual(items[0].visit_key, "requested:圆明园")

    def test_saved_plan_duplicate_branches_are_removed_before_scheduling(self):
        plan = TripPlan(
            city="北京",
            start_date="2026-10-01",
            end_date="2026-10-01",
            overall_suggestions="",
            days=[
                DayPlan(
                    date="2026-10-01",
                    day_index=0,
                    description="",
                    transportation="混合",
                    accommodation="",
                    attractions=[attraction(self.branch), attraction(self.entrance), attraction(self.parent)],
                )
            ],
        )
        removed = POIIdentityResolver().deduplicate_plan(plan, must_visit=["圆明园"])
        self.assertEqual(len(removed), 2)
        self.assertEqual([item.name for item in plan.days[0].attractions], ["圆明园遗址公园"])

    def test_quality_gate_marks_requested_branches_as_a_critical_duplicate(self):
        plan = TripPlan(
            city="北京",
            start_date="2026-10-01",
            end_date="2026-10-01",
            overall_suggestions="",
            days=[
                DayPlan(
                    date="2026-10-01",
                    day_index=0,
                    description="",
                    transportation="混合",
                    accommodation="",
                    attractions=[attraction(self.branch), attraction(self.entrance)],
                )
            ],
        )
        result = ItineraryCompletenessGate().evaluate(request(), plan)
        self.assertTrue(any(item.issue_type == "duplicate_visit" and item.is_blocking for item in result.issues))

    def test_collector_uses_parent_detail_for_a_broad_must_visit(self):
        service = SimpleNamespace(
            search_poi=lambda keyword, city: [self.branch, self.entrance] if keyword == "圆明园" else [],
            resolve_poi_detail=lambda poi_id, city: self.parent if poi_id == "parent" else None,
        )
        selected = POICollector(service).collect_attractions(request())
        matches = [item for item in selected if "圆明园" in item.name]
        self.assertEqual([item.name for item in matches], ["圆明园遗址公园"])
        self.assertEqual(matches[0].visit_key, "requested:圆明园")


if __name__ == "__main__":
    unittest.main()
