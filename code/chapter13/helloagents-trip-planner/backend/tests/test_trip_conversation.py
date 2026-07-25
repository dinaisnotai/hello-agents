import tempfile
import unittest
from pathlib import Path

from app.models.schemas import Budget, ReplanRequest, TripPlan, TripRequest
from app.repositories.trip_session_repository import TripSessionRepository
from app.services.trip_conversation_service import (
    TripConversationService,
    _merge_patches,
    apply_trip_request_patch,
    parse_trip_request_patch,
)
from app.models.conversation import TripRequestPatch


def _request() -> TripRequest:
    return TripRequest(
        city="北京",
        start_date="2026-08-01",
        end_date="2026-08-02",
        travel_days=2,
        transportation="公共交通",
        accommodation="经济型酒店",
        budget_limit=2500,
        pace="balanced",
        must_visit=["故宫"],
    )


def _plan(request: TripRequest) -> TripPlan:
    return TripPlan(
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        days=[],
        overall_suggestions="测试计划",
        budget=Budget(
            total=1000,
            budget_limit=request.budget_limit,
            remaining=(request.budget_limit or 0) - 1000,
        ),
    )


class _Planner:
    def __init__(self):
        self.plan_calls = 0
        self.replan_calls = 0

    def plan_trip(self, request: TripRequest) -> TripPlan:
        self.plan_calls += 1
        return _plan(request)

    def replan(self, request: ReplanRequest) -> TripPlan:
        self.replan_calls += 1
        plan = request.plan.model_copy(deep=True)
        plan.budget.budget_limit = request.request.budget_limit
        plan.budget.remaining = request.request.budget_limit - plan.budget.total
        return plan


class TripPatchTest(unittest.TestCase):
    def test_parses_budget_pace_and_must_visit_changes(self):
        patch = parse_trip_request_patch(
            "预算改成 2000，安排轻松一点，加入颐和园，把故宫去掉"
        )

        self.assertEqual(patch.budget_limit, 2000)
        self.assertEqual(patch.pace, "relaxed")
        self.assertEqual(patch.add_must_visit, ["颐和园"])
        self.assertEqual(patch.remove_must_visit, ["故宫"])

    def test_parses_symbolic_budget_and_english_add_syntax(self):
        patch = parse_trip_request_patch("钱<1000, add景山公园")

        self.assertEqual(patch.budget_limit, 999)
        self.assertEqual(patch.add_must_visit, ["景山公园"])

    def test_rule_patch_fills_fields_omitted_by_llm(self):
        merged = _merge_patches(
            TripRequestPatch(budget_limit=999),
            parse_trip_request_patch("钱<1000, add景山公园"),
        )

        self.assertEqual(merged.budget_limit, 999)
        self.assertEqual(merged.add_must_visit, ["景山公园"])

    def test_applies_patch_without_duplicate_places(self):
        patch = parse_trip_request_patch("加入故宫、颐和园，预算控制在1800")
        updated = apply_trip_request_patch(_request(), patch)

        self.assertEqual(updated.budget_limit, 1800)
        self.assertEqual(updated.must_visit, ["故宫", "颐和园"])


class TripSessionRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = TripSessionRepository(
            Path(self.temp_dir.name) / "sessions.db"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_persists_and_restores_current_plan_version(self):
        request = _request()
        session = self.repository.create(request, _plan(request))
        updated_request = request.model_copy(update={"budget_limit": 1800})
        self.repository.add_version(
            session.id,
            updated_request,
            _plan(updated_request),
            "预算上限调整为 1800 元",
        )

        restored = self.repository.get(session.id)

        self.assertEqual(restored.current_version, 2)
        self.assertEqual(restored.request.budget_limit, 1800)
        self.assertEqual(len(restored.versions), 2)
        self.assertEqual(self.repository.list()[0].id, session.id)

    def test_conversation_uses_light_replan_for_budget_only(self):
        planner = _Planner()
        service = TripConversationService(
            self.repository,
            planner,
            patch_interpreter=lambda request, content: parse_trip_request_patch(content),
        )
        session = self.repository.create(_request(), _plan(_request()))

        detail, patch, _ = service.send_message(session.id, "预算改为 1900 元")

        self.assertEqual(patch.budget_limit, 1900)
        self.assertEqual(planner.replan_calls, 1)
        self.assertEqual(planner.plan_calls, 0)
        self.assertEqual(detail.current_version, 2)
        self.assertEqual(detail.request.budget_limit, 1900)
        self.assertEqual(
            [item.role for item in detail.messages],
            ["assistant", "user", "assistant"],
        )

    def test_conversation_rediscovers_for_pace_or_must_visit(self):
        planner = _Planner()
        service = TripConversationService(
            self.repository,
            planner,
            patch_interpreter=lambda request, content: parse_trip_request_patch(content),
        )
        session = self.repository.create(_request(), _plan(_request()))

        detail, _, _ = service.send_message(
            session.id,
            "节奏轻松一点，加入颐和园",
        )

        self.assertEqual(planner.plan_calls, 1)
        self.assertEqual(planner.replan_calls, 0)
        self.assertEqual(detail.request.pace, "relaxed")
        self.assertIn("颐和园", detail.request.must_visit)


if __name__ == "__main__":
    unittest.main()
