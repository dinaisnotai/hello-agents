"""Trip planning API routes."""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from ...agents.multi_agent_orchestrator import get_multi_agent_orchestrator
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
        planner = get_multi_agent_orchestrator()
        trip_plan = await run_in_threadpool(planner.plan_trip, request)
        conversation = get_trip_conversation_service()
        session = await run_in_threadpool(
            conversation.save_existing_plan,
            request,
            trip_plan,
        )
        return TripPlanResponse(
            success=True,
            message="旅行计划生成成功",
            data=trip_plan,
            session_id=session.id,
            plan_version=session.current_version,
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
        planner = get_multi_agent_orchestrator()
        trip_plan = await run_in_threadpool(planner.replan, request)
        return TripPlanResponse(success=True, message="行程已重新计算", data=trip_plan)
    except Exception as exc:
        logger.exception("Trip replanning failed")
        raise HTTPException(status_code=500, detail=f"重规划失败: {exc}") from exc


@router.get("/health", summary="Trip planner health check")
async def health_check():
    try:
        planner = get_multi_agent_orchestrator()
        return planner.health_snapshot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"服务不可用: {exc}") from exc
