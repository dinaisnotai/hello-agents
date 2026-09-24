import json
import unittest

from app.agents.attraction_search_agent import AttractionSearchAgent
from app.agents.hotel_agent import HotelAgent
from app.agents.weather_query_agent import WeatherQueryAgent
from app.models.schemas import EvidenceSource, Location, POIInfo, TripRequest, WeatherInfo
from app.tools.amap_tools import AttractionSearchTool, HotelSearchTool, WeatherQueryTool


class _FakeAmapService:
    def __init__(self):
        self.poi_calls = []
        self.weather_calls = []

    def search_poi(self, keywords, city, citylimit=True):
        self.poi_calls.append((keywords, city, citylimit))
        if "酒店" in keywords:
            return [
                POIInfo(
                    id="hotel-1",
                    name="测试酒店",
                    type="住宿服务;酒店",
                    address="测试路1号",
                    location=Location(longitude=116.40, latitude=39.90),
                    rating=4.6,
                )
            ]
        return [
            POIInfo(
                id="poi-1",
                name="故宫博物院",
                type="风景名胜;博物馆",
                address="景山前街4号",
                location=Location(longitude=116.397, latitude=39.916),
                rating=4.9,
            )
        ]

    def get_weather(self, city):
        self.weather_calls.append(city)
        return [
            WeatherInfo(
                date="2026-08-01",
                day_weather="雷阵雨",
                night_weather="多云",
                day_temp=36,
                night_temp=25,
            )
        ]


class _StubAgent:
    def __init__(self, payload):
        self.payload = payload
        self.kwargs = {}
        self.input_payload = {}

    def run(self, input_text, **kwargs):
        self.input_payload = json.loads(input_text)
        self.kwargs = kwargs
        return self.payload


def _request():
    return TripRequest(
        city="北京",
        start_date="2026-08-01",
        end_date="2026-08-02",
        travel_days=2,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["历史文化"],
        pace="balanced",
        must_visit=["故宫"],
        hotel_area="东城区",
    )


class AmapAgentToolsTest(unittest.TestCase):
    def test_attraction_tool_returns_typed_poi_data(self):
        amap = _FakeAmapService()
        response = AttractionSearchTool(amap).run(
            {"city": "北京", "keyword": "历史文化"}
        )

        self.assertEqual(response.data["pois"][0]["name"], "故宫博物院")
        self.assertEqual(amap.poi_calls, [("历史文化", "北京", True)])

    def test_hotel_tool_normalizes_cost_without_llm_guessing(self):
        amap = _FakeAmapService()
        response = HotelSearchTool(amap).run(
            {
                "city": "北京",
                "keyword": "东城区 经济型酒店 酒店",
                "accommodation": "经济型酒店",
            }
        )

        hotel = response.data["hotels"][0]
        self.assertEqual(hotel["estimated_cost"], 350)
        self.assertEqual(hotel["name"], "测试酒店")


class SpecialistAgentsTest(unittest.TestCase):
    @staticmethod
    def _evidence():
        return [EvidenceSource(
            title="核心区安排", city="北京", source="guide:test",
            snippet="历史景点适合按相邻片区安排。", score=0.9,
        )]

    def test_attraction_agent_parses_fenced_json(self):
        amap = _FakeAmapService()
        agent = AttractionSearchAgent(None, AttractionSearchTool(amap))
        payload = {
            "search_keywords": ["北京历史文化"],
            "attractions": [
                {
                    "name": "故宫博物院",
                    "address": "景山前街4号",
                    "location": {"longitude": 116.397, "latitude": 39.916},
                    "category": "博物馆",
                    "poi_id": "poi-1",
                }
            ],
            "warnings": [],
        }
        agent.agent = _StubAgent(f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```")

        result = agent.run(_request())

        self.assertFalse(result.used_fallback)
        self.assertEqual(result.attractions[0].name, "故宫博物院")
        self.assertEqual(agent.agent.kwargs["max_tool_iterations"], 1)

    def test_attraction_agent_uses_llm_queries_but_keeps_map_facts(self):
        amap = _FakeAmapService()
        agent = AttractionSearchAgent(None, AttractionSearchTool(amap))
        agent.agent = _StubAgent(json.dumps({
            "search_keywords": ["北京历史地标"],
            "attractions": [{
                "name": "模型编造景点", "poi_id": "fake",
                "location": {"longitude": 0, "latitude": 0},
            }],
        }, ensure_ascii=False))

        result = agent.run(_request(), evidence=self._evidence())

        self.assertNotIn("模型编造景点", [item.name for item in result.attractions])
        self.assertTrue(all(item.poi_id == "poi-1" for item in result.attractions))
        self.assertEqual(agent.agent.input_payload["guide_evidence"][0]["source"], "guide:test")

    def test_weather_agent_keeps_verified_forecast_when_llm_invents_weather(self):
        amap = _FakeAmapService()
        agent = WeatherQueryAgent(None, WeatherQueryTool(amap))
        agent.agent = _StubAgent(json.dumps({
            "weather": [{
                "date": "2026-08-01", "day_weather": "暴雪",
                "night_weather": "暴雪",
            }],
            "risk_summary": ["2026-08-01暴雪，景点关闭"],
        }, ensure_ascii=False))

        result = agent.run(_request(), evidence=self._evidence())

        self.assertEqual(result.weather[0].day_weather, "雷阵雨")
        self.assertEqual(result.weather[1].day_weather, "未知")
        self.assertFalse(any("暴雪" in item or "关闭" in item for item in result.risk_summary))

    def test_hotel_agent_uses_search_direction_but_keeps_map_hotel(self):
        amap = _FakeAmapService()
        agent = HotelAgent(None, HotelSearchTool(amap))
        agent.agent = _StubAgent(json.dumps({
            "search_keywords": ["东城区 地铁 酒店"],
            "candidates": [{"name": "模型编造酒店", "estimated_cost": 1}],
            "recommended_hotel": {"name": "模型编造酒店", "estimated_cost": 1},
        }, ensure_ascii=False))

        result = agent.run(_request(), evidence=self._evidence())

        self.assertEqual(result.recommended_hotel.name, "测试酒店")
        self.assertEqual(result.recommended_hotel.estimated_cost, 350)

    def test_attraction_agent_reuses_poi_collector_as_fallback(self):
        amap = _FakeAmapService()
        agent = AttractionSearchAgent(None, AttractionSearchTool(amap))

        result = agent.run(_request())

        self.assertTrue(result.used_fallback)
        self.assertTrue(result.attractions)
        self.assertEqual(result.attractions[0].name, "故宫博物院")

    def test_weather_agent_fills_missing_dates_in_fallback(self):
        amap = _FakeAmapService()
        agent = WeatherQueryAgent(None, WeatherQueryTool(amap))

        result = agent.run(_request())

        self.assertTrue(result.used_fallback)
        self.assertEqual([item.date for item in result.weather], ["2026-08-01", "2026-08-02"])
        self.assertEqual(result.weather[1].day_weather, "未知")
        self.assertEqual(len(result.risk_summary), 2)

    def test_hotel_agent_reuses_existing_hotel_fallback(self):
        amap = _FakeAmapService()
        agent = HotelAgent(None, HotelSearchTool(amap))

        result = agent.run(_request())

        self.assertTrue(result.used_fallback)
        self.assertEqual(result.recommended_hotel.name, "测试酒店")
        self.assertEqual(result.recommended_hotel.estimated_cost, 350)


if __name__ == "__main__":
    unittest.main()
