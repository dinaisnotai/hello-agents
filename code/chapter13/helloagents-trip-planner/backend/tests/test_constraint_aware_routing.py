import unittest

from app.agents.trip_planner_agent import (
    ConstraintChecker,
    MultiAgentTripPlanner,
    PlannerReviewer,
    POICollector,
    RouteEvaluator,
)
from app.models.schemas import (
    Attraction,
    DayPlan,
    Hotel,
    Location,
    POIInfo,
    TripPlan,
    TripRequest,
)
from app.services.amap_service import AmapService
from app.services.poi_metadata_service import (
    build_preference_profile,
    enrich_attraction,
)
from app.services.spatial_planner import SpatialItineraryPlanner


def request(**updates):
    data = {
        "city": "北京",
        "start_date": "2026-08-01",
        "end_date": "2026-08-03",
        "travel_days": 3,
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
        "pace": "balanced",
    }
    data.update(updates)
    return TripRequest(**data)


def attraction(name, area, longitude, score=70, **updates):
    data = {
        "name": name,
        "area": area,
        "location": Location(longitude=longitude, latitude=39.9),
        "score": score,
    }
    data.update(updates)
    return Attraction(**data)


class ConstraintAwareRoutingTest(unittest.TestCase):
    def test_first_visit_profile_promotes_classics_over_niche_museums(self):
        trip_request = request(
            first_visit=True,
            prefer_classic=True,
            free_text_input="第一次来北京",
        )
        profile = build_preference_profile(trip_request)
        palace = enrich_attraction(
            attraction("故宫博物院", "", 116.397), trip_request, profile
        )
        customs = enrich_attraction(
            attraction("中国海关博物馆", "", 116.41), trip_request, profile
        )

        self.assertTrue(profile.first_visit)
        self.assertEqual(palace.area, "东城区")
        self.assertEqual(palace.suggested_duration_minutes, 180)
        self.assertGreater(palace.score, customs.score)
        self.assertGreater(palace.first_visit_priority, customs.first_visit_priority)

    def test_empty_preferences_use_classic_baseline_instead_of_museum_query(self):
        class RecordingAmap:
            def __init__(self):
                self.keywords = []

            def search_poi(self, keyword, city):
                self.keywords.append(keyword)
                return []

        amap = RecordingAmap()
        trip_request = request(preferences=[])
        profile = build_preference_profile(trip_request)

        POICollector(amap).collect_attractions(trip_request)

        self.assertFalse(profile.first_visit)
        self.assertTrue(profile.prefer_classic)
        self.assertEqual(
            amap.keywords[:4],
            ["经典景点", "城市地标", "历史名胜", "自然景观"],
        )
        self.assertNotIn("博物馆", amap.keywords[:4])

    def test_neutral_selection_caps_niche_cultural_venues(self):
        classics = [
            attraction(
                name,
                area,
                longitude,
                score=score,
                first_visit_priority=priority,
                categories=categories,
            )
            for name, area, longitude, score, priority, categories in (
                ("故宫", "东城区", 116.397, 96, 10, ["historic"]),
                ("天坛", "东城区", 116.410, 94, 10, ["historic", "park"]),
                ("颐和园", "海淀区", 116.270, 93, 10, ["historic", "park"]),
                ("景山", "东城区", 116.400, 84, 8, ["park"]),
                ("什刹海", "西城区", 116.385, 82, 8, ["natural"]),
            )
        ]
        niche = [
            attraction(
                name,
                "东城区",
                116.42 + index * 0.001,
                score=91 - index,
                first_visit_priority=3,
                categories=["museum"],
            )
            for index, name in enumerate(
                (
                    "北京古代建筑博物馆",
                    "中国美术馆",
                    "东四胡同博物馆",
                    "中国海关博物馆",
                )
            )
        ]

        selected = SpatialItineraryPlanner().select_attractions(
            [*niche, *classics],
            travel_days=3,
            pace="balanced",
            must_visit=[],
        )

        self.assertTrue({item.name for item in classics}.issubset(
            {item.name for item in selected}
        ))
        self.assertLessEqual(
            sum(
                SpatialItineraryPlanner._is_niche_cultural(item)
                for item in selected
            ),
            2,
        )

    def test_explicit_museum_preference_relaxes_diversity_cap(self):
        museums = [
            attraction(
                f"专题博物馆{index}",
                "东城区",
                116.40 + index * 0.001,
                score=90 - index,
                first_visit_priority=3,
                categories=["museum"],
                score_breakdown={"preference_match": 30},
            )
            for index in range(3)
        ]

        selected = SpatialItineraryPlanner().select_attractions(
            museums,
            travel_days=1,
            pace="balanced",
            must_visit=[],
            per_day_limit=3,
        )

        self.assertEqual(len(selected), 3)

    def test_llm_three_poi_plan_is_supplemented_with_classic_coverage(self):
        planner = MultiAgentTripPlanner()
        planner.amap_service.settings.amap_api_key = ""
        trip_request = request(preferences=[])
        specialist_candidates = [
            attraction(
                "故宫博物院",
                "东城区",
                116.397,
                category="博物馆",
            ),
            attraction(
                "中国考古博物馆",
                "朝阳区",
                116.420,
                category="博物馆",
            ),
            attraction(
                "八达岭长城",
                "延庆区",
                116.016,
                category="景点",
            ),
        ]
        specialist_candidates[1].location = Location(
            longitude=116.420, latitude=40.010
        )
        specialist_candidates[2].location = Location(
            longitude=116.016, latitude=40.356
        )
        hotel = Hotel(
            name="测试酒店",
            location=Location(longitude=116.397, latitude=39.916),
        )

        plan = planner.build_plan_from_inputs(
            trip_request,
            specialist_candidates,
            hotel,
            [],
            [],
        )

        planned_names = {
            item.name
            for day in plan.days
            for item in day.attractions
        }
        self.assertGreater(len(planned_names), 3)
        self.assertTrue(
            {"故宫博物院", "天坛公园", "颐和园"}.issubset(planned_names)
        )
        self.assertTrue(
            all(
                len(day.attractions) >= 2 or day.daily_duration_minutes >= 420
                for day in plan.days
                if day.daily_travel_minutes
                <= SpatialItineraryPlanner.remote_travel_threshold_minutes
            )
        )
        self.assertTrue(all({meal.type for meal in day.meals} == {"lunch", "dinner"} for day in plan.days))

    def test_history_theme_prefers_classics_over_low_score_museums(self):
        planner = MultiAgentTripPlanner()
        planner.amap_service.settings.amap_api_key = ""
        trip_request = request(preferences=["历史"])
        attractions = planner.poi_collector.collect_attractions(trip_request)
        hotel = planner.poi_collector.collect_hotel(trip_request)

        plan = planner.build_plan_from_inputs(
            trip_request, attractions, hotel, [], []
        )
        planned_names = {
            item.name for day in plan.days for item in day.attractions
        }

        self.assertIn("故宫博物院", planned_names)
        self.assertIn("天坛公园", planned_names)
        self.assertNotIn("北京艺术博物馆", planned_names)
        self.assertNotIn("北京古代建筑博物馆", planned_names)
        self.assertNotIn("北京湖广会馆", planned_names)
        self.assertNotIn("万寿寺", planned_names)

    def test_explicit_areas_are_allocated_before_daily_route_ordering(self):
        candidates = [
            attraction("故宫", "东城区", 116.39, 100),
            attraction("景山", "东城区", 116.40, 80),
            attraction("颐和园", "海淀区", 116.27, 95),
            attraction("圆明园", "海淀区", 116.30, 75),
            attraction("798", "朝阳区", 116.50, 70),
            attraction("电影博物馆", "朝阳区", 116.55, 60),
        ]

        groups = SpatialItineraryPlanner().plan(
            candidates,
            travel_days=3,
            pace="balanced",
            must_visit=[],
            hotel_location=Location(longitude=116.4, latitude=39.9),
            transportation="公共交通",
        )

        self.assertEqual(len(groups), 3)
        self.assertTrue(all(len({item.area for item in group}) == 1 for group in groups))

    def test_short_public_transport_leg_is_changed_to_walking(self):
        service = AmapService()
        service.settings.amap_api_key = ""
        hotel = Hotel(
            name="酒店",
            location=Location(longitude=116.397, latitude=39.9),
        )
        day = DayPlan(
            date="2026-08-01",
            day_index=0,
            description="短途",
            transportation="公共交通",
            accommodation="经济型酒店",
            hotel=hotel,
            attractions=[attraction("景点", "东城区", 116.409)],
        )

        routes = RouteEvaluator(service).build_day_routes(day, "北京")

        self.assertEqual([route.route_type for route in routes], ["walking", "walking"])
        self.assertTrue(all(route.distance_meters < 1500 for route in routes))

    def test_area_jump_is_reported_as_constraint_failure(self):
        day = DayPlan(
            date="2026-08-01",
            day_index=0,
            description="跨区",
            transportation="公共交通",
            accommodation="经济型酒店",
            attractions=[
                attraction("A", "东城区", 116.3),
                attraction("B", "海淀区", 116.4),
                attraction("C", "朝阳区", 116.5),
            ],
        )
        plan = TripPlan(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-01",
            days=[day],
            overall_suggestions="",
        )

        report = ConstraintChecker().check(plan, request(travel_days=1))
        area_item = next(item for item in report.items if item.name == "每日区域聚合")

        self.assertFalse(area_item.passed)
        self.assertTrue(report.passed)
        self.assertLess(report.score, 1)

    def test_repair_removes_low_value_remote_time_expensive_poi_first(self):
        planner = MultiAgentTripPlanner.__new__(MultiAgentTripPlanner)
        planner.spatial_planner = SpatialItineraryPlanner()
        planner.reviewer = PlannerReviewer()
        classic = attraction(
            "故宫",
            "东城区",
            116.39,
            score=95,
            popularity=10,
            first_visit_priority=10,
            visit_duration=180,
        )
        niche = attraction(
            "中国海关博物馆",
            "东城区",
            117.2,
            score=20,
            popularity=3,
            first_visit_priority=2,
            visit_duration=240,
        )
        day = DayPlan(
            date="2026-08-01",
            day_index=0,
            description="超时",
            transportation="公共交通",
            accommodation="经济型酒店",
            attractions=[classic, niche],
            daily_duration_minutes=900,
        )
        plan = TripPlan(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-01",
            days=[day],
            overall_suggestions="",
        )

        action = planner._apply_next_repair(
            plan,
            request(travel_days=1, pace="relaxed"),
            [classic, niche],
        )

        self.assertEqual(action[0], "remove_optional_attraction_to_reduce_daily_duration")
        self.assertEqual([item.name for item in day.attractions], ["故宫"])

    def test_soft_review_exposes_four_scores_and_warnings(self):
        day = DayPlan(
            date="2026-08-01",
            day_index=0,
            description="长距离",
            transportation="公共交通",
            accommodation="经济型酒店",
            attractions=[attraction("故宫", "东城区", 116.39, score=95)],
            daily_distance_km=40,
            daily_duration_minutes=400,
        )
        plan = TripPlan(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-01",
            days=[day],
            overall_suggestions="",
        )

        reviewed = PlannerReviewer().review(plan, request(travel_days=1))

        self.assertLess(reviewed.review_scores.distance_score, 100)
        self.assertEqual(
            reviewed.review_scores.score, reviewed.review_scores.route_score
        )
        self.assertTrue(reviewed.review_scores.route_score >= 0)
        self.assertIn("Day1移动距离较长（40.0km）", reviewed.review_scores.warnings)

    def test_restaurants_are_excluded_from_attraction_candidates(self):
        restaurant = POIInfo(
            name="王府井烤鸭餐厅",
            type="餐饮服务;中餐厅",
            location=Location(longitude=116.4, latitude=39.9),
        )
        museum = POIInfo(
            name="故宫博物院",
            type="博物馆",
            location=Location(longitude=116.397, latitude=39.916),
        )

        self.assertTrue(POICollector._is_meal_poi(restaurant))
        self.assertFalse(POICollector._is_meal_poi(museum))

    def test_beijing_day_is_filled_with_same_area_pois_until_time_budget(self):
        planner = MultiAgentTripPlanner()
        planner.amap_service.settings.amap_api_key = ""
        trip_request = request(
            first_visit=True,
            prefer_classic=True,
            daily_start_time="09:00",
            daily_end_time="18:00",
            max_daily_walk_km=8,
        )
        attractions = planner.poi_collector.collect_attractions(trip_request)
        hotel = planner.poi_collector.collect_hotel(trip_request)

        plan = planner.build_plan_from_inputs(
            trip_request, attractions, hotel, [], []
        )
        haidian_day = next(
            day
            for day in plan.days
            if any(item.name == "颐和园" for item in day.attractions)
        )

        self.assertGreaterEqual(len(haidian_day.attractions), 2)
        self.assertTrue(
            any(
                item.name in {"圆明园", "北京大学"}
                for item in haidian_day.attractions
            )
        )
        self.assertGreaterEqual(haidian_day.day_utilization_score, 70)
        self.assertLessEqual(len({item.area for item in haidian_day.attractions}), 2)
        self.assertTrue(plan.candidate_debug)
        self.assertIn("total_score", plan.candidate_debug[0].score_breakdown)
        self.assertIn("diversity", plan.review_scores.score_breakdown)


if __name__ == "__main__":
    unittest.main()
