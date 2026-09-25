import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from app.models.schemas import Attraction, DayPlan, Hotel, Location, POIInfo, RouteInfo, TripRequest
from app.agents.trip_planner_agent import BudgetEstimator, RouteEvaluator
from app.services.hotel_location import match_hotel_location
from app.services.mixed_route_selector import select_mixed_route
from app.services.visit_facts import apply_visit_facts
from app.services.meal_planner import prepare_meals
from app.services.rag_service import TravelGuideRAG, EmbeddingError
from app.services.accommodation_selector import AccommodationSelector


def request(**changes):
    fields = dict(city="北京", start_date="2026-10-01", end_date="2026-10-03", travel_days=3,
                  transportation="混合", accommodation="豪华酒店", budget_limit=5000, party_size=2, max_daily_walk_km=8)
    fields.update(changes)
    return TripRequest(**fields)


def loc(x=116.4):
    return Location(longitude=x, latitude=39.9)


class DecisionsRegressionTest(unittest.TestCase):
    def test_supplier_hotel_uses_unique_map_lodging_not_address_centroid(self):
        poi = POIInfo(id="real", name="北京燕莎中心凯宾斯基饭店", type="住宿服务;宾馆酒店", address="亮马桥路50号", location=loc())
        amap = SimpleNamespace(settings=SimpleNamespace(amap_api_key="test"), search_poi=lambda *args: [poi])
        matched = match_hotel_location({"name": "Kempinski Hotel Beijing Yansha Center", "address": "50 Liangmaqiao Road"}, "北京", amap)
        self.assertEqual(matched.id, "real")
        amap.search_poi = lambda *args: [poi, poi.model_copy(update={"id": "ambiguous"})]
        self.assertIsNone(match_hotel_location({"name": "Kempinski", "address": "50 Liangmaqiao Road"}, "北京", amap))
        amap.search_poi = lambda *args: [poi.model_copy(update={"location_source": "estimated"})]
        self.assertIsNone(match_hotel_location({"name": poi.name, "address": "50 Liangmaqiao Road"}, "北京", amap))

    def test_mixed_transport_switches_away_from_actual_long_walk(self):
        def route(**kwargs):
            mode = kwargs['route_type']
            return RouteInfo(route_type=mode, distance=2700 if mode == 'walking' else 5000,
                             duration=2100 if mode == 'walking' else 1800 if mode == 'transit' else 900,
                             walking_distance=2000 if mode == 'transit' else 0, description='[amap] test')
        result = select_mixed_route(SimpleNamespace(route_between_pois=route),
                                   dict(origin=loc(), destination=loc(116.41)), request(), 800)
        self.assertEqual(result.route_type, 'driving')
        self.assertIn('上下车点', result.description)

    def test_mixed_transport_does_not_choose_unverified_taxi_over_real_transit(self):
        def route(**kwargs):
            mode = kwargs['route_type']
            return RouteInfo(route_type=mode, distance=4000, duration=600,
                             walking_distance=1800 if mode == 'transit' else 0,
                             description='[amap] test' if mode == 'transit' else '[estimated] test')
        result = select_mixed_route(SimpleNamespace(route_between_pois=route),
                                   dict(origin=loc(), destination=loc(116.5)), request(), 500)
        self.assertEqual(result.route_type, 'transit')

    def test_seasonal_ticket_price_free_and_unknown_are_distinct(self):
        palace = Attraction(name="故宫", location=loc())
        apply_visit_facts(palace, "北京", "2026-10-01")
        self.assertEqual(palace.ticket_price, 60)
        self.assertEqual(palace.latest_entry_time, "16:00")
        apply_visit_facts(palace, "北京", "2026-11-01")
        self.assertEqual(palace.ticket_price, 40)
        apply_visit_facts(palace, "北京", "2027-11-01")
        self.assertEqual(palace.ticket_price_status, 'unknown')
        museum = Attraction(name="国博", location=loc())
        apply_visit_facts(museum, "北京", "2026-10-01")
        self.assertEqual(museum.ticket_price_status, 'confirmed_free')
        unknown = Attraction(name="未核实公园", location=loc())
        day = DayPlan(date="2026-10-01", day_index=0, description="", transportation="混合", accommodation="", attractions=[museum, unknown])
        budget = BudgetEstimator().estimate([day], request())
        self.assertEqual(budget.unpriced_attractions, [unknown.name])
        self.assertFalse(budget.pricing_complete)

    def test_meals_exclude_cafe_and_consider_hotel_neighborhood(self):
        cafe = POIInfo(id="c", name="博物馆咖啡厅", type="餐饮服务", location=loc())
        dinner = POIInfo(id="r", name="酒店旁中餐厅", type="餐饮服务", location=loc(116.501))
        day = DayPlan(date="2026-10-01", day_index=0, description="", transportation="混合", accommodation="",
                      attractions=[Attraction(name="某博物馆", location=loc())], hotel=Hotel(name="酒店", location=loc(116.5)))
        queries = []
        def nearby(query, city, location, radius):
            queries.append((query, location, radius))
            return [dinner] if location == day.hotel.location else [cafe]
        prepare_meals(
            day,
            request(),
            SimpleNamespace(
                settings=SimpleNamespace(amap_api_key="test"),
                search_nearby_poi=nearby,
                search_poi=lambda *args: self.fail('餐饮周边查询不应回退到名称全文搜索'),
            ),
        )
        self.assertEqual(day.meals[0].source, 'unconfirmed')
        self.assertEqual(day.meals[1].poi_id, 'r')
        self.assertTrue(any(location == day.hotel.location for _, location, _ in queries))

    def test_route_uses_exit_of_previous_and_entrance_of_next_attraction(self):
        a = Attraction(name="甲", location=loc(), exit_location=loc(116.401))
        b = Attraction(name="乙", location=loc(116.41), entrance_location=loc(116.409))
        calls = []
        def route(**kwargs):
            calls.append(kwargs)
            return RouteInfo(route_type=kwargs['route_type'], distance=800, duration=600)
        day = DayPlan(date="2026-10-01", day_index=0, description="", transportation="步行", accommodation="", attractions=[a,b])
        RouteEvaluator(SimpleNamespace(route_between_pois=route)).build_day_routes(day, "北京")
        self.assertEqual(calls[0]['origin'], a.exit_location)
        self.assertEqual(calls[0]['destination'], b.entrance_location)

    def test_rag_separates_planning_questions_when_embeddings_unavailable(self):
        class Offline:
            def embed(self, texts):
                raise EmbeddingError('offline test')
        root = Path(__file__).resolve().parents[1] / 'app/data/travel_guides'
        with patch.multiple(settings, enable_travel_knowledge=True, travel_knowledge_retrieval_mode='hybrid'):
            rag = TravelGuideRAG(root, Offline())
            evidence = rag.search_for_request(request(preferences=['自然风光']))
        self.assertTrue(any('自然' in e.title for e in evidence))
        self.assertGreaterEqual(len({e.retrieval_purpose for e in evidence}), 3)
        self.assertFalse(any(e.title == '北京旅行决策知识库' for e in evidence))

    def test_hotel_final_selection_uses_real_roundtrip_routes(self):
        day = DayPlan(date="2026-10-01", day_index=0, description="", transportation="公共交通", accommodation="",
                      attractions=[Attraction(name="某景点", location=loc())])
        hotels = [Hotel(name="远", type="豪华酒店", location=loc(116.5), estimated_cost=500),
                  Hotel(name="近", type="豪华酒店", location=loc(116.41), estimated_cost=500)]
        def route(**kwargs):
            far = '远' in (kwargs['origin_name'], kwargs['destination_name'])
            return RouteInfo(route_type='transit', distance=10000, duration=3600 if far else 1200,
                             walking_distance=1800 if far else 400, description='[amap] test')
        amap = SimpleNamespace(settings=SimpleNamespace(amap_api_key='test'), route_between_pois=route)
        hotel = AccommodationSelector().select_for_days(request(transportation='公共交通'), hotels, [day], amap)
        self.assertEqual(hotel.name, '近')
        self.assertEqual(hotel.score_breakdown['actual_commute_minutes'], 20)


if __name__ == '__main__':
    unittest.main()
