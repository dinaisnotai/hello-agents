import unittest
from threading import Barrier

from app.agents.multi_agent_orchestrator import MultiAgentOrchestrator
from app.models.agent_outputs import (
    AttractionSearchResult,
    HotelSearchResult,
    WeatherQueryResult,
)
from app.models.schemas import Hotel, TripPlan, TripRequest


class _Rag:
    def search(self, city, query, top_k):
        return []


class _PlanBuilder:
    def __init__(self):
        self.rag = _Rag()

    def build_rag_query(self, request):
        return request.city


class _Specialist:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def run(self, request):
        self.calls += 1
        return self.result


class _BarrierSpecialist(_Specialist):
    def __init__(self, result, barrier):
        super().__init__(result)
        self.barrier = barrier

    def run(self, request):
        self.barrier.wait(timeout=2)
        return super().run(request)


class _Planner:
    def __init__(self):
        self.last_warning = ""
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        request = kwargs["request"]
        return TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=[],
            overall_suggestions="四 Agent 编排成功",
        )


def _request():
    return TripRequest(
        city="北京",
        start_date="2026-08-01",
        end_date="2026-08-01",
        travel_days=1,
        transportation="公共交通",
        accommodation="经济型酒店",
    )


class MultiAgentOrchestratorTest(unittest.TestCase):
    def test_routes_three_specialist_outputs_into_planner_agent(self):
        orchestrator = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)
        orchestrator.initialization_warning = ""
        orchestrator.last_run_status = {}
        orchestrator.plan_builder = _PlanBuilder()
        orchestrator.attraction_agent = _Specialist(
            AttractionSearchResult(used_fallback=False)
        )
        orchestrator.weather_agent = _Specialist(
            WeatherQueryResult(used_fallback=True)
        )
        orchestrator.hotel_agent = _Specialist(
            HotelSearchResult(
                candidates=[Hotel(name="测试酒店")],
                used_fallback=False,
            )
        )
        orchestrator.planner_agent = _Planner()

        plan = orchestrator.plan_trip(_request())

        self.assertEqual(plan.overall_suggestions, "四 Agent 编排成功")
        self.assertEqual(len(orchestrator.planner_agent.calls), 1)
        self.assertEqual(
            orchestrator.last_run_status,
            {
                "AttractionSearchAgent": "llm",
                "WeatherQueryAgent": "fallback",
                "HotelAgent": "llm",
                "PlannerAgent": "llm",
            },
        )

    def test_runs_three_specialists_concurrently(self):
        barrier = Barrier(3)
        orchestrator = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)
        orchestrator.initialization_warning = ""
        orchestrator.last_run_status = {}
        orchestrator.plan_builder = _PlanBuilder()
        orchestrator.attraction_agent = _BarrierSpecialist(
            AttractionSearchResult(), barrier
        )
        orchestrator.weather_agent = _BarrierSpecialist(
            WeatherQueryResult(), barrier
        )
        orchestrator.hotel_agent = _BarrierSpecialist(
            HotelSearchResult(candidates=[Hotel(name="测试酒店")]), barrier
        )
        orchestrator.planner_agent = _Planner()

        plan = orchestrator.plan_trip(_request())

        self.assertEqual(plan.overall_suggestions, "四 Agent 编排成功")
        self.assertEqual(barrier.n_waiting, 0)

    def test_health_lists_the_four_real_agent_roles(self):
        orchestrator = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)
        orchestrator.initialization_warning = ""
        orchestrator.last_run_status = {}
        orchestrator.attraction_agent = type("AttractionSearchAgent", (), {})()
        orchestrator.weather_agent = type("WeatherQueryAgent", (), {})()
        orchestrator.hotel_agent = type("HotelAgent", (), {})()
        orchestrator.planner_agent = type("PlannerAgent", (), {})()

        snapshot = orchestrator.health_snapshot()

        self.assertEqual(
            snapshot["roles"],
            [
                "AttractionSearchAgent",
                "WeatherQueryAgent",
                "HotelAgent",
                "PlannerAgent",
            ],
        )


if __name__ == "__main__":
    unittest.main()
