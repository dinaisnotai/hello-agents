import tempfile
import unittest
from pathlib import Path

from app.models.agent_outputs import (
    AttractionSearchResult,
    HotelSearchResult,
    WeatherQueryResult,
)
from app.models.schemas import ConstraintReport, Hotel, TripPlan, TripRequest
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


if __name__ == "__main__":
    unittest.main()
