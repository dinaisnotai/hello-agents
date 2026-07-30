import unittest
from unittest.mock import patch

from app.agents.trip_planner_agent import ConstraintChecker, MultiAgentTripPlanner, POICollector
from app.models.schemas import Attraction, DayPlan, Hotel, Location, POIInfo, TripPlan, TripRequest
from app.services.amap_service import AmapService
from app.services.place_name_service import place_names_match


def make_request(**overrides):
    values = {
        "city": "北京",
        "start_date": "2026-08-01",
        "end_date": "2026-08-01",
        "travel_days": 1,
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
        "must_visit": ["国博"],
    }
    values.update(overrides)
    return TripRequest(**values)


class PlaceNameMatchingTest(unittest.TestCase):
    def test_matches_guobo_alias_to_canonical_names(self):
        self.assertTrue(place_names_match("国博", "国家博物馆"))
        self.assertTrue(place_names_match("国博", "中国国家博物馆"))
        self.assertFalse(place_names_match("国博", "首都博物馆"))

    def test_fallback_search_prioritizes_alias_match(self):
        service = AmapService()
        service.settings.amap_api_key = ""

        result = service.search_poi("国博", "北京")

        self.assertEqual(result[0].name, "国家博物馆")

    def test_collector_deduplicates_and_prioritizes_must_visit_alias(self):
        service = AmapService()
        service.settings.amap_api_key = ""
        attractions = POICollector(service).collect_attractions(
            make_request(preferences=["历史文化"])
        )

        museum_matches = [item for item in attractions if item.name == "国家博物馆"]
        self.assertEqual(len(museum_matches), 1)
        self.assertEqual(attractions[0].name, "国家博物馆")
        self.assertEqual(attractions[0].score, 100)

    def test_collector_deduplicates_alias_variants_from_poi_search(self):
        service = AmapService()
        results = [
            POIInfo(
                id="palace-1",
                name="故宫",
                location=Location(longitude=116.397, latitude=39.916),
            ),
            POIInfo(
                id="palace-2",
                name="故宫博物院",
                location=Location(longitude=116.397, latitude=39.916),
            ),
        ]
        with patch.object(service, "search_poi", return_value=results):
            attractions = POICollector(service).collect_attractions(
                make_request(preferences=["历史文化"], must_visit=["故宫"])
            )

        self.assertEqual(len(attractions), 1)
        self.assertEqual(attractions[0].score, 100)

    def test_hotel_collector_rejects_non_lodging_pois_instead_of_numbering_them(self):
        service = AmapService()
        non_lodging = [
            POIInfo(
                id="museum-1",
                name="国家博物馆",
                type="风景名胜;博物馆",
                location=Location(longitude=116.407, latitude=39.904),
            ),
            POIInfo(
                id="mall-1",
                name="王府井商场",
                type="购物服务;商场",
                location=Location(longitude=116.411, latitude=39.908),
            ),
        ]
        with patch.object(service, "search_poi", return_value=non_lodging):
            hotels = POICollector(service).collect_hotels(make_request())

        self.assertEqual(len(hotels), 1)
        self.assertIn("待确认", hotels[0].name)
        self.assertNotIn("推荐酒店", hotels[0].name)
        self.assertIsNone(hotels[0].location)

    def test_hotel_collector_preserves_verified_provider_hotel_name(self):
        service = AmapService()
        provider_hotel = POIInfo(
            id="hotel-1",
            name="北京东方酒店",
            type="住宿服务;宾馆酒店",
            location=Location(longitude=116.40, latitude=39.91),
        )
        with patch.object(service, "search_poi", return_value=[provider_hotel]):
            hotels = POICollector(service).collect_hotels(make_request())

        self.assertEqual(hotels[0].name, "北京东方酒店")
        self.assertIsNotNone(hotels[0].location)

    def test_each_must_visit_is_kept_exactly_once_when_candidates_are_truncated(self):
        service = AmapService()
        ordinary = [
            POIInfo(
                id=f"ordinary-{index}",
                name=f"普通景点{index}",
                type="历史文化",
                location=Location(longitude=116.0 + index / 100, latitude=39.9),
            )
            for index in range(35)
        ]
        palace = POIInfo(
            id="palace",
            name="故宫博物院",
            type="博物馆",
            location=Location(longitude=116.397, latitude=39.916),
        )

        def search(keyword, city):
            if keyword == "历史文化":
                return ordinary + [palace]
            if keyword == "故宫":
                return [palace]
            return []

        with patch.object(service, "search_poi", side_effect=search):
            attractions = POICollector(service).collect_attractions(
                make_request(preferences=["历史文化"], must_visit=["故宫"])
            )

        matches = [item for item in attractions if place_names_match("故宫", item.name)]
        self.assertEqual(len(attractions), 30)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].score, 100)

    def test_collector_filters_avoided_categories(self):
        service = AmapService()
        results = [
            POIInfo(
                id="mall",
                name="测试商场",
                type="购物服务;商场",
                location=Location(longitude=116.3, latitude=39.9),
            ),
            POIInfo(
                id="museum",
                name="测试博物馆",
                type="科教文化;博物馆",
                location=Location(longitude=116.4, latitude=39.9),
            ),
        ]
        with patch.object(service, "search_poi", return_value=results):
            attractions = POICollector(service).collect_attractions(
                make_request(
                    preferences=["城市体验"],
                    must_visit=[],
                    avoid_categories=["shopping"],
                )
            )

        self.assertEqual([item.name for item in attractions], ["测试博物馆"])
        self.assertTrue(all("购物" not in (item.category or "") for item in attractions))

    def test_fallback_park_category_filters_tiantan_and_summer_palace(self):
        service = AmapService()
        service.settings.amap_api_key = ""

        attractions = POICollector(service).collect_attractions(
            make_request(
                preferences=["历史文化"],
                must_visit=[],
                avoid_categories=["park"],
            )
        )

        names = {item.name for item in attractions}
        self.assertNotIn("天坛公园", names)
        self.assertNotIn("颐和园", names)

    def test_unlisted_poi_is_classified_from_live_search_result(self):
        service = AmapService()
        results = [
            POIInfo(
                id="xidan-joy-city",
                name="西单大悦城",
                type="购物服务;商场;购物中心",
                location=Location(longitude=116.372, latitude=39.91),
            ),
            POIInfo(
                id="capital-museum",
                name="首都博物馆",
                type="科教文化;博物馆",
                location=Location(longitude=116.341, latitude=39.907),
            ),
        ]
        with patch.object(service, "search_poi", return_value=results):
            attractions = POICollector(service).collect_attractions(
                make_request(
                    preferences=["城市体验"],
                    must_visit=[],
                    avoid_categories=["shopping"],
                )
            )

        self.assertEqual([item.name for item in attractions], ["首都博物馆"])

    def test_unlisted_poi_can_still_be_used_as_a_must_visit(self):
        service = AmapService()
        joy_city = POIInfo(
            id="xidan-joy-city",
            name="西单大悦城",
            type="购物服务;商场;购物中心",
            location=Location(longitude=116.372, latitude=39.91),
        )
        with patch.object(service, "search_poi", return_value=[joy_city]):
            attractions = POICollector(service).collect_attractions(
                make_request(
                    preferences=["城市体验"],
                    must_visit=["西单大悦城"],
                    avoid_categories=[],
                )
            )

        self.assertEqual([item.name for item in attractions], ["西单大悦城"])
        self.assertEqual(attractions[0].score, 100)

    def test_collector_handles_empty_search_results(self):
        service = AmapService()
        with patch.object(service, "search_poi", return_value=[]):
            attractions = POICollector(service).collect_attractions(
                make_request(preferences=["历史文化"], must_visit=["不存在的景点"])
            )
            hotel = POICollector(service).collect_hotel(make_request())

        self.assertEqual(attractions, [])
        self.assertEqual(hotel.name, "北京待确认酒店")

    def test_unknown_city_does_not_fabricate_pois(self):
        service = AmapService()
        service.settings.amap_api_key = ""

        self.assertEqual(service.search_poi("博物馆", "不存在市"), [])

    def test_day_assignment_does_not_reuse_attractions_when_candidates_are_few(self):
        request = make_request(travel_days=3, end_date="2026-08-03", must_visit=[])
        attractions = [
            Attraction(
                name=name,
                location=Location(longitude=116.397 + index, latitude=39.916),
            )
            for index, name in enumerate(["故宫", "故宫博物院", "天坛公园"])
        ]

        planner = MultiAgentTripPlanner.__new__(MultiAgentTripPlanner)
        days = planner._assign_days(request, attractions, Hotel(name="测试酒店"))
        assigned_names = [item.name for day in days for item in day.attractions]

        self.assertEqual(len(assigned_names), 2)
        self.assertEqual(len(assigned_names), len(set(assigned_names)))

    def test_constraint_checker_accepts_alias_in_itinerary(self):
        request = make_request()
        attraction = Attraction(
            name="中国国家博物馆",
            location=Location(longitude=116.407, latitude=39.904),
        )
        day = DayPlan(
            date="2026-08-01",
            day_index=0,
            description="北京一日游",
            transportation="公共交通",
            accommodation="经济型酒店",
            attractions=[attraction],
        )
        plan = TripPlan(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-01",
            days=[day],
            overall_suggestions="",
        )

        report = ConstraintChecker().check(plan, request)

        self.assertTrue(report.passed)
        self.assertTrue(report.items[0].passed)

    def test_constraint_report_includes_avoided_category(self):
        request = make_request(must_visit=[], avoid_categories=["park"])
        attraction = Attraction(
            name="天坛公园",
            category="公园;文物古迹",
            location=Location(longitude=116.407, latitude=39.904),
        )
        day = DayPlan(
            date="2026-08-01",
            day_index=0,
            description="北京一日游",
            transportation="公共交通",
            accommodation="经济型酒店",
            attractions=[attraction],
        )
        plan = TripPlan(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-01",
            days=[day],
            overall_suggestions="",
        )

        report = ConstraintChecker().check(plan, request)

        self.assertFalse(report.passed)
        self.assertEqual(report.items[0].name, "避开类型：公园园林")
        self.assertFalse(report.items[0].passed)
        self.assertIn("天坛公园", report.items[0].actual)


if __name__ == "__main__":
    unittest.main()
