"""Trip planning API routes."""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from ...services.planner_service import get_planner
from ...config import settings
from ...models.schemas import ReplanRequest, TripPlanResponse, TripRequest
from ...services.trip_conversation_service import get_trip_conversation_service

router = APIRouter(prefix="/trip", tags=["Trip Planning"])
logger = logging.getLogger("uvicorn.error")


@router.post(
    "/plan",
    response_model=TripPlanResponse,
    summary="Generate a constraint-aware trip plan",
)
async def plan_trip(request: TripRequest):
    try:
        workflow_summary = None
        if settings.workflow_mode.strip().lower() == "langgraph":
            from ...workflows.langgraph_trip_workflow import get_langgraph_trip_workflow

            workflow = get_langgraph_trip_workflow()
            trip_plan, workflow_summary = await run_in_threadpool(workflow.run, request)
        else:
            planner = get_planner()
            trip_plan = await run_in_threadpool(planner.plan_trip, request)
        conversation = get_trip_conversation_service()
        session = await run_in_threadpool(
            conversation.save_existing_plan,
            request,
            trip_plan,
        )
        executable = (
            trip_plan.validation_result.valid
            and trip_plan.quality_gate_passed
            and not trip_plan.unresolved_blocking_issues
        )
        return TripPlanResponse(
            success=executable,
            message=(
                (
                    "旅行计划已生成，并保留未解决的非阻断提示"
                    if trip_plan.unresolved_non_blocking_issues
                    else "旅行计划生成成功"
                )
                if executable
                else "已返回包含完整 TripPlan 的 degraded best-effort 结果；请查看 degraded_reason、unresolved issues 和 suggested_alternatives"
            ),
            data=trip_plan,
            session_id=session.id,
            plan_version=session.current_version,
            workflow=workflow_summary,
        )
    except Exception as exc:
        logger.exception("Trip planning failed")
        raise HTTPException(status_code=500, detail=f"旅行计划生成失败: {exc}") from exc


@router.post(
    "/replan",
    response_model=TripPlanResponse,
    summary="Recalculate routes, budget, and constraints after edits",
)
async def replan_trip(request: ReplanRequest):
    try:
        planner = get_planner()
        trip_plan = await run_in_threadpool(planner.replan, request)
        executable = (
            trip_plan.validation_result.valid
            and trip_plan.quality_gate_passed
            and not trip_plan.unresolved_blocking_issues
        )
        return TripPlanResponse(
            success=executable,
            message=(
                "行程已重新计算"
                if executable
                else "重规划仍有阻断问题；已返回非空 best-effort TripPlan 和处理建议"
            ),
            data=trip_plan,
        )
    except Exception as exc:
        logger.exception("Trip replanning failed")
        raise HTTPException(status_code=500, detail=f"重规划失败: {exc}") from exc


@router.get("/health", summary="Trip planner health check")
async def health_check():
    try:
        planner = get_planner()
        return planner.health_snapshot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"服务不可用: {exc}") from exc
