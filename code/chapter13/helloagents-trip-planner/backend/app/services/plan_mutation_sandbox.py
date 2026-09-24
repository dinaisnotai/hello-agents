"""Isolated, versioned execution for itinerary repair candidates.

The sandbox deliberately owns the only transition from a repair candidate to a
committed TripPlan.  Repair handlers may mutate their supplied candidate, but
can never mutate the formal plan passed to :meth:`execute`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Sequence
from uuid import uuid4

from pydantic import ValidationError

from ..models.quality import (
    CommitDecisionCode,
    ExperienceEvaluation,
    ExperienceIssue,
    PlanDiff,
    PlanVersionMetadata,
    RepairAttempt,
    RepairAttemptStatus,
    RepairStrategy,
)
from ..models.schemas import Attraction, Hotel, TripPlan, TripRequest
from ..models.repair_skills import RepairSkillExecution
from .repair_skill_metrics import RepairSkillExecutionStore
from .repair_skill_registry import get_repair_skill_registry


@dataclass
class CommitDecision:
    code: CommitDecisionCode
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.code == CommitDecisionCode.COMMITTED


@dataclass
class SandboxExecutionResult:
    plan: TripPlan
    attempt: RepairAttempt
    decision: CommitDecision
    candidate: TripPlan | None = None
    before_evaluation: ExperienceEvaluation | None = None
    after_evaluation: ExperienceEvaluation | None = None


class CommitGate:
    """Pure candidate acceptance rules; validators remain authoritative."""

    @staticmethod
    def decide(
        *,
        base_version: int,
        current_version: int,
        before_validation: dict[str, Any],
        after_validation: dict[str, Any],
        before_quality: ExperienceEvaluation,
        after_quality: ExperienceEvaluation,
        issue: ExperienceIssue,
        candidate: TripPlan,
        recomputed: bool,
    ) -> CommitDecision:
        if base_version != current_version:
            return CommitDecision(CommitDecisionCode.STALE_BASE_PLAN_VERSION)
        if not recomputed:
            return CommitDecision(CommitDecisionCode.RECOMPUTE_FAILED)
        if not candidate.days:
            return CommitDecision(CommitDecisionCode.INVALID_CANDIDATE, "no days")
        before_hard = set(before_validation.get("hard_keys", []))
        after_hard = set(after_validation.get("hard_keys", []))
        if after_hard - before_hard:
            return CommitDecision(CommitDecisionCode.NEW_HARD_VIOLATION)
        if len(after_hard) > len(before_hard):
            return CommitDecision(CommitDecisionCode.NEW_HARD_VIOLATION)
        if any(not day.attractions for day in candidate.days):
            return CommitDecision(CommitDecisionCode.INVALID_CANDIDATE, "empty day")
        visit_keys = [
            poi.visit_key or poi.poi_id or poi.name
            for day in candidate.days for poi in day.attractions
        ]
        if len(visit_keys) != len(set(visit_keys)):
            return CommitDecision(CommitDecisionCode.INVALID_CANDIDATE, "duplicate visit")
        remaining = {item.fingerprint for item in after_quality.issues}
        hard_issue_improved = issue.is_blocking and len(after_hard) < len(before_hard)
        before_amounts = before_validation.get("excess", {})
        after_amounts = after_validation.get("excess", {})
        if before_amounts:
            if any(amount > before_amounts.get(key, 0) + 1e-6 for key, amount in after_amounts.items()):
                return CommitDecision(CommitDecisionCode.NEW_HARD_VIOLATION, "existing violation worsened")
            hard_issue_improved = hard_issue_improved or (issue.is_blocking and any(
                after_amounts.get(key, 0) < amount - 1e-6 for key, amount in before_amounts.items()))
        weather_backup_added = (
            issue.repair_strategy in {
                RepairStrategy.ADD_WEATHER_BACKUP,
                RepairStrategy.SWAP_WITH_INDOOR_CANDIDATE,
            }
            and issue.day is not None
            and issue.day <= len(candidate.days)
            and bool(candidate.days[issue.day - 1].weather_warning)
        )
        if issue.fingerprint in remaining and not hard_issue_improved and not weather_backup_added:
            return CommitDecision(CommitDecisionCode.TARGET_ISSUE_NOT_IMPROVED)
        # Repairing a hard issue may trade a small amount of soft score for a
        # valid plan.  Soft repairs must not lower the evaluator score.
        delta = after_quality.overall_score - before_quality.overall_score
        if not issue.is_blocking and delta < 0:
            return CommitDecision(CommitDecisionCode.QUALITY_REGRESSION)
        return CommitDecision(CommitDecisionCode.COMMITTED)


class PlanMutationSandbox:
    """Apply one repair against a deep copy and atomically produce vN+1."""

    def __init__(
        self,
        *,
        recalculate: Callable[[TripPlan, TripRequest], None],
        evaluate: Callable[[TripPlan], ExperienceEvaluation],
        hard_snapshot: Callable[[TripPlan], dict[str, Any]],
        execution_store: RepairSkillExecutionStore | None = None,
        pipeline_mode: str = "deterministic",
        evaluation_mode: str = "offline",
        run_id: str = "",
    ) -> None:
        self.recalculate = recalculate
        self.evaluate = evaluate
        self.hard_snapshot = hard_snapshot
        self.execution_store = execution_store or RepairSkillExecutionStore(
            Path(os.getenv("REPAIR_SKILL_ARTIFACT_PATH", "data/repair_skill_executions.jsonl"))
        )
        self.pipeline_mode = pipeline_mode
        self.evaluation_mode = evaluation_mode
        self.run_id = run_id

    def execute(
        self,
        *,
        base_plan: TripPlan,
        current_plan: TripPlan,
        request: TripRequest,
        issue: ExperienceIssue,
        strategy: RepairStrategy,
        mutate: Callable[[TripPlan], Any],
        before_evaluation: ExperienceEvaluation | None = None,
        after_recalculate: Callable[[TripPlan], None] | None = None,
        additional_gate: Callable[
            [TripPlan, TripPlan, ExperienceEvaluation, ExperienceEvaluation],
            CommitDecision | None,
        ] | None = None,
    ) -> SandboxExecutionResult:
        self._ensure_version(base_plan)
        self._ensure_version(current_plan)
        attempt = RepairAttempt(
            attempt_id=uuid4().hex,
            base_plan_version=base_plan.plan_version.version,
            issue_fingerprint=issue.fingerprint,
            issue_type=issue.issue_type,
            repair_strategy=strategy,
        )
        before_validation = self.hard_snapshot(base_plan)
        before_quality = before_evaluation or self.evaluate(base_plan)
        attempt.validation_before = before_validation
        attempt.quality_before = self._quality_snapshot(before_quality)
        started = perf_counter()
        decision: CommitDecision | None = None
        candidate: TripPlan | None = None
        try:
            # model_copy(deep=True) preserves Pydantic types and ensures nested
            # days, attractions and route segments are independent objects.
            candidate = base_plan.model_copy(deep=True)
            result = mutate(candidate)
            if isinstance(result, TripPlan):
                candidate = result
                result = True
            if result is False or result is None:
                attempt.status = RepairAttemptStatus.ROLLED_BACK
                attempt.rollback_reason = CommitDecisionCode.MUTATION_FAILED.value
                decision = CommitDecision(CommitDecisionCode.MUTATION_FAILED)
                return SandboxExecutionResult(
                    current_plan, attempt,
                    decision, candidate,
                )
            attempt.status = RepairAttemptStatus.APPLIED
            self.recalculate(candidate, request)
            if after_recalculate is not None:
                after_recalculate(candidate)
            after_validation = self.hard_snapshot(candidate)
            after_quality = self.evaluate(candidate)
            attempt.status = RepairAttemptStatus.VALIDATED
            attempt.validation_after = after_validation
            attempt.quality_after = self._quality_snapshot(after_quality)
            attempt.quality_delta = round(
                after_quality.overall_score - before_quality.overall_score, 4
            )
            attempt.plan_diff = self.diff(base_plan, candidate)
            extra_decision = (
                additional_gate(base_plan, candidate, before_quality, after_quality)
                if additional_gate is not None
                else None
            )
            decision = extra_decision or CommitGate.decide(
                base_version=attempt.base_plan_version,
                current_version=current_plan.plan_version.version,
                before_validation=before_validation,
                after_validation=after_validation,
                before_quality=before_quality,
                after_quality=after_quality,
                issue=issue,
                candidate=candidate,
                recomputed=True,
            )
            if not decision.accepted:
                attempt.status = RepairAttemptStatus.ROLLED_BACK
                attempt.rollback_reason = decision.code.value
                return SandboxExecutionResult(current_plan, attempt, decision, candidate, before_quality, after_quality)
            candidate.plan_version = PlanVersionMetadata(
                version=attempt.base_plan_version + 1,
                parent_version=attempt.base_plan_version,
                mutation_source="repair_controller",
                mutation_action=strategy.value,
                mutation_reason=issue.issue_type,
                attempt_id=attempt.attempt_id,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            attempt.status = RepairAttemptStatus.COMMITTED
            attempt.committed_plan_version = candidate.plan_version.version
            return SandboxExecutionResult(candidate, attempt, decision, candidate, before_quality, after_quality)
        except (ValidationError, ValueError, TypeError) as exc:
            attempt.status = RepairAttemptStatus.FAILED
            attempt.error = f"{type(exc).__name__}: {exc}"
            attempt.rollback_reason = CommitDecisionCode.INVALID_CANDIDATE.value
            decision = CommitDecision(CommitDecisionCode.INVALID_CANDIDATE, attempt.error)
            return SandboxExecutionResult(current_plan, attempt, decision, candidate)
        except Exception as exc:  # handlers are untrusted from a state-safety view
            attempt.status = RepairAttemptStatus.FAILED
            attempt.error = f"{type(exc).__name__}: {exc}"
            attempt.rollback_reason = CommitDecisionCode.MUTATION_FAILED.value
            decision = CommitDecision(CommitDecisionCode.MUTATION_FAILED, attempt.error)
            return SandboxExecutionResult(current_plan, attempt, decision, candidate)
        finally:
            attempt.quality_after.setdefault("elapsed_ms", round((perf_counter() - started) * 1000, 2))
            self._record_execution(attempt, issue, strategy, decision)

    def _record_execution(
        self,
        attempt: RepairAttempt,
        issue: ExperienceIssue,
        strategy: RepairStrategy,
        decision: CommitDecision | None,
    ) -> None:
        """Persist a compact event; never stores a plan or prompt payload."""
        if self.execution_store is None:
            return
        skill = get_repair_skill_registry().get(strategy)
        if skill is None:
            return
        before = attempt.validation_before
        after = attempt.validation_after
        execution = RepairSkillExecution(
            skill_id=skill.skill_id,
            skill_version=skill.version,
            run_id=self.run_id,
            attempt_id=attempt.attempt_id,
            base_plan_version=attempt.base_plan_version,
            issue_type=issue.issue_type,
            issue_severity=issue.severity,
            issue_fingerprint=issue.fingerprint,
            target_scope=[f"days[{issue.day - 1}]" if issue.day else "days[*]"],
            mutation_succeeded=attempt.status in {RepairAttemptStatus.APPLIED, RepairAttemptStatus.VALIDATED, RepairAttemptStatus.COMMITTED},
            committed=attempt.status == RepairAttemptStatus.COMMITTED,
            quality_improved=(attempt.quality_delta > 0 if attempt.quality_after else None),
            commit_decision=(decision.code if decision else None),
            rollback_reason=attempt.rollback_reason,
            quality_before=attempt.quality_before.get("score"),
            quality_after=attempt.quality_after.get("score"),
            quality_delta=attempt.quality_delta if attempt.quality_after else None,
            hard_violations_before=len(before.get("hard_keys", [])),
            hard_violations_after=len(after.get("hard_keys", [])),
            warning_count_before=attempt.quality_before.get("warning_count", 0),
            warning_count_after=attempt.quality_after.get("warning_count", 0),
            execution_time_ms=attempt.quality_after.get("elapsed_ms", 0),
            pipeline_mode=self.pipeline_mode,
            evaluation_mode=self.evaluation_mode,
            error=attempt.error,
        )
        try:
            self.execution_store.append(execution)
        except OSError:
            # Artifact persistence must not alter repair safety semantics.
            return

    @staticmethod
    def _ensure_version(plan: TripPlan) -> None:
        if not getattr(plan, "plan_version", None):
            plan.plan_version = PlanVersionMetadata()

    @staticmethod
    def _quality_snapshot(evaluation: ExperienceEvaluation) -> dict[str, Any]:
        return {
            "score": evaluation.overall_score,
            "issue_fingerprints": [item.fingerprint for item in evaluation.issues],
            "warning_count": sum(
                item.severity in {"warning", "info"}
                for item in evaluation.issues
            ),
        }

    @staticmethod
    def _poi_key(poi: Attraction) -> str:
        return poi.visit_key or poi.poi_id or poi.name

    @classmethod
    def diff(cls, before: TripPlan, after: TripPlan) -> PlanDiff:
        before_days = {day.day_index: day for day in before.days}
        after_days = {day.day_index: day for day in after.days}
        before_locations = {
            cls._poi_key(poi): index
            for index, day in before_days.items() for poi in day.attractions
        }
        after_locations = {
            cls._poi_key(poi): index
            for index, day in after_days.items() for poi in day.attractions
        }
        changed = [
            index + 1 for index in sorted(set(before_days) | set(after_days))
            if [cls._poi_key(p) for p in before_days.get(index, type("D", (), {"attractions": []})()).attractions]
            != [cls._poi_key(p) for p in after_days.get(index, type("D", (), {"attractions": []})()).attractions]
        ]
        before_walk = sum(day.daily_walking_distance_km for day in before.days)
        after_walk = sum(day.daily_walking_distance_km for day in after.days)
        before_transport = sum(day.daily_travel_minutes for day in before.days)
        after_transport = sum(day.daily_travel_minutes for day in after.days)
        return PlanDiff(
            changed_days=changed,
            added_poi_ids=sorted(set(after_locations) - set(before_locations)),
            removed_poi_ids=sorted(set(before_locations) - set(after_locations)),
            moved_poi_ids=sorted(
                key for key in set(before_locations) & set(after_locations)
                if before_locations[key] != after_locations[key]
            ),
            hotel_changed=[getattr(day.hotel, "poi_id", None) or getattr(day.hotel, "name", None) for day in before.days]
            != [getattr(day.hotel, "poi_id", None) or getattr(day.hotel, "name", None) for day in after.days],
            route_changed=before.route_segments != after.route_segments,
            cost_delta=(after.budget.total if after.budget else 0) - (before.budget.total if before.budget else 0),
            walking_delta=round(after_walk - before_walk, 3),
            transport_time_delta=after_transport - before_transport,
        )
