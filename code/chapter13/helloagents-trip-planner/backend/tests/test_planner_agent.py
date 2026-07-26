import json
import unittest

from app.agents.planner_agent import PlannerAgent
from app.models.agent_outputs import (
    AttractionSearchResult,
    HotelSearchResult,
    WeatherQueryResult,
)
from app.agents.trip_planner_agent import BudgetEstimator, ConstraintChecker, MultiAgentTripPlanner
from app.models.schemas import (
    Attraction,
    DayPlan,
    EvidenceSource,
    Hotel,
    Location,
    PlanningTraceItem,
    TripPlan,
    TripRequest,
    WeatherInfo,
)
from app.services.spatial_planner import SpatialItineraryPlanner


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


class _NoRouteEvaluator:
    def build_day_routes(self, day, city):
        return []


def _repair_planner():
    planner = MultiAgentTripPlanner.__new__(MultiAgentTripPlanner)
    planner.max_planning_iterations = 5
    planner.route_evaluator = _NoRouteEvaluator()
    planner.spatial_planner = SpatialItineraryPlanner()
    planner.budget_estimator = BudgetEstimator()
    planner.constraint_checker = ConstraintChecker()
    return planner


def _repair_plan(*, attractions=None):
    return TripPlan(
        city="北京",
        start_date="2026-08-01",
        end_date="2026-08-01",
        overall_suggestions="test",
        days=[
            DayPlan(
                date="2026-08-01",
                day_index=0,
                description="test",
                transportation="公共交通",
                accommodation="经济型酒店",
                hotel=Hotel(name="test hotel", estimated_cost=0),
                attractions=attractions or [],
                meals=[],
            )
        ],
    )


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
    def test_repair_loop_removes_low_priority_attraction_and_records_trace(self):
        attraction = Attraction(
            name="可选景点",
            location=Location(longitude=116.4, latitude=39.9),
            ticket_price=200,
            score=10,
        )
        request = TripRequest(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-01",
            travel_days=1,
            transportation="公共交通",
            accommodation="经济型酒店",
            budget_limit=50,
        )

        result = _repair_planner()._repair_until_stable(
            _repair_plan(attractions=[attraction]), request, [attraction]
        )

        self.assertTrue(result.constraint_report.passed)
        self.assertEqual(result.days[0].attractions, [])
        self.assertIsNone(result.failure_reason)
        self.assertIn(
            "remove_low_priority_attraction",
            [item.action for item in result.planning_trace],
        )

    def test_repair_loop_removes_optional_attraction_when_day_is_too_long(self):
        required = Attraction(
            name="必去景点",
            location=Location(longitude=116.4, latitude=39.9),
            visit_duration=400,
            score=100,
        )
        optional = Attraction(
            name="可选景点",
            location=Location(longitude=116.41, latitude=39.9),
            visit_duration=200,
            score=10,
        )
        request = TripRequest(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-01",
            travel_days=1,
            transportation="公共交通",
            accommodation="经济型酒店",
            must_visit=["必去景点"],
        )

        result = _repair_planner()._repair_until_stable(
            _repair_plan(attractions=[required, optional]), request, [required, optional]
        )

        self.assertTrue(result.constraint_report.passed)
        self.assertEqual([item.name for item in result.days[0].attractions], ["必去景点"])
        self.assertIn(
            "remove_optional_attraction_to_reduce_daily_duration",
            [item.action for item in result.planning_trace],
        )

    def test_repair_loop_returns_failure_reason_when_missing_must_visit_cannot_be_found(self):
        request = TripRequest(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-01",
            travel_days=1,
            transportation="公共交通",
            accommodation="经济型酒店",
            must_visit=["不存在的景点"],
        )

        result = _repair_planner()._repair_until_stable(_repair_plan(), request, [])

        self.assertFalse(result.constraint_report.passed)
        self.assertTrue(result.failure_reason)
        self.assertEqual(result.planning_trace[0].role, "ConstraintChecker")

    def test_dietary_constraint_requires_explicit_meal_markers(self):
        request = TripRequest(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-01",
            travel_days=1,
            transportation="公共交通",
            accommodation="经济型酒店",
            dietary_restrictions=["不吃海鲜"],
        )

        report = ConstraintChecker().check(_repair_plan(), request)

        dietary_item = next(item for item in report.items if item.name == "饮食限制")
        self.assertFalse(dietary_item.passed)
        self.assertFalse(report.passed)

    def test_trip_plan_serializes_planning_trace_and_failure_reason(self):
        plan = TripPlan(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-01",
            days=[],
            overall_suggestions="无法满足全部约束",
            planning_trace=[
                PlanningTraceItem(
                    iteration=0,
                    role="ConstraintChecker",
                    action="check_budget",
                    reason="预算超限",
                    score_before=0.7,
                    score_after=0.7,
                )
            ],
            failure_reason="预算仍超限",
        )

        payload = plan.model_dump(mode="json")

        self.assertEqual(payload["planning_trace"][0]["action"], "check_budget")
        self.assertEqual(payload["failure_reason"], "预算仍超限")

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
