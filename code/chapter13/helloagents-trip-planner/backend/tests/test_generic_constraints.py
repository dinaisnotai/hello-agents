import unittest

from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.constraints.extractor import ConstraintExtractor
from app.constraints.schema import ConstraintType
from app.constraints.validators import ConstraintValidationEngine
from app.models.schemas import (
    Attraction,
    DayPlan,
    Location,
    TripPlan,
    TripRequest,
)


def elderly_beijing_request():
    return TripRequest(
        city="北京",
        start_date="2026-08-01",
        end_date="2026-08-04",
        travel_days=4,
        transportation="公共交通",
        accommodation="经济型酒店",
        travelers=["senior"],
        free_text_input="老人同行，不能长时间步行，中午需要休息",
        daily_start_time="09:00",
        daily_end_time="18:00",
        max_daily_walk_km=5,
        first_visit=True,
    )


class ConstraintExtractorTest(unittest.TestCase):
    def test_elderly_preset_is_normalized_without_identity_rules_downstream(self):
        result = ConstraintExtractor().extract(elderly_beijing_request())
        by_type = {item.type: item for item in result.constraints}

        self.assertEqual(result.applied_presets, ["elderly"])
        self.assertEqual(
            by_type[ConstraintType.WALKING_DURATION].value, 20
        )
        self.assertEqual(
            by_type[ConstraintType.AVOID_ACTIVITY].value,
            "high_intensity",
        )
        self.assertEqual(by_type[ConstraintType.DAILY_LOAD].value, "low")
        self.assertEqual(
            by_type[ConstraintType.DAILY_END_TIME].value, "18:00"
        )
        self.assertEqual(
            by_type[ConstraintType.WALKING_DISTANCE].value, 5
        )
        rest = by_type[ConstraintType.TIME_WINDOW]
        self.assertEqual((rest.start, rest.end, rest.reason), ("12:00", "14:00", "rest"))

    def test_free_text_constraints_do_not_require_a_persona(self):
        request = TripRequest(
            city="北京",
            start_date="2026-08-01",
            end_date="2026-08-01",
            travel_days=1,
            transportation="公共交通",
            accommodation="经济型酒店",
            free_text_input="不能长时间步行，中午需要休息，每天18点前返回",
        )

        result = ConstraintExtractor().extract(request)
        by_type = {item.type: item for item in result.constraints}

        self.assertEqual(result.applied_presets, [])
        self.assertEqual(
            by_type[ConstraintType.WALKING_DURATION].value, 20
        )
        self.assertEqual(
            by_type[ConstraintType.DAILY_END_TIME].value, "18:00"
        )
        self.assertEqual(
            by_type[ConstraintType.TIME_WINDOW].reason, "rest"
        )

    def test_validator_returns_typed_time_and_activity_violations(self):
        request = elderly_beijing_request()
        constraint_set = ConstraintExtractor().extract(request)
        day = DayPlan(
            date="2026-08-01",
            day_index=0,
            description="invalid",
            transportation="公共交通",
            accommodation="经济型酒店",
            attractions=[
                Attraction(
                    name="测试爬山景点",
                    location=Location(longitude=116.4, latitude=39.9),
                    intensity_level="high",
                )
            ],
            planned_end_time="19:00",
        )
        plan = TripPlan(
            city="北京",
            start_date=request.start_date,
            end_date=request.end_date,
            days=[day],
            overall_suggestions="",
        )

        result = ConstraintValidationEngine().validate(plan, constraint_set)
        violation_types = {item.type for item in result.violations}

        self.assertFalse(result.valid)
        self.assertIn("TIME_LIMIT", violation_types)
        self.assertIn("ACTIVITY_LEVEL", violation_types)
        self.assertIn("REST_WINDOW", violation_types)
        self.assertTrue(
            all(item.repair_hint for item in result.violations)
        )


class ElderlyBeijingCase2Test(unittest.TestCase):
    def test_four_day_plan_satisfies_normalized_elderly_constraints(self):
        request = elderly_beijing_request()
        constraint_set = ConstraintExtractor().extract(request)
        planner = MultiAgentTripPlanner()
        planner.amap_service.settings.amap_api_key = ""
        attractions = planner.poi_collector.collect_attractions(request)
        hotel = planner.poi_collector.collect_hotel(request)

        plan = planner.build_plan_from_inputs(
            request,
            attractions,
            hotel,
            [],
            [],
            constraint_set=constraint_set,
        )

        self.assertEqual(len(plan.days), 4)
        self.assertTrue(all(day.attractions for day in plan.days))
        self.assertTrue(
            all(
                int(day.planned_end_time[:2]) * 60
                + int(day.planned_end_time[3:])
                <= 18 * 60
                for day in plan.days
            )
        )
        self.assertTrue(
            all(
                day.daily_walking_distance_km
                + sum(
                    item.estimated_internal_walking_km
                    for item in day.attractions
                )
                <= 5
                for day in plan.days
            )
        )
        self.assertTrue(
            all(
                any(
                    block.type == "rest"
                    and block.start_time >= "12:00"
                    and block.end_time <= "14:00"
                    for block in day.schedule_blocks
                )
                for day in plan.days
            )
        )
        self.assertFalse(
            any(
                item.intensity_level == "high"
                for day in plan.days
                for item in day.attractions
            )
        )
        self.assertFalse(
            any(
                segment.route_type == "driving"
                for day in plan.days
                for segment in day.route_segments
            )
        )
        self.assertTrue(plan.validation_result.valid)
        self.assertFalse(
            any(
                item.severity == "hard"
                for item in plan.validation_result.violations
            )
        )
        self.assertIn(
            ConstraintType.WALKING_DURATION,
            {item.type for item in plan.normalized_constraints},
        )


if __name__ == "__main__":
    unittest.main()
