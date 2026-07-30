import unittest

from app.models.schemas import Attraction, Hotel, Location, TripRequest
from app.services.accommodation_selector import AccommodationSelector


def request(accommodation, budget):
    return TripRequest(
        city="北京",
        start_date="2026-10-10",
        end_date="2026-10-11",
        travel_days=2,
        transportation="public transit",
        accommodation=accommodation,
        budget_limit=budget,
    )


def hotel(name, hotel_type, cost, longitude=116.40):
    return Hotel(
        name=name,
        type=hotel_type,
        estimated_cost=cost,
        location=Location(longitude=longitude, latitude=39.91),
    )


class AccommodationSelectorTest(unittest.TestCase):
    def setUp(self):
        self.selector = AccommodationSelector()
        self.attractions = [
            Attraction(
                name="核心景点",
                score=90,
                location=Location(longitude=116.40, latitude=39.91),
            )
        ]
        self.candidates = [
            hotel("经济酒店", "经济型酒店", 250),
            hotel("舒适酒店", "舒适型酒店", 500),
            hotel("豪华酒店", "豪华型酒店", 1200),
            hotel("胡同民宿", "精品民宿", 450),
        ]

    def test_budget_hotel_case(self):
        selected = self.selector.select(
            request("budget hotel", 1400),
            self.candidates,
            self.attractions,
        )
        self.assertEqual(selected.name, "经济酒店")

    def test_comfortable_hotel_case(self):
        selected = self.selector.select(
            request("comfortable hotel", 3000),
            self.candidates,
            self.attractions,
        )
        self.assertEqual(selected.name, "舒适酒店")

    def test_luxury_hotel_case(self):
        selected = self.selector.select(
            request("luxury hotel", 8000),
            self.candidates,
            self.attractions,
        )
        self.assertEqual(selected.name, "豪华酒店")

    def test_homestay_case(self):
        selected = self.selector.select(
            request("民宿", 3000),
            self.candidates,
            self.attractions,
        )
        self.assertEqual(selected.name, "胡同民宿")

    def test_accessible_hotel_beats_remote_equal_candidate(self):
        candidates = [
            hotel("近酒店", "舒适型酒店", 500, longitude=116.40),
            hotel("远酒店", "舒适型酒店", 500, longitude=117.20),
        ]

        selected = self.selector.select(
            request("comfortable hotel", 3000),
            candidates,
            self.attractions,
        )

        self.assertEqual(selected.name, "近酒店")
        self.assertGreater(
            selected.score_breakdown["attraction_accessibility"],
            0,
        )
        self.assertLessEqual(
            selected.score_breakdown["average_attraction_travel_minutes"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
