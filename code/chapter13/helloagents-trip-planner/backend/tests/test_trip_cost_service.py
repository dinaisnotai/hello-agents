import unittest
from unittest.mock import patch

from app.agents.trip_planner_agent import BudgetEstimator, POICollector
from app.config import settings
from app.models.schemas import DayPlan, Hotel, POIInfo, Location, RouteSegment, TripRequest
from app.services.trip_cost_service import price_day, stay_nights


def request(**changes):
    data = dict(city="北京", start_date="2026-10-01", end_date="2026-10-03",
                travel_days=3, transportation="打车/网约车", accommodation="豪华酒店")
    data.update(changes)
    return TripRequest(**data)


def day(index=0, mode="打车/网约车"):
    return DayPlan(date=f"2026-10-0{index + 1}", day_index=index, description="",
                   transportation=mode, accommodation="酒店", hotel=Hotel(name="测试", estimated_cost=1000),
                   route_segments=[RouteSegment(day_index=index, origin="酒店", destination="景点",
                                                route_type="driving", distance_meters=10000)])


class TripCostTest(unittest.TestCase):
    def test_three_days_two_nights_and_totals_match(self):
        days = [day(i) for i in range(3)]
        budget = BudgetEstimator().estimate(days, request(room_count=2))
        self.assertEqual(stay_nights(request()), 2)
        self.assertEqual(budget.total_hotels, 4000)
        self.assertEqual([d.accommodation_nights for d in days], [1, 1, 0])
        self.assertEqual(sum(d.daily_cost for d in days), budget.total)
        repeated = BudgetEstimator().estimate(days, request(room_count=2))
        self.assertEqual(budget, repeated)

    def test_day_trip_has_no_hotel_charge(self):
        budget = BudgetEstimator().estimate([day()], request(end_date="2026-10-01", travel_days=1))
        self.assertEqual(budget.total_hotels, 0)

    def test_taxi_is_per_car_transit_per_person(self):
        with patch.multiple(settings, taxi_base_fare=15, taxi_included_km=3,
                            taxi_per_km=3, vehicle_capacity=4, transit_fare_per_leg=6):
            first, second = day(), day()
            self.assertEqual(price_day(first, request(party_size=1)), 36)
            self.assertEqual(price_day(second, request(party_size=5)), 72)
            second.route_segments[0].route_type = "transit"
            self.assertEqual(price_day(second, request(party_size=5)), 30)

    def test_rental_is_charged_once_and_keeps_line_items(self):
        with patch.multiple(settings, rental_per_day=300, rental_insurance_per_day=60,
                            parking_per_day=50, driving_cost_per_km=0.7, toll_per_km=0):
            days = [day(i, "租车自驾") for i in range(3)]
            budget = BudgetEstimator().estimate(days, request(transportation="租车自驾", rental_days=2))
            self.assertEqual(budget.total_transportation, 720 + 150 + 21)
            self.assertEqual(days[0].transport_fixed_costs["租车租金"], 600)
            self.assertNotIn("租车租金", days[1].transport_fixed_costs)

    def test_map_hotels_do_not_inherit_requested_luxury_or_fake_rating(self):
        class Map:
            def search_poi(self, *args, **kwargs):
                return [POIInfo(id="hotel", name="测试旅馆", type="住宿服务;旅馆招待所",
                                location=Location(longitude=116.4, latitude=39.9))]
        hotels = POICollector(Map()).collect_hotels(request())
        self.assertEqual(hotels[0].type, "住宿服务;旅馆招待所")
        self.assertEqual(hotels[0].price_source, "estimate")
        self.assertEqual(hotels[0].rating, "暂无评分")


if __name__ == "__main__":
    unittest.main()
