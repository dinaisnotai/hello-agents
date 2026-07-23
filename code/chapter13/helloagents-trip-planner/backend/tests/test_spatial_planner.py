import unittest

from app.agents.trip_planner_agent import MultiAgentTripPlanner, RouteEvaluator
from app.models.schemas import Attraction, DayPlan, Hotel, Location, TripRequest
from app.services.amap_service import AmapService
from app.services.spatial_planner import (
    SpatialItineraryPlanner,
    estimate_visit_duration,
    get_pace_profile,
    haversine_meters,
)


def make_attraction(name, longitude, latitude=39.9, score=50):
    return Attraction(
        name=name,
        poi_id=name,
        location=Location(longitude=longitude, latitude=latitude),
        score=score,
    )


def route_length(attractions):
    return sum(
        haversine_meters(left.location, right.location)
        for left, right in zip(attractions, attractions[1:])
    )


def make_request(pace="balanced", travel_days=1, must_visit=None):
    return TripRequest(
        city="测试市",
        start_date="2026-08-01",
        end_date=f"2026-08-{travel_days:02d}",
        travel_days=travel_days,
        transportation="公共交通",
        accommodation="经济型酒店",
        pace=pace,
        must_visit=must_visit or [],
    )


class SpatialItineraryPlannerTest(unittest.TestCase):
    def setUp(self):
        self.planner = SpatialItineraryPlanner()

    def test_interleaved_scores_are_grouped_by_geography_and_reduce_route(self):
        attractions = [
            make_attraction("东区1", 116.00, score=60),
            make_attraction("西区1", 117.00, score=59),
            make_attraction("东区2", 116.01, score=58),
            make_attraction("西区2", 117.01, score=57),
            make_attraction("东区3", 116.02, score=56),
            make_attraction("西区3", 117.02, score=55),
        ]

        groups = self.planner.plan(
            attractions, travel_days=2, pace="balanced", must_visit=[]
        )

        group_names = [{item.name[:2] for item in group} for group in groups]
        self.assertEqual(group_names, [{"东区"}, {"西区"}])

        legacy_groups = [attractions[:3], attractions[3:]]
        spatial_distance = sum(route_length(group) for group in groups)
        legacy_distance = sum(route_length(group) for group in legacy_groups)
        self.assertLess(spatial_distance, legacy_distance)

    def test_same_candidates_in_different_order_produce_same_plan(self):
        attractions = [
            make_attraction("A1", 116.00, score=60),
            make_attraction("B1", 117.00, score=59),
            make_attraction("A2", 116.01, score=58),
            make_attraction("B2", 117.01, score=57),
        ]

        first = self.planner.plan(
            attractions, travel_days=2, pace="balanced", must_visit=[]
        )
        second = self.planner.plan(
            list(reversed(attractions)),
            travel_days=2,
            pace="balanced",
            must_visit=[],
        )

        first_names = [[item.name for item in group] for group in first]
        second_names = [[item.name for item in group] for group in second]
        self.assertEqual(first_names, second_names)

    def test_nearest_neighbor_orders_each_day_from_hotel(self):
        hotel_location = Location(longitude=116.00, latitude=39.9)
        attractions = [
            make_attraction("最远", 116.03, score=60),
            make_attraction("最近", 116.01, score=50),
            make_attraction("中间", 116.02, score=40),
        ]

        groups = self.planner.plan(
            attractions,
            travel_days=1,
            pace="balanced",
            must_visit=[],
            hotel_location=hotel_location,
        )

        self.assertEqual([item.name for item in groups[0]], ["最近", "中间", "最远"])

    def test_all_must_visits_survive_when_they_exceed_pace_capacity(self):
        attractions = [
            make_attraction("必去1", 116.00, score=100),
            make_attraction("必去2", 116.01, score=100),
            make_attraction("必去3", 116.02, score=100),
            make_attraction("普通景点", 116.03, score=99),
        ]

        selected = self.planner.select_attractions(
            attractions,
            travel_days=1,
            pace="relaxed",
            must_visit=["必去1", "必去2", "必去3"],
        )

        self.assertEqual({item.name for item in selected}, {"必去1", "必去2", "必去3"})

    def test_pace_controls_daily_count_and_visit_duration(self):
        attractions = [
            make_attraction(f"景点{index}", 116 + index / 100, score=100 - index)
            for index in range(6)
        ]
        expected = {
            "relaxed": (2, 150),
            "balanced": (3, 120),
            "packed": (4, 90),
        }

        for pace, (expected_count, expected_duration) in expected.items():
            with self.subTest(pace=pace):
                request = make_request(pace=pace)
                trip_planner = MultiAgentTripPlanner.__new__(MultiAgentTripPlanner)
                days = trip_planner._assign_days(
                    request, attractions, Hotel(name="测试酒店")
                )

                self.assertEqual(len(days[0].attractions), expected_count)
                self.assertTrue(
                    all(
                        item.visit_duration == expected_duration
                        for item in days[0].attractions
                    )
                )
                profile = get_pace_profile(pace)
                self.assertEqual(profile.attractions_per_day, expected_count)

    def test_remote_great_wall_activity_is_not_forced_to_fill_three_slots(self):
        hotel = Location(longitude=116.40, latitude=39.90)
        attractions = [
            Attraction(
                name="八达岭长城",
                category="风景名胜;长城",
                location=Location(longitude=116.00, latitude=40.35),
                score=100,
            ),
            make_attraction("市区博物馆", 116.41, score=90),
            make_attraction("市区公园", 116.42, score=80),
        ]

        groups = self.planner.plan(
            attractions,
            travel_days=1,
            pace="balanced",
            must_visit=["八达岭长城"],
            hotel_location=hotel,
            transportation="公共交通",
        )

        self.assertEqual([item.name for item in groups[0]], ["八达岭长城"])
        self.assertEqual(groups[0][0].visit_duration, 240)
        timing = self.planner.estimate_day_timing(
            groups[0], hotel, "公共交通", get_pace_profile("balanced")
        )
        self.assertGreater(timing.travel_minutes, 180)

    def test_visit_duration_uses_activity_type_before_pace_adjustment(self):
        self.assertEqual(
            estimate_visit_duration("八达岭长城", "风景名胜", "balanced"), 240
        )
        self.assertEqual(
            estimate_visit_duration("国家博物馆", "博物馆", "balanced"), 180
        )
        self.assertEqual(
            estimate_visit_duration("普通景点", "城市观光", "balanced"), 120
        )

    def test_hotel_outbound_and_return_legs_are_included_in_day_metrics(self):
        service = AmapService()
        service.settings.amap_api_key = ""
        hotel = Hotel(
            name="测试酒店",
            location=Location(longitude=116.40, latitude=39.90),
        )
        attraction = make_attraction("测试景点", 116.42)
        attraction.visit_duration = 120
        day = DayPlan(
            date="2026-08-01",
            day_index=0,
            description="测试行程",
            transportation="公共交通",
            accommodation="经济型酒店",
            hotel=hotel,
            attractions=[attraction],
        )

        day.route_segments = RouteEvaluator(service).build_day_routes(day, "测试市")
        trip_planner = MultiAgentTripPlanner.__new__(MultiAgentTripPlanner)
        trip_planner.spatial_planner = SpatialItineraryPlanner()
        trip_planner._update_day_metrics(day, "balanced")

        self.assertEqual(
            [(item.origin, item.destination) for item in day.route_segments],
            [("测试酒店", "测试景点"), ("测试景点", "测试酒店")],
        )
        self.assertGreater(day.daily_travel_minutes, 0)
        self.assertGreater(day.daily_distance_km, 0)
        self.assertEqual(day.daily_walking_distance_km, 0)
        self.assertEqual(day.daily_buffer_minutes, 60)
        self.assertEqual(
            day.daily_duration_minutes,
            day.daily_visit_minutes
            + day.daily_travel_minutes
            + day.daily_buffer_minutes,
        )


if __name__ == "__main__":
    unittest.main()
