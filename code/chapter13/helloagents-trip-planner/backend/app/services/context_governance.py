"""Role-specific prompt payloads with deterministic, safe truncation."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Sequence

from ..models.context_governance import ContextPolicy, ContextRole, ContextTrace, GovernedContext
from ..models.schemas import Attraction, EvidenceSource, TripPlan, TripRequest
from .repair_skill_registry import get_repair_skill_registry
from .repair_skill_metrics import RepairSkillExecutionStore, RepairSkillMetricsAggregator


class TokenEstimator:
    """Stable fallback estimator when a provider tokenizer is unavailable."""
    def estimate(self, value: Any) -> int:
        return max(1, (len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) + 3) // 4)


_POLICIES = {
    ContextRole.EXPERIENCE_EVALUATOR: ContextPolicy(
        role=ContextRole.EXPERIENCE_EVALUATOR,
        required_sections=["request", "hard_constraints", "plan_summary", "validation"],
        optional_sections=["weather", "candidates", "repair_history", "knowledge_summary", "evidence"],
        forbidden_sections=["raw_map_response", "full_candidate_pool", "checkpoint"],
        max_estimated_tokens=2400, max_candidates=8, max_rag_evidence_items=3,
        section_priorities={"repair_history": 1, "evidence": 2, "candidates": 3, "weather": 4},
    ),
    ContextRole.REPAIR_STRATEGIST: ContextPolicy(
        role=ContextRole.REPAIR_STRATEGIST,
        required_sections=["request", "hard_constraints", "plan_summary", "issue", "skill_options"],
        optional_sections=["local_candidates", "repair_history", "skill_metrics"],
        forbidden_sections=["raw_map_response", "full_candidate_pool", "full_repair_attempts"],
        max_estimated_tokens=1800, max_candidates=6, max_rag_evidence_items=0,
    ),
    ContextRole.FINAL_EXPLAINER: ContextPolicy(
        role=ContextRole.FINAL_EXPLAINER,
        required_sections=["request", "plan_summary", "validation"],
        optional_sections=["warnings", "weather", "evidence"],
        forbidden_sections=["candidates", "repair_history", "full_repair_attempts"],
        max_estimated_tokens=1800, max_candidates=0, max_rag_evidence_items=2,
    ),
}


class RoleContextBuilder:
    def __init__(self, estimator: TokenEstimator | None = None) -> None:
        self.estimator = estimator or TokenEstimator()

    def policy_for(self, role: ContextRole) -> ContextPolicy:
        return _POLICIES[role].model_copy(deep=True)

    def build(self, *, role: ContextRole, request: TripRequest, plan: TripPlan, issue=None, candidates: Sequence[Attraction] = (), evidence: Sequence[EvidenceSource] = (), weather_risks: Sequence[str] = (), knowledge_summary: Sequence[dict] = (), policy: ContextPolicy | None = None, run_id: str = "") -> GovernedContext:
        policy = policy or self.policy_for(role)
        hard = self._hard_constraints(request, plan)
        payload = {
            "request": self._request_summary(request),
            "hard_constraints": hard,
            "plan_summary": self._plan_summary(plan, policy.max_plan_days),
            "validation": self._validation(plan),
        }
        included = list(payload)
        optional = self._optional(role, plan, issue, candidates, evidence, weather_risks, knowledge_summary, policy)
        payload.update(optional)
        included.extend(optional)
        before = self.estimator.estimate(payload)
        truncated, reasons = [], []
        for section in self._trim_order(role):
            if self.estimator.estimate(payload) <= policy.max_estimated_tokens:
                break
            if section in payload and section not in policy.required_sections:
                payload.pop(section)
                truncated.append(section)
                reasons.append("token_budget")
        required_over = self.estimator.estimate(payload) > policy.max_estimated_tokens
        if required_over:
            reasons.append("required_context_over_budget")
        return GovernedContext(
            role=role, policy_version=policy.policy_version, payload=payload,
            estimated_tokens_before=before,
            estimated_tokens=self.estimator.estimate(payload), included_sections=[item for item in included if item in payload],
            omitted_sections=[item for item in policy.forbidden_sections if item not in payload],
            truncated_sections=truncated, truncation_reasons=reasons,
            artifact_references=[f"planning-trace:{run_id}"] if run_id else [],
            hard_constraints_present=bool(hard), plan_version=plan.plan_version.version,
            required_context_over_budget=required_over,
        )

    @staticmethod
    def trace(context: GovernedContext, *, node_name: str, run_id: str = "", issue_fingerprint: str = "", model: str = "") -> ContextTrace:
        return ContextTrace(run_id=run_id, node_name=node_name, role=context.role, policy_version=context.policy_version, plan_version=context.plan_version, issue_fingerprint=issue_fingerprint, estimated_tokens_before=context.estimated_tokens_before, estimated_tokens_after=context.estimated_tokens, included_sections=context.included_sections, omitted_sections=context.omitted_sections, truncated_sections=context.truncated_sections, truncation_reasons=context.truncation_reasons, artifact_references=context.artifact_references, hard_constraints_present=context.hard_constraints_present, required_context_over_budget=context.required_context_over_budget, model=model)

    def _optional(self, role, plan, issue, candidates, evidence, weather_risks, knowledge_summary, policy):
        result = {}
        if role == ContextRole.EXPERIENCE_EVALUATOR:
            result["weather"] = list(weather_risks)
            result["candidates"] = [self._candidate(item) for item in list(candidates)[:policy.max_candidates]]
            result["repair_history"] = [self._repair(item) for item in plan.repair_attempts[-policy.max_repair_history_items:]]
            if knowledge_summary:
                result["knowledge_summary"] = list(knowledge_summary)[:8]
            result["evidence"] = [{"title": item.title, "source": item.source, "snippet": item.snippet} for item in list(evidence)[:policy.max_rag_evidence_items]]
        elif role == ContextRole.REPAIR_STRATEGIST:
            result["issue"] = issue.model_dump(mode="json") if issue else {}
            result["local_candidates"] = [self._candidate(item) for item in list(candidates)[:policy.max_candidates]]
            result["repair_history"] = [self._repair(item) for item in plan.repair_attempts[-policy.max_repair_history_items:]]
            skills = get_repair_skill_registry().candidates_for_issue(issue.issue_type) if issue else []
            metrics = RepairSkillMetricsAggregator().aggregate(
                RepairSkillExecutionStore(Path("data/repair_skill_executions.jsonl")).load(),
                issue_type=(issue.issue_type if issue else None),
            )
            result["skill_options"] = [
                {
                    "skill_id": skill.skill_id,
                    "strategy": skill.strategy.value,
                    "version": skill.version,
                    "metrics": {
                        "sample_size": metrics.get(skill.skill_id, {}).get("execution_count", 0),
                        "commit_rate": metrics.get(skill.skill_id, {}).get("commit_rate"),
                        "average_quality_delta": metrics.get(skill.skill_id, {}).get("average_quality_delta"),
                        "insufficient_data": metrics.get(skill.skill_id, {}).get("execution_count", 0) < 5,
                    },
                }
                for skill in skills
            ]
        else:
            result["warnings"] = list(plan.risk_warnings)
            result["weather"] = [{"day": day.day_index + 1, "backup": [poi.name for poi in day.weather_backup], "warning": day.weather_warning} for day in plan.days if day.weather_warning or day.weather_backup]
            result["evidence"] = [{"title": item.title, "snippet": item.snippet} for item in list(evidence)[:policy.max_rag_evidence_items]]
        return result

    @staticmethod
    def _hard_constraints(request, plan):
        return {"must_visit": list(request.must_visit), "avoid_categories": list(request.avoid_categories), "budget_limit": request.budget_limit, "max_daily_walk_km": request.max_daily_walk_km, "daily_start_time": request.daily_start_time, "daily_end_time": request.daily_end_time, "normalized_constraints": [item.model_dump(mode="json") for item in plan.normalized_constraints]}

    @staticmethod
    def _request_summary(request):
        return {"city": request.city, "dates": [request.start_date, request.end_date], "travel_days": request.travel_days, "preferences": list(request.preferences), "pace": request.pace, "travelers": list(request.travelers)}

    @staticmethod
    def _plan_summary(plan, max_days):
        return {"version": plan.plan_version.version, "days": [{"day": day.day_index + 1, "pois": [item.name for item in day.attractions], "travel_minutes": day.daily_travel_minutes, "walking_km": day.daily_walking_distance_km, "utilization": day.day_utilization_score} for day in plan.days[:max_days]], "budget_total": plan.budget.total if plan.budget else None}

    @staticmethod
    def _validation(plan):
        return {"valid": plan.validation_result.valid, "violations": [{"type": item.type, "severity": item.severity, "message": item.message} for item in plan.validation_result.violations]}

    @staticmethod
    def _candidate(item):
        return {
            "id": item.visit_key or item.poi_id,
            "name": item.name,
            "category": item.category,
            "categories": list(item.categories),
            "area": item.area,
            "environment": getattr(item, "environment_type", None) or "unknown",
            "walking_intensity": item.intensity_level,
            "accessible": item.accessible,
            "internal_walking_km": item.estimated_internal_walking_km,
            "score": item.score,
        }

    @staticmethod
    def _repair(item):
        return {"attempt_id": item.attempt_id, "strategy": item.repair_strategy.value, "status": item.status.value, "reason": item.rollback_reason}

    @staticmethod
    def _trim_order(role):
        return ["repair_history", "evidence", "knowledge_summary", "candidates", "local_candidates", "weather", "warnings"]
