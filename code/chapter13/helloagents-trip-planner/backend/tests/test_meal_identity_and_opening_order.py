import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from app.models.schemas import Attraction, DayPlan, Hotel, Location, Meal, POIInfo, RouteInfo, RouteSegment, TripPlan, TripRequest
from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.services.meal_planner import prepare_meals, is_restaurant_record
from app.services.itinerary_quality import ItineraryCompletenessGate
from app.services.venue_policy import venue_kind


def request(**changes):
    data = dict(city="北京", start_date="2026-10-01", end_date="2026-10-01", travel_days=1,
                transportation="公共交通", accommodation="经济酒店", preferences=["美食"], daily_start_time="09:40")
    data.update(changes)
    return TripRequest(**data)


def loc(x=116.4):
    return Location(longitude=x, latitude=39.9)


def day():
    return DayPlan(date="2026-10-01", day_index=0, description="", transportation="公共交通", accommodation="",
                   attractions=[Attraction(name="某公园", location=loc())])


class MealIdentityOpeningTest(unittest.TestCase):
    def test_charging_station_with_hotel_suffix_is_never_a_restaurant(self):
        name = "依威能源汽车充电站(北京燕莎中心有限公司凯宾斯基饭店)"
        for kind in ("汽车服务;充电站", "", "餐饮服务"):
            self.assertEqual(venue_kind(name, kind), 'infrastructure')
            self.assertFalse(is_restaurant_record(name, kind))
        self.assertFalse(is_restaurant_record("某某饭店", ""))
        self.assertTrue(is_restaurant_record("北京烤鸭餐厅", "餐饮服务;中餐厅"))
        self.assertFalse(is_restaurant_record("凯宾美食廊", "餐饮服务;糕饼店;糕饼店"))

    def test_saved_bad_meal_is_not_reused_without_new_provider_evidence(self):
        d = day()
        d.meals = [Meal(type="dinner", name="依威能源充电站(凯宾斯基饭店)", source="map_poi", poi_id="bad", location=loc())]
        prepare_meals(d, request(), SimpleNamespace(settings=SimpleNamespace(amap_api_key="")))
        self.assertTrue(all(m.source == 'unconfirmed' for m in d.meals))
        self.assertFalse(any('充电站' in m.name for m in d.meals))

    def test_regional_food_wins_small_detour_but_not_unlimited_detour(self):
        ordinary = POIInfo(id='plain', name='普通中餐厅', type='餐饮服务', location=loc())
        duck = POIInfo(id='duck', name='本地烤鸭店', type='餐饮服务;中餐厅', location=loc(116.402))
        distant = POIInfo(id='far', name='远处烤鸭店', type='餐饮服务', location=loc(117))
        bad = POIInfo(id='bad', name='充电站(饭店)', type='汽车服务', location=loc())
        amap = SimpleNamespace(settings=SimpleNamespace(amap_api_key='fake'), search_poi=lambda *a: [ordinary, duck, distant, bad])
        d = day()
        prepare_meals(d, request(), amap)
        self.assertEqual(d.meals[0].poi_id, 'duck')
        self.assertEqual(d.meals[0].cuisine_hint, '烤鸭')
        self.assertEqual(d.meals[1].poi_id, 'plain')
        self.assertTrue(all(m.provider_type for m in d.meals))

    def test_previous_day_restaurant_is_penalized_when_alternative_exists(self):
        a = POIInfo(id='a', name='甲烤鸭餐厅', type='餐饮服务', location=loc())
        b = POIInfo(id='b', name='乙中餐厅', type='餐饮服务', location=loc(116.401))
        amap = SimpleNamespace(settings=SimpleNamespace(amap_api_key='fake'), search_poi=lambda *args: [a,b])
        d = day()
        prepare_meals(d, request(), amap, previous_meal_ids={'a'})
        self.assertEqual(d.meals[0].poi_id, 'b')

    def test_hotel_neighborhood_uses_confirmed_coordinate_not_hotel_name_search(self):
        hotel_location = loc(116.50)
        hotel_meal = POIInfo(
            id='hotel-meal', name='酒店旁铜锅涮肉', type='餐饮服务;中餐厅',
            location=loc(116.501),
        )
        calls = []

        def nearby(query, city, location, radius):
            calls.append((query, location, radius))
            return [hotel_meal] if location == hotel_location else []

        d = day()
        d.hotel = SimpleNamespace(name='测试酒店', location=hotel_location)
        amap = SimpleNamespace(
            settings=SimpleNamespace(amap_api_key='fake'),
            search_nearby_poi=nearby,
            search_poi=lambda *args: self.fail('不应回退到酒店名称全文搜索'),
        )
        prepare_meals(d, request(), amap)
        self.assertEqual(d.meals[1].poi_id, 'hotel-meal')
        self.assertEqual(d.meals[1].cuisine_hint, '涮肉')
        self.assertTrue(any(location == hotel_location for _, location, _ in calls))
        # /place/around already has the coordinate. Cuisine keywords stay concise so
        # they do not become a second, competing POI-name query.
        self.assertIn('中餐厅', [query for query, _, _ in calls])
        self.assertIn('烤鸭', [query for query, _, _ in calls])
        self.assertIn('涮肉', [query for query, _, _ in calls])

    def test_early_dinner_is_delayed_and_the_hotel_rest_is_visible(self):
        planner = MultiAgentTripPlanner(lightweight=True)
        d = day()
        hotel_location = loc(116.50)
        d.hotel = Hotel(name='测试酒店', location=hotel_location)
        d.meals = [
            Meal(type='lunch', name='午餐厅', location=loc(116.41)),
            Meal(type='dinner', name='酒店餐厅', location=loc(116.501)),
        ]
        d.route_segments = [
            RouteSegment(day_index=0, origin='测试酒店', destination='某公园', duration_minutes=75),
            RouteSegment(day_index=0, origin='某公园', destination='午餐厅', duration_minutes=20),
            RouteSegment(day_index=0, origin='午餐厅', destination='酒店餐厅', duration_minutes=68),
            RouteSegment(day_index=0, origin='酒店餐厅', destination='测试酒店', duration_minutes=1),
        ]

        planner._schedule_day_opening_hours(d, request())

        dinner = next(meal for meal in d.meals if meal.type == 'dinner')
        self.assertEqual(dinner.planned_arrival_time, '17:00')
        self.assertTrue(any(
            block.type == 'rest'
            and block.start_time < block.end_time
            and '晚餐前休息' in block.reason
            for block in d.schedule_blocks
        ))

    def test_quality_gate_flags_a_persisted_afternoon_dinner(self):
        d = day()
        d.meals = [
            Meal(type='lunch', name='午餐厅', planned_arrival_time='13:15', planned_departure_time='14:15'),
            Meal(type='dinner', name='晚餐厅', planned_arrival_time='15:43', planned_departure_time='16:43'),
        ]

        plan = TripPlan(
            city='北京', start_date='2026-10-01', end_date='2026-10-01',
            overall_suggestions='', days=[d],
        )
        evaluation = ItineraryCompletenessGate().evaluate(request(), plan)

        self.assertTrue(any(
            '晚餐安排在15:43' in issue.evidence
            for issue in evaluation.issues
        ))

    def test_reorder_resolves_closing_time_without_deleting_or_shortening(self):
        with patch.object(settings, 'amap_api_key', ''):
            planner = MultiAgentTripPlanner(lightweight=True)
            planner.amap_service.route_between_pois = lambda **kw: RouteInfo(route_type='transit', distance=2000, duration=1800, walking_distance=300)
            d = day()
            d.attractions = [
                Attraction(name='中国国家博物馆', location=loc(), visit_duration=180, opening_time='09:00', closing_time='18:00'),
                Attraction(name='故宫博物院', location=loc(116.41), visit_duration=180),
            ]
            planner._recalculate_day(d, request())
            self.assertEqual(d.attractions[1].opening_hours_status, 'closed')
            self.assertTrue(planner._try_reorder_for_opening_hours(d, request()))
            self.assertEqual(d.attractions[0].name, '故宫博物院')
            self.assertTrue(all(a.opening_hours_status == 'open' for a in d.attractions))
            self.assertEqual([a.visit_duration for a in d.attractions], [180,180])

    def test_impossible_opening_window_does_not_silently_shorten_visits(self):
        with patch.object(settings, 'amap_api_key', ''):
            planner = MultiAgentTripPlanner(lightweight=True)
            planner.amap_service.route_between_pois = lambda **kw: RouteInfo(route_type='walking', distance=500, duration=600)
            d = day()
            d.attractions = [Attraction(name=n, location=loc(), visit_duration=180, opening_time='09:00', closing_time='10:00', opening_hours_status='closed') for n in ('甲馆','乙馆')]
            before = d.model_dump()
            self.assertFalse(planner._try_reorder_for_opening_hours(d, request()))
            self.assertEqual(d.model_dump(), before)

    def test_twenty_one_minute_overrun_leaves_earlier_and_keeps_the_attraction(self):
        with patch.object(settings, 'amap_api_key', ''):
            planner = MultiAgentTripPlanner(lightweight=True)
            d = day()
            d.attractions = [Attraction(name='故宫博物院', location=loc(), visit_duration=180)]
            req = request(daily_start_time='14:21')
            planner._recalculate_day(d, req)
            self.assertEqual(d.attractions[0].planned_departure_time, '17:21')
            self.assertTrue(planner._try_shorten_for_opening_hours(d, req))
            self.assertEqual(d.attractions[0].visit_duration, 159)
            self.assertEqual(d.attractions[0].planned_departure_time, '17:00')
            self.assertEqual(d.attractions[0].opening_hours_status, 'open')
            self.assertEqual(d.attractions[0].original_visit_duration, 180)
            self.assertEqual(len(d.attractions), 1)
            # Repeated repairs cannot keep shrinking beyond the original-time bound.
            late = request(daily_start_time='15:00')
            planner._recalculate_day(d, late)
            self.assertFalse(planner._try_shorten_for_opening_hours(d, late))

    def test_late_entry_cannot_be_fixed_by_shortening_the_visit(self):
        with patch.object(settings, 'amap_api_key', ''):
            planner = MultiAgentTripPlanner(lightweight=True)
            d = day()
            d.attractions = [Attraction(name='故宫博物院', location=loc(), visit_duration=45)]
            req = request(daily_start_time='16:20')
            planner._recalculate_day(d, req)
            self.assertFalse(planner._try_shorten_for_opening_hours(d, req))


if __name__ == '__main__':
    unittest.main()
