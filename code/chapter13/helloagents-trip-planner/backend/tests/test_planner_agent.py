import json
import unittest

from app.agents.planner_agent import PlannerAgent
from app.models.agent_outputs import (
    AttractionSearchResult,
    HotelSearchResult,
    WeatherQueryResult,
)
from app.models.schemas import EvidenceSource, Hotel, TripPlan, TripRequest, WeatherInfo


class _StubPlanBuilder:
    def __init__(self):
        self.calls = []

    def build_plan_from_inputs(self, request, attractions, hotel, weather, evidence):
        self.calls.append(
            {
                "request": request,
                "attractions": attractions,
                "hotel": hotel,
                "weather": weather,
                "evidence": evidence,
            }
        )
        return TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=[],
            weather_info=weather,
            overall_suggestions="确定性规划说明",
            evidence_sources=evidence,
        )


class _StubAgent:
    def __init__(self, response):
        self.response = response

    def run(self, input_text):
        return self.response


def _request():
    return TripRequest(
        city="北京",
        start_date="2026-08-01",
        end_date="2026-08-01",
        travel_days=1,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["历史文化"],
    )


def _inputs():
    hotel = Hotel(name="测试酒店", type="经济型酒店", estimated_cost=350)
    return (
        AttractionSearchResult(),
        WeatherQueryResult(
            weather=[WeatherInfo(date="2026-08-01", day_weather="小雨")],
            risk_summary=["当天有雨，建议准备室内备选"],
        ),
        HotelSearchResult(candidates=[hotel], recommended_hotel=hotel),
        [
            EvidenceSource(
                title="北京出行提示",
                city="北京",
                source="data/travel_guides/beijing.md",
                snippet="热门景点建议提前预约。",
            )
        ],
    )


class PlannerAgentTest(unittest.TestCase):
    def test_without_llm_returns_complete_deterministic_plan(self):
        builder = _StubPlanBuilder()
        planner = PlannerAgent(None, builder)
        attractions, weather, hotels, evidence = _inputs()

        plan = planner.run(_request(), attractions, weather, hotels, evidence)

        self.assertEqual(len(builder.calls), 1)
        self.assertEqual(plan.overall_suggestions, "确定性规划说明")
        self.assertIn("当天有雨，建议准备室内备选", plan.risk_warnings)
        self.assertIn("跳过", planner.last_warning)

    def test_llm_review_can_explain_but_not_replace_plan_structure(self):
        builder = _StubPlanBuilder()
        planner = PlannerAgent(None, builder)
        review = {
            "summary": "第一天以历史文化为主，雨天注意调整室外停留。",
            "soft_warnings": [
                {
                    "message": "热门景点可能需要提前预约",
                    "basis": "rag",
                    "evidence_source": "data/travel_guides/beijing.md",
                },
                {
                    "message": "降雨可能影响室外体验",
                    "basis": "weather",
                    "evidence_source": None,
                },
            ],
            "evidence_sources": ["data/travel_guides/beijing.md"],
        }
        planner.agent = _StubAgent(json.dumps(review, ensure_ascii=False))
        attractions, weather, hotels, evidence = _inputs()

        plan = planner.run(_request(), attractions, weather, hotels, evidence)

        self.assertEqual(plan.days, [])
        self.assertEqual(plan.overall_suggestions, review["summary"])
        self.assertIn("降雨可能影响室外体验", plan.risk_warnings)
        self.assertTrue(
            any("data/travel_guides/beijing.md" in item for item in plan.risk_warnings)
        )

    def test_unverifiable_rag_warning_is_discarded(self):
        planner = PlannerAgent(None, _StubPlanBuilder())
        review = {
            "summary": "",
            "soft_warnings": [
                {
                    "message": "这是没有来源支持的提醒",
                    "basis": "rag",
                    "evidence_source": "invented.md",
                }
            ],
            "evidence_sources": ["invented.md"],
        }
        planner.agent = _StubAgent(json.dumps(review, ensure_ascii=False))
        attractions, weather, hotels, evidence = _inputs()

        plan = planner.run(_request(), attractions, weather, hotels, evidence)

        self.assertFalse(any("没有来源支持" in item for item in plan.risk_warnings))


if __name__ == "__main__":
    unittest.main()
