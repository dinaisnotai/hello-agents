import unittest
from unittest.mock import patch

import httpx

from app.models.schemas import Location
from app.services.amap_service import AmapService
from app.models.schemas import RouteInfo


class AmapRouteTest(unittest.TestCase):
    def setUp(self):
        self.service = AmapService()
        self.origin = Location(longitude=116.397, latitude=39.916)
        self.destination = Location(longitude=116.407, latitude=39.926)

    def test_falls_back_to_estimate_without_api_key(self):
        self.service.settings.amap_api_key = ""

        with patch.object(self.service, "_plan_route_by_locations") as plan_route:
            result = self.service.route_between_pois(
                origin_name="景点A",
                origin_address="北京市景点A",
                origin=self.origin,
                destination_name="景点B",
                destination_address="北京市景点B",
                destination=self.destination,
                city="北京",
                route_type="walking",
            )

        plan_route.assert_not_called()
        self.assertGreater(result.distance, 0)
        self.assertGreater(result.duration, 0)
        self.assertTrue(result.description.startswith("[estimated]"))




    def test_uses_amap_when_result_is_valid(self):
        self.service.settings.amap_api_key = "fake-key"

        amap_result = RouteInfo(
            distance=2300,
            duration=1800,
            route_type="walking",
            description="真实道路路线",
        )

        with patch.object(
            self.service,
            "_plan_route_by_locations",
            return_value=amap_result,
        ) as plan_route:
            result = self.service.route_between_pois(
                origin_name="景点A",
                origin_address="北京市景点A",
                origin=self.origin,
                destination_name="景点B",
                destination_address="北京市景点B",
                destination=self.destination,
                city="北京",
                route_type="walking",
            )

        plan_route.assert_called_once_with(
            self.origin,
            self.destination,
            route_type="walking",
            origin_city="北京",
            destination_city="北京",
        )
        self.assertEqual(result.distance, 2300)
        self.assertTrue(result.description.startswith("[amap]"))

    def test_falls_back_when_amap_result_is_invalid(self):
        self.service.settings.amap_api_key = "fake-key"

        invalid_result = RouteInfo(
            distance=0,
            duration=0,
            route_type="walking",
            description="高德服务调用失败",
        )

        with patch.object(
            self.service,
            "_plan_route_by_locations",
            return_value=invalid_result,
        ):
            result = self.service.route_between_pois(
                origin_name="景点A",
                origin_address="北京市景点A",
                origin=self.origin,
                destination_name="景点B",
                destination_address="北京市景点B",
                destination=self.destination,
                city="北京",
                route_type="walking",
            )

        self.assertGreater(result.distance, 0)
        self.assertTrue(result.description.startswith("[estimated]"))

    def test_poi_timeout_falls_back_to_local_data(self):
        self.service.settings.amap_api_key = "fake-key"

        with patch(
            "app.services.amap_service.httpx.get",
            side_effect=httpx.ReadTimeout("timed out"),
        ):
            result = self.service.search_poi("博物馆", "北京")

        self.assertGreater(len(result), 0)
        self.assertTrue(result[0].id.startswith("fallback-"))

    def test_amap_application_error_falls_back(self):
        self.service.settings.amap_api_key = "fake-key"

        with patch.object(
            self.service,
            "_request",
            side_effect=RuntimeError("Amap rejected the request"),
        ):
            result = self.service.get_weather("北京")

        self.assertEqual(result, [])

if __name__ == "__main__":
    unittest.main()
