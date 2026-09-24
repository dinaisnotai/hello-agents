import unittest
from unittest.mock import patch
from types import SimpleNamespace
import httpx

from app.config import settings
from app.models.schemas import Attraction, DayPlan, Hotel, Location, POIInfo, TripPlan, TripRequest
from app.agents.trip_planner_agent import POICollector, BudgetEstimator, RouteEvaluator, MultiAgentTripPlanner
from app.services.venue_policy import venue_kind
from app.services.poi_metadata_service import metadata_for_poi
from app.services.meal_planner import prepare_meals, schedule_stops
from app.services.hotel_quote_service import HotelQuoteService
from app.services.plan_mutation_sandbox import CommitGate
from app.models.quality import ExperienceEvaluation, ExperienceIssue, RepairStrategy, CommitDecisionCode


def request(**updates):
    data = dict(city="北京", start_date="2026-10-01", end_date="2026-10-03", travel_days=3,
                transportation="公共交通", accommodation="豪华酒店", preferences=["自然", "美食", "博物馆"])
    data.update(updates)
    return TripRequest(**data)


def day():
    return DayPlan(date="2026-10-01", day_index=0, description="", transportation="公共交通", accommodation="酒店",
                   attractions=[Attraction(name="天坛公园", location=Location(longitude=116.4, latitude=39.9))])


class ProductContractTest(unittest.TestCase):
    def test_reported_bad_pois_never_become_landmarks(self):
        cases = [("首都医科大学附属北京天坛医院", "医疗保健服务", "medical"),
                 ("大鸭梨烤鸭(王府井臻选店)", "餐饮服务", "restaurant"),
                 ("百年前门铜锅涮肉(王府井喜悦7店)", "餐饮服务", "restaurant"),
                 ("颐和园路", "道路名", "infrastructure")]
        for name, kind, expected in cases:
            self.assertEqual(venue_kind(name, kind), expected)
            metadata = metadata_for_poi(city="北京", name=name, poi_type=kind)
            self.assertLess(metadata.first_visit_priority, 7)
        self.assertEqual(metadata_for_poi(city="北京", name="天坛公园").first_visit_priority, 10)

    def test_meals_are_independent_nearby_stops_and_missing_data_is_explicit(self):
        restaurant = POIInfo(id="r", name="附近涮肉店", type="餐饮服务", location=Location(longitude=116.401, latitude=39.9))
        amap = SimpleNamespace(settings=SimpleNamespace(amap_api_key="fake"), search_poi=lambda *args: [restaurant])
        plan_day = day()
        prepare_meals(plan_day, request(), amap)
        self.assertEqual(plan_day.meals[0].poi_id, "r")
        self.assertEqual(plan_day.meals[1].source, "unconfirmed")
        self.assertEqual([stop.name for stop in schedule_stops(plan_day)][:2], ["天坛公园", "附近涮肉店"])
        self.assertEqual(len(plan_day.attractions), 1)

    def test_hotel_offer_is_all_rooms_all_nights_and_sandbox_is_labelled(self):
        bodies = []
        def provider(req):
            if req.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "h", "name": "五星测试酒店", "stars": 5, "address": "地址"}]})
            import json
            bodies.append(json.loads(req.content))
            return httpx.Response(200, json={"sandbox": True, "data": [{"hotelId": "h", "roomTypes": [{
                "offerRetailRate": {"amount": 2400, "currency": "CNY"}, "rates": [{"retailRate": {"taxesAndFees": [
                    {"included": False, "amount": 100, "currency": "CNY"}]}}]}]}]})
        with httpx.Client(base_url="https://provider.test/", transport=httpx.MockTransport(provider)) as client:
            with patch.multiple(settings, hotel_api_provider="liteapi", liteapi_api_key="test", liteapi_environment="sandbox"):
                hotels, warning = HotelQuoteService(client).search(request(party_size=4, room_count=2), None)
        self.assertFalse(warning)
        self.assertEqual(bodies[0]["occupancies"], [{"adults": 2}, {"adults": 2}])
        self.assertEqual(hotels[0].quoted_total, 2500)
        self.assertEqual(hotels[0].price_source, "sandbox_quote")
        days = [day() for _ in range(3)]
        for plan_day in days:
            plan_day.hotel = hotels[0].model_copy(deep=True)
        budget = BudgetEstimator().estimate(days, request(party_size=4, room_count=2))
        self.assertEqual(budget.total_hotels, 2500)

    def test_partial_hard_improvement_is_accepted_but_worsening_is_not(self):
        issue = ExperienceIssue(issue_type="constraint_failure", severity="high", evidence="walk", repair_strategy=RepairStrategy.RUN_CONSTRAINT_REPAIR)
        evaluation = ExperienceEvaluation(**{"pass": False}, issues=[issue])
        candidate = TripPlan(city="北京", start_date="2026-10-01", end_date="2026-10-01", days=[day()], overall_suggestions="")
        kwargs = dict(base_version=1, current_version=1, before_validation={"hard_keys": ["walk"], "excess": {"walk": 2}},
                      before_quality=evaluation, after_quality=evaluation, issue=issue, candidate=candidate, recomputed=True)
        improved = CommitGate.decide(**kwargs, after_validation={"hard_keys": ["walk"], "excess": {"walk": 1}})
        self.assertTrue(improved.accepted)
        worse = CommitGate.decide(**kwargs, after_validation={"hard_keys": ["walk"], "excess": {"walk": 3}})
        self.assertEqual(worse.code, CommitDecisionCode.NEW_HARD_VIOLATION)


if __name__ == "__main__":
    unittest.main()
