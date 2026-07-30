import unittest
from pathlib import Path

from app.evaluation import evaluate_plan, load_evaluation_cases
from app.evaluation.schemas import EvaluationCase
from app.models.schemas import (
    Attraction,
    Budget,
    ConstraintReport,
    DayPlan,
    Location,
    TripPlan,
    TripRequest,
)


ROOT = Path(__file__).resolve().parents[1]


class EvaluationDatasetTest(unittest.TestCase):
    def test_dataset_contains_all_required_representative_scenarios(self):
        cases = load_evaluation_cases(
            ROOT / "evals" / "travel_scenarios.json"
        )
        names = {case.name for case in cases}

        self.assertGreaterEqual(len(cases), 7)
        self.assertTrue(
            {
                "no_preference_beijing_trip",
                "elderly_beijing_trip",
                "museum_lover_beijing_trip",
                "food_lover_shanghai_trip",
                "family_with_children_beijing_trip",
                "tight_budget_beijing_trip",
                "relaxed_pace_hangzhou_trip",
                "palace_parent_child_dedup",
                "multi_day_palace_no_repeat",
            }.issubset(names)
        )


class EvaluationCriteriaTest(unittest.TestCase):
    def _case(self):
        return EvaluationCase(
            name="elderly_beijing_trip",
            input_request=TripRequest(
                city="Beijing",
                start_date="2026-10-10",
                end_date="2026-10-10",
                travel_days=1,
                transportation="public transit",
                accommodation="comfortable hotel",
                travelers=["elderly"],
                preferences=["park"],
                pace="relaxed",
                max_daily_walk_km=5,
                daily_end_time="18:00",
            ),
            constraints={
                "walking_limit_km": 5,
                "avoid_hiking": True,
                "end_time": "18:00",
                "budget_limit": 800,
            },
            quality_expectations={
                "low_transport_time": True,
                "relaxed_pace": True,
                "category_diversity": True,
                "min_categories": 1,
            },
        )

    def _plan(self, *, walking=2.0, end_time="17:30"):
        attraction = Attraction(
            name="Accessible Park",
            category="park",
            categories=["park"],
            tags=["park"],
            intensity_level="low",
            location=Location(longitude=116.4, latitude=39.9),
        )
        day = DayPlan(
            date="2026-10-10",
            day_index=0,
            description="test",
            transportation="public transit",
            accommodation="comfortable hotel",
            attractions=[attraction],
            daily_travel_minutes=30,
            daily_walking_distance_km=walking,
            day_utilization_score=70,
            planned_start_time="09:00",
            planned_end_time=end_time,
        )
        return TripPlan(
            city="Beijing",
            start_date="2026-10-10",
            end_date="2026-10-10",
            days=[day],
            overall_suggestions="test",
            budget=Budget(total=700, remaining=100),
            constraint_report=ConstraintReport(passed=True, score=1),
        )

    def test_all_expected_criteria_can_pass(self):
        result = evaluate_plan(self._case(), self._plan())

        self.assertTrue(result.passed)
        self.assertTrue(result.constraint_passed)
        self.assertTrue(result.quality_passed)

    def test_independent_oracle_catches_walking_violation(self):
        result = evaluate_plan(self._case(), self._plan(walking=6.5))

        self.assertFalse(result.passed)
        self.assertFalse(result.constraint_passed)
        walking = next(
            item for item in result.criteria if item.name == "walking_limit"
        )
        self.assertFalse(walking.passed)
        self.assertEqual(walking.actual, 6.5)


if __name__ == "__main__":
    unittest.main()
