import unittest

from app.models.schemas import (
    Attraction,
    DayPlan,
    Location,
    TripPlan,
)
from app.services.poi_identity_resolver import POIIdentityResolver


def attraction(
    name,
    poi_id,
    *,
    parent_poi_id="",
    longitude=116.397,
):
    return Attraction(
        name=name,
        poi_id=poi_id,
        parent_poi_id=parent_poi_id,
        location=Location(longitude=longitude, latitude=39.918),
    )


class POIIdentityResolverTest(unittest.TestCase):
    def setUp(self):
        self.resolver = POIIdentityResolver()

    def test_parent_and_child_share_visit_key_and_deduplicate(self):
        palace = attraction("故宫博物院", "palace")
        wenhua = attraction(
            "文华殿",
            "wenhua",
            parent_poi_id="palace",
            longitude=116.4018,
        )

        unique = self.resolver.deduplicate([wenhua, palace])

        self.assertEqual(len(unique), 1)
        self.assertEqual(unique[0].name, "故宫博物院")
        self.assertEqual(unique[0].visit_key, "provider:palace")

    def test_explicit_child_must_visit_selects_child_representation_once(self):
        palace = attraction("故宫博物院", "palace")
        wenhua = attraction("文华殿", "wenhua", parent_poi_id="palace")

        unique = self.resolver.deduplicate(
            [palace, wenhua],
            must_visit=["文华殿"],
        )

        self.assertEqual([item.name for item in unique], ["文华殿"])

    def test_compound_child_name_defaults_to_parent_display_entity(self):
        palace = attraction("故宫博物院", "palace")
        wenhua = attraction(
            "故宫博物院-文华殿", "wenhua", parent_poi_id="palace"
        )

        unique = self.resolver.deduplicate([wenhua, palace])

        self.assertEqual([item.name for item in unique], ["故宫博物院"])
        self.assertIn("文华殿", unique[0].description)

    def test_multi_day_duplicate_is_removed_by_canonical_identity(self):
        day_one = DayPlan(
            date="2026-10-10",
            day_index=0,
            description="one",
            transportation="public transit",
            accommodation="budget hotel",
            attractions=[attraction("故宫", "palace")],
        )
        day_two = DayPlan(
            date="2026-10-11",
            day_index=1,
            description="two",
            transportation="public transit",
            accommodation="budget hotel",
            attractions=[attraction("故宫博物院", "palace")],
        )
        plan = TripPlan(
            city="北京",
            start_date="2026-10-10",
            end_date="2026-10-11",
            days=[day_one, day_two],
            overall_suggestions="test",
        )

        removed = self.resolver.deduplicate_plan(plan)

        self.assertEqual(removed, ["故宫博物院"])
        self.assertEqual(
            sum(len(day.attractions) for day in plan.days),
            1,
        )


if __name__ == "__main__":
    unittest.main()
