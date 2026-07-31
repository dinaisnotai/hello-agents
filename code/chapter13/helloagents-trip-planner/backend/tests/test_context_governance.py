import unittest

from app.models.context_governance import ContextPolicy, ContextRole
from app.models.quality import ExperienceIssue, RepairStrategy
from app.models.schemas import Attraction, DayPlan, Location, TripPlan, TripRequest
from app.services.context_governance import RoleContextBuilder


def request():
    return TripRequest(city="Beijing", start_date="2026-08-01", end_date="2026-08-01", travel_days=1, transportation="transit", accommodation="budget", must_visit=["Forbidden City"], budget_limit=1000, max_daily_walk_km=6)


def plan():
    poi = Attraction(name="Forbidden City", poi_id="f", visit_key="f", location=Location(latitude=39.9, longitude=116.4))
    return TripPlan(city="Beijing", start_date="2026-08-01", end_date="2026-08-01", overall_suggestions="", days=[DayPlan(date="2026-08-01", day_index=0, description="", transportation="transit", accommodation="budget", attractions=[poi])])


class ContextGovernanceTest(unittest.TestCase):
    def test_roles_have_different_views_and_keep_hard_constraints(self):
        builder = RoleContextBuilder()
        contexts = [builder.build(role=role, request=request(), plan=plan()) for role in ContextRole]
        self.assertNotEqual(contexts[0].included_sections, contexts[1].included_sections)
        for context in contexts:
            self.assertTrue(context.hard_constraints_present)
            self.assertIn("hard_constraints", context.payload)
        self.assertNotIn("repair_history", contexts[2].payload)

    def test_budget_trims_optional_but_not_l0(self):
        builder = RoleContextBuilder()
        policy = ContextPolicy(role=ContextRole.EXPERIENCE_EVALUATOR, required_sections=["request", "hard_constraints", "plan_summary", "validation"], optional_sections=["candidates"], max_estimated_tokens=1, max_candidates=10)
        context = builder.build(role=ContextRole.EXPERIENCE_EVALUATOR, request=request(), plan=plan(), candidates=[plan().days[0].attractions[0]] * 10, policy=policy)
        self.assertIn("hard_constraints", context.payload)
        self.assertTrue(context.required_context_over_budget)
        self.assertIn("required_context_over_budget", context.truncation_reasons)

    def test_builder_does_not_mutate_plan(self):
        before = plan()
        builder = RoleContextBuilder()
        builder.build(role=ContextRole.EXPERIENCE_EVALUATOR, request=request(), plan=before)
        self.assertEqual(len(before.days[0].attractions), 1)
        self.assertEqual(before.plan_version.version, 1)

    def test_repair_strategist_gets_only_applicable_skill_summary(self):
        issue = ExperienceIssue(issue_type="underfilled_day", severity="warning", day=1, evidence="short day", repair_strategy=RepairStrategy.ADD_NEARBY_COMPLEMENTARY_POI)
        context = RoleContextBuilder().build(role=ContextRole.REPAIR_STRATEGIST, request=request(), plan=plan(), issue=issue)
        self.assertTrue(context.payload["skill_options"])
        self.assertTrue(all(item["strategy"] == "ADD_NEARBY_COMPLEMENTARY_POI" for item in context.payload["skill_options"]))
        self.assertNotIn("candidates", context.payload)
