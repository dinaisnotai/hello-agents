import unittest

from app.models.schemas import TripRequest
from app.services.city_name_service import normalize_city_name


class CityNameServiceTest(unittest.TestCase):
    def test_normalizes_beijing_aliases_and_english_name(self):
        for value in ("北京", "北京市", "北平", "beijing", " Beijing City "):
            self.assertEqual(normalize_city_name(value), "北京")

    def test_keeps_unknown_city_instead_of_fuzzy_matching(self):
        self.assertEqual(normalize_city_name("北海"), "北海")

    def test_trip_request_normalizes_city_at_api_boundary(self):
        request = TripRequest(
            city="beijing",
            start_date="2026-08-01",
            end_date="2026-08-03",
            travel_days=3,
            transportation="公共交通",
            accommodation="经济型酒店",
        )
        self.assertEqual(request.city, "北京")


if __name__ == "__main__":
    unittest.main()
