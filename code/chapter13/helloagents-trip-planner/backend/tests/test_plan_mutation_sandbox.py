"""Regression tests for isolated and versioned repair mutations."""

import unittest
import tempfile
from pathlib import Path

from app.models.quality import (
    ExperienceEvaluation,
    ExperienceIssue,
    PlanVersionMetadata,
    RepairAttemptStatus,
    RepairStrategy,
)
from app.models.schemas import Attraction, DayPlan, Location, TripPlan, TripRequest
from app.services.plan_mutation_sandbox import (
    CommitDecisionCode,
    PlanMutationSandbox,
)
from app.services.repair_skill_metrics import RepairSkillExecutionStore


def request():
    return TripRequest(
        city="Beijing", start_date="2026-08-01", end_date="2026-08-01",
        travel_days=1, transportation="transit", accommodation="budget",
    )


def plan(version=1):
    return TripPlan(
        city="Beijing", start_date="2026-08-01", end_date="2026-08-01",
        overall_suggestions="base",
        days=[DayPlan(
            date="2026-08-01", day_index=0, description="", transportation="transit",
            accommodation="budget", attractions=[poi("A", "a")],
        )],
        plan_version=PlanVersionMetadata(version=version),
    )


def poi(name, poi_id):
    return Attraction(
        name=name, poi_id=poi_id, visit_key=poi_id,
        location=Location(latitude=39.9, longitude=116.4),
    )


class PlanMutationSandboxTest(unittest.TestCase):
    def setUp(self):
        self.issue = ExperienceIssue(
            issue_type="underfilled_day", severity="warning", day=1,
            evidence="needs one more POI",
            repair_strategy=RepairStrategy.ADD_NEARBY_COMPLEMENTARY_POI,
        )

    def sandbox(self, store=None):
        def recalculate(candidate, _request):
            candidate.days[0].daily_walking_distance_km = len(candidate.days[0].attractions)
            candidate.days[0].daily_travel_minutes = 10 * len(candidate.days[0].attractions)

        def hard_snapshot(candidate):
            invalid = candidate.overall_suggestions == "invalid"
            return {"hard_pass": not invalid, "hard_keys": ["bad"] if invalid else []}

        def evaluate(candidate):
            unresolved = len(candidate.days[0].attractions) < 2
            return ExperienceEvaluation(
                **{"pass": not unresolved}, overall_score=8 if not unresolved else 6,
                issues=[self.issue] if unresolved else [],
            )
        return PlanMutationSandbox(
            recalculate=recalculate, evaluate=evaluate, hard_snapshot=hard_snapshot,
            execution_store=store,
        )

    def test_deep_copy_and_successful_commit_creates_v2(self):
        base = plan()
        result = self.sandbox().execute(
            base_plan=base, current_plan=base, request=request(), issue=self.issue,
            strategy=self.issue.repair_strategy,
            mutate=lambda candidate: candidate.days[0].attractions.append(
                poi("B", "b")
            ) or True,
        )
        self.assertTrue(result.decision.accepted)
        self.assertEqual(result.plan.plan_version.version, 2)
        self.assertEqual(result.plan.plan_version.parent_version, 1)
        self.assertEqual(result.plan.plan_version.mutation_action, self.issue.repair_strategy.value)
        self.assertEqual(result.attempt.status, RepairAttemptStatus.COMMITTED)
        self.assertEqual(result.attempt.committed_plan_version, 2)
        self.assertEqual(len(base.days[0].attractions), 1)
        self.assertIsNot(base.days[0], result.plan.days[0])
        self.assertIsNot(base.days[0].attractions, result.plan.days[0].attractions)

    def test_new_hard_violation_rolls_back_without_version_change(self):
        base = plan()
        result = self.sandbox().execute(
            base_plan=base, current_plan=base, request=request(), issue=self.issue,
            strategy=self.issue.repair_strategy,
            mutate=lambda candidate: (
                candidate.days[0].attractions.append(poi("B", "b")),
                setattr(candidate, "overall_suggestions", "invalid"),
                True,
            )[-1],
        )
        self.assertEqual(result.decision.code, CommitDecisionCode.NEW_HARD_VIOLATION)
        self.assertEqual(result.plan.plan_version.version, 1)
        self.assertEqual(result.attempt.status, RepairAttemptStatus.ROLLED_BACK)
        self.assertEqual(len(base.days[0].attractions), 1)

    def test_stale_base_and_handler_exception_preserve_current_plan(self):
        base, current = plan(1), plan(2)
        stale = self.sandbox().execute(
            base_plan=base, current_plan=current, request=request(), issue=self.issue,
            strategy=self.issue.repair_strategy,
            mutate=lambda candidate: candidate.days[0].attractions.append(
                poi("B", "b")
            ) or True,
        )
        self.assertEqual(stale.decision.code, CommitDecisionCode.STALE_BASE_PLAN_VERSION)
        self.assertEqual(stale.plan.plan_version.version, 2)
        failed = self.sandbox().execute(
            base_plan=current, current_plan=current, request=request(), issue=self.issue,
            strategy=self.issue.repair_strategy,
            mutate=lambda _candidate: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        self.assertEqual(failed.decision.code, CommitDecisionCode.MUTATION_FAILED)
        self.assertEqual(failed.plan.plan_version.version, 2)
        self.assertEqual(failed.attempt.status, RepairAttemptStatus.FAILED)

    def test_execution_event_distinguishes_mutation_and_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RepairSkillExecutionStore(Path(directory) / "events.jsonl")
            base = plan()
            result = self.sandbox(store).execute(
                base_plan=base, current_plan=base, request=request(), issue=self.issue,
                strategy=self.issue.repair_strategy,
                mutate=lambda candidate: candidate.days[0].attractions.append(poi("B", "b")) or True,
            )
            event = store.load()[0]
            self.assertTrue(event.selected)
            self.assertTrue(event.mutation_succeeded)
            self.assertTrue(event.committed)
            self.assertEqual(event.attempt_id, result.attempt.attempt_id)
