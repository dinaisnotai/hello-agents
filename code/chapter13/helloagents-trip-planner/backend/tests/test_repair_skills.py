import tempfile
import unittest
from pathlib import Path

from app.models.quality import CommitDecisionCode, RepairStrategy
from app.models.repair_skills import RepairSkillExecution
from app.services.repair_skill_metrics import RepairSkillExecutionStore, RepairSkillMetricsAggregator
from app.services.repair_skill_registry import get_repair_skill_registry


class RepairSkillTest(unittest.TestCase):
    def test_every_strategy_has_stable_enabled_definition(self):
        registry = get_repair_skill_registry()
        for strategy in RepairStrategy:
            skill = registry.get(strategy)
            self.assertIsNotNone(skill)
            self.assertTrue(skill.enabled)
            self.assertGreaterEqual(skill.version, 1)
            self.assertTrue(skill.skill_id.startswith("repair."))

    def test_registry_rejects_unsupported_issue(self):
        registry = get_repair_skill_registry()
        self.assertFalse(registry.is_applicable(RepairStrategy.ADD_WEATHER_BACKUP, "budget_violation"))

    def test_metrics_do_not_treat_missing_quality_delta_as_zero(self):
        items = [
            RepairSkillExecution(skill_id="repair.a.v1", skill_version=1, base_plan_version=1, issue_type="underfilled_day", issue_severity="warning", issue_fingerprint="a", mutation_succeeded=True, committed=True, quality_delta=0.4),
            RepairSkillExecution(skill_id="repair.a.v1", skill_version=1, base_plan_version=1, issue_type="underfilled_day", issue_severity="warning", issue_fingerprint="b", mutation_succeeded=False, committed=False, rollback_reason="MUTATION_FAILED"),
        ]
        result = RepairSkillMetricsAggregator().aggregate(items)["repair.a.v1"]
        self.assertEqual(result["commit_rate"], 0.5)
        self.assertEqual(result["average_quality_delta"], 0.4)
        self.assertEqual(result["rollback_reasons"]["MUTATION_FAILED"], 1)

    def test_jsonl_store_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RepairSkillExecutionStore(Path(directory) / "events.jsonl")
            store.append(RepairSkillExecution(skill_id="repair.a.v1", skill_version=1, base_plan_version=1, issue_type="underfilled_day", issue_severity="warning", issue_fingerprint="a", commit_decision=CommitDecisionCode.COMMITTED, committed=True))
            self.assertEqual(len(store.load()), 1)

