import tempfile
import unittest
from pathlib import Path

from app.models.agent_outputs import (
    AttractionSearchResult,
    HotelSearchResult,
    WeatherQueryResult,
)
from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.schemas import (
    Attraction,
    ConstraintReport,
    Hotel,
    Location,
    TripPlan,
    TripRequest,
)
from app.workflows.langgraph_trip_workflow import LangGraphTripWorkflow


class _Rag:
    def search(self, city, query, top_k):
        return []


class _PlanBuilder:
    def __init__(self, *, constraints_pass=True):
        self.rag = _Rag()
        self.constraints_pass = constraints_pass
        self.repairs = 0

    def build_rag_query(self, request):
        return request.city

    def build_plan_from_inputs(self, request, attractions, hotel, weather, evidence):
        return TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=[],
            overall_suggestions="deterministic",
            constraint_report=ConstraintReport(passed=self.constraints_pass, score=1 if self.constraints_pass else 0),
        )

    def _apply_next_repair(self, plan, request, attractions):
        self.repairs += 1
        return None

    def _recalculate(self, plan, request):
        return None


class _Specialist:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def run(self, request):
        if self.error:
            raise self.error
        return self.result


class _PlannerAgent:
    def review_plan(self, request, plan, weather, evidence):
        plan.overall_suggestions = "reviewed"
        return plan


def _request(**changes):
    base = {
        "city": "Beijing",
        "start_date": "2026-08-01",
        "end_date": "2026-08-01",
        "travel_days": 1,
        "transportation": "transit",
        "accommodation": "budget hotel",
        "pace": "relaxed",
    }
    base.update(changes)
    return TripRequest(**base)


def _orchestrator(*, weather_error=None, constraints_pass=True):
    orchestrator = type("Orchestrator", (), {})()
    orchestrator.plan_builder = _PlanBuilder(constraints_pass=constraints_pass)
    orchestrator.attraction_agent = _Specialist(AttractionSearchResult())
    orchestrator.weather_agent = _Specialist(WeatherQueryResult(), weather_error)
    orchestrator.hotel_agent = _Specialist(
        HotelSearchResult(candidates=[Hotel(name="Test hotel")])
    )
    orchestrator.planner_agent = _PlannerAgent()
    return orchestrator


class LangGraphTripWorkflowTest(unittest.TestCase):
    def _workflow(self, orchestrator):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workflow = LangGraphTripWorkflow(
            orchestrator,
            checkpoint_db_path=Path(tempdir.name) / "checkpoints.db",
        )
        self.addCleanup(workflow.close)
        return workflow

    def test_form_intent_wins_and_checkpoint_is_saved(self):
        workflow = self._workflow(_orchestrator())
        request = _request(
            travelers=["senior"],
            daily_start_time="08:30",
            daily_end_time="19:30",
            hard_constraints=["no stairs"],
            free_text_input="start at noon and visit every museum",
        )

        plan, summary = workflow.run(request, thread_id="intent-priority")
        state = workflow.graph.get_state({"configurable": {"thread_id": "intent-priority"}})

        self.assertEqual(plan.overall_suggestions, "reviewed")
        self.assertEqual(summary.run_id, "intent-priority")
        self.assertEqual(state.values["intent"]["daily_start_time"], "08:30")
        self.assertEqual(state.values["intent"]["travelers"], ["senior"])
        self.assertEqual(state.values["intent"]["hard_constraints"], ["no stairs"])

        resumed_plan, resumed_summary = workflow.resume("intent-priority")
        self.assertEqual(resumed_plan.city, request.city)
        self.assertTrue(resumed_summary.resumed)

    def test_specialist_failure_returns_degraded_plan(self):
        workflow = self._workflow(_orchestrator(weather_error=RuntimeError("weather unavailable")))

        plan, summary = workflow.run(_request())

        self.assertEqual(plan.overall_suggestions, "reviewed")
        self.assertIn("weather", summary.degraded_services)
        self.assertTrue(any("weather unavailable" in error for error in summary.errors))

    def test_repairs_are_bounded_by_workflow_limit(self):
        orchestrator = _orchestrator(constraints_pass=False)
        workflow = self._workflow(orchestrator)

        plan, summary = workflow.run(_request())

        self.assertFalse(plan.constraint_report.passed)
        self.assertEqual(orchestrator.plan_builder.repairs, workflow.max_repair_attempts)
        self.assertEqual(summary.workflow_mode, "langgraph")

    def test_thin_specialist_result_is_expanded_before_multiday_planning(self):
        plan_builder = MultiAgentTripPlanner()
        plan_builder.amap_service.settings.amap_api_key = ""
        plan_builder.rag = _Rag()
        orchestrator = type("Orchestrator", (), {})()
        orchestrator.plan_builder = plan_builder
        orchestrator.attraction_agent = _Specialist(
            AttractionSearchResult(
                attractions=[
                    Attraction(
                        name="故宫博物院",
                        location=Location(
                            longitude=116.397,
                            latitude=39.916,
                        ),
                    )
                ]
            )
        )
        orchestrator.weather_agent = _Specialist(WeatherQueryResult())
        orchestrator.hotel_agent = _Specialist(
            HotelSearchResult(
                candidates=[
                    Hotel(
                        name="测试酒店",
                        location=Location(
                            longitude=116.397,
                            latitude=39.916,
                        ),
                    )
                ]
            )
        )
        orchestrator.planner_agent = _PlannerAgent()
        workflow = self._workflow(orchestrator)
        request = TripRequest(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-04",
            travel_days=4,
            transportation="公共交通",
            accommodation="经济型酒店",
            travelers=["senior"],
            free_text_input="老人同行，中午需要休息",
            daily_start_time="09:00",
            daily_end_time="18:00",
            max_daily_walk_km=5,
            must_visit=["故宫"],
            first_visit=True,
        )

        plan, _ = workflow.run(request, thread_id="thin-candidates")
        state = workflow.graph.get_state(
            {"configurable": {"thread_id": "thin-candidates"}}
        )

        self.assertEqual(len(plan.days), 4)
        self.assertTrue(all(day.attractions for day in plan.days))
        self.assertTrue(plan.validation_result.valid)
        self.assertGreaterEqual(len(state.values["planning_candidates"]), 8)
        self.assertFalse(
            any(
                "第2、3、4天空白" in warning
                for warning in plan.risk_warnings
            )
        )


if __name__ == "__main__":
    unittest.main()
