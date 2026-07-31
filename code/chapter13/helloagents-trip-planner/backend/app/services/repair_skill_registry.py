"""Static registry for the existing deterministic repair handlers."""

from __future__ import annotations

from collections import defaultdict

from ..models.quality import RepairStrategy
from ..models.repair_skills import RepairSkillDefinition


_ISSUES: dict[RepairStrategy, list[str]] = {
    RepairStrategy.ADD_UNUSED_CANDIDATE: ["empty_day"],
    RepairStrategy.ADD_NEARBY_COMPLEMENTARY_POI: ["underfilled_day", "experience_quality"],
    RepairStrategy.SWAP_WITH_INDOOR_CANDIDATE: ["weather_risk", "safety_risk"],
    RepairStrategy.ADD_WEATHER_BACKUP: ["weather_risk"],
    RepairStrategy.RECLUSTER_ROUTE: ["long_transport", "time_violation"],
    RepairStrategy.RESELECT_HOTEL: ["long_transport", "budget_violation"],
    RepairStrategy.REMOVE_DUPLICATE: ["duplicate_visit"],
    RepairStrategy.REPLACE_LOW_VALUE_CATEGORY: ["poor_diversity", "preference_alignment"],
    RepairStrategy.RUN_CONSTRAINT_REPAIR: ["constraint_failure", "time_violation", "impossible_schedule"],
    RepairStrategy.REDUCE_COST: ["budget_violation"],
    RepairStrategy.ADD_MUST_VISIT: ["missing_must_visit", "low_landmark_coverage"],
    RepairStrategy.REMOVE_CLOSED_ATTRACTION: ["attraction_closed"],
    RepairStrategy.RESOLVE_SAFETY_RISK: ["safety_risk"],
}


class RepairSkillRegistry:
    """Read-only mapping; applicability complements, never replaces, gates."""

    def __init__(self) -> None:
        self._by_strategy = {
            strategy: RepairSkillDefinition(
                skill_id=f"repair.{strategy.value.lower()}.v1",
                strategy=strategy,
                supported_issue_types=issues,
                applicable_scopes=["days[*]"],
                description=f"Deterministic handler for {strategy.value}",
                required_context_fields=["issue", "plan_version", "hard_constraints"],
            )
            for strategy, issues in _ISSUES.items()
        }

    def get(self, strategy: RepairStrategy) -> RepairSkillDefinition | None:
        return self._by_strategy.get(strategy)

    def candidates_for_issue(self, issue_type: str) -> list[RepairSkillDefinition]:
        return [item for item in self._by_strategy.values() if item.enabled and issue_type in item.supported_issue_types]

    def is_applicable(self, strategy: RepairStrategy, issue_type: str, scope: str | None = None) -> bool:
        skill = self.get(strategy)
        return bool(skill and skill.enabled and issue_type in skill.supported_issue_types and (scope is None or any(scope.startswith(item[:-3]) if item.endswith("[*]") else scope == item for item in skill.applicable_scopes)))

    def all(self) -> list[RepairSkillDefinition]:
        return list(self._by_strategy.values())


_registry: RepairSkillRegistry | None = None


def get_repair_skill_registry() -> RepairSkillRegistry:
    global _registry
    if _registry is None:
        _registry = RepairSkillRegistry()
    return _registry
