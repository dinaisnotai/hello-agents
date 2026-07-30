import json
import unittest

from app.models.schemas import (
    Attraction,
    Budget,
    DayPlan,
    Location,
    TripPlan,
    TripRequest,
)
from app.services.planning_observability import (
    build_planning_trace,
    emit_planning_trace,
)


def _request():
    return TripRequest(
        city="Beijing",
        start_date="2026-10-10",
        end_date="2026-10-10",
        travel_days=1,
        transportation="public transit",
        accommodation="budget hotel",
        travelers=["elderly"],
        preferences=["museum"],
        budget_limit=800,
        hard_constraints=["return before 18:00"],
        soft_preferences=["avoid crowds"],
    )


def _attraction(name, category, longitude, score):
    return Attraction(
        name=name,
        category=category,
        categories=[category],
        tags=[category],
        location=Location(longitude=longitude, latitude=39.9),
        score=score,
        score_breakdown={
            "selection_base_score": score + 5,
            "preference_score": 24 if category == "museum" else 0,
        },
    )


class PlanningObservabilityTest(unittest.TestCase):
    def test_trace_explains_requirements_candidates_route_and_validation(self):
        selected = _attraction("Museum A", "museum", 116.4, 88)
        rejected = _attraction("Park B", "park", 116.8, 65)
        filtered = _attraction("Mountain C", "natural", 117.0, 60)
        day = DayPlan(
            date="2026-10-10",
            day_index=0,
            description="test",
            transportation="public transit",
            accommodation="budget hotel",
            attractions=[selected],
            daily_travel_minutes=45,
            daily_walking_distance_km=2.1,
            planned_start_time="09:00",
            planned_end_time="17:00",
        )
        plan = TripPlan(
            city="Beijing",
            start_date="2026-10-10",
            end_date="2026-10-10",
            days=[day],
            overall_suggestions="test",
            budget=Budget(total=600, remaining=200),
        )

        trace = build_planning_trace(
            _request(),
            plan,
            candidates=[selected, rejected, filtered],
            eligible_candidates=[selected, rejected],
            run_id="trace-test",
        )

        self.assertEqual(trace.user_requirement.age_group, "elderly")
        self.assertEqual(trace.user_requirement.budget, 800)
        self.assertEqual(trace.route_planning.selected_pois, ["Museum A"])
        self.assertEqual(trace.route_planning.travel_time, 45)
        self.assertEqual(
            trace.route_planning.category_distribution,
            {"museum": 1},
        )
        by_name = {item.name: item for item in trace.candidate_pois}
        self.assertIsNone(by_name["Museum A"].rejection_reason)
        self.assertEqual(
            by_name["Park B"].rejection_reason,
            "not_selected_by_route_planner",
        )
        self.assertEqual(
            by_name["Mountain C"].rejection_reason,
            "planning_constraint_filter",
        )
        self.assertTrue(trace.validation_result.constraint_pass)

    def test_emit_writes_one_parseable_json_record(self):
        plan = TripPlan(
            city="Beijing",
            start_date="2026-10-10",
            end_date="2026-10-10",
            days=[],
            overall_suggestions="test",
        )
        trace = build_planning_trace(_request(), plan, run_id="json-test")

        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            emit_planning_trace(trace)

        payload = json.loads(captured.output[0].split(":", 2)[-1])
        self.assertEqual(payload["event"], "travel_planning_run")
        self.assertEqual(payload["run_id"], "json-test")

    def test_final_trace_releases_stale_major_quota_and_uses_final_state(self):
        major = _attraction("Major", "historic", 116.4, 80)
        major.selection_role = "major_attraction"
        major.selection_reason = "major_attraction_quota"
        plan = TripPlan(
            city="Beijing", start_date="2026-10-10", end_date="2026-10-10",
            days=[], overall_suggestions="test",
        )

        trace = build_planning_trace(_request(), plan, candidates=[major])

        self.assertEqual(trace.portfolio_metrics.major_attraction_count, 0)
        candidate = trace.candidate_pois[0]
        self.assertEqual(candidate.selection_reason, "rejected_final_route_or_repair")
        self.assertFalse(candidate.final_selected)


if __name__ == "__main__":
    unittest.main()
