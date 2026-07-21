"""Trip planning API routes."""

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from ...agents.trip_planner_agent import get_trip_planner_agent
from ...models.schemas import ReplanRequest, TripPlanResponse, TripRequest

router = APIRouter(prefix="/trip", tags=["Trip Planning"])


@router.post(
    "/plan",
    response_model=TripPlanResponse,
    summary="Generate a constraint-aware trip plan",
)
async def plan_trip(request: TripRequest):
    try:
        planner = get_trip_planner_agent()
        trip_plan = await run_in_threadpool(planner.plan_trip, request)
        return TripPlanResponse(success=True, message="旅行计划生成成功", data=trip_plan)
    except Exception as exc:
        print(f"Trip planning failed: {exc}")
        raise HTTPException(status_code=500, detail=f"旅行计划生成失败: {exc}") from exc


@router.post(
    "/replan",
    response_model=TripPlanResponse,
    summary="Recalculate routes, budget, and constraints after edits",
)
async def replan_trip(request: ReplanRequest):
    try:
        planner = get_trip_planner_agent()
        trip_plan = await run_in_threadpool(planner.replan, request)
        return TripPlanResponse(success=True, message="行程已重新计算", data=trip_plan)
    except Exception as exc:
        print(f"Trip replanning failed: {exc}")
        raise HTTPException(status_code=500, detail=f"重规划失败: {exc}") from exc


@router.get("/health", summary="Trip planner health check")
async def health_check():
    try:
        planner = get_trip_planner_agent()
        return {
            "status": "healthy",
            "service": "constraint-aware-trip-planner",
            "roles": [
                planner.poi_collector.__class__.__name__,
                planner.route_evaluator.__class__.__name__,
                planner.budget_estimator.__class__.__name__,
                planner.constraint_checker.__class__.__name__,
                planner.reviewer.__class__.__name__,
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"服务不可用: {exc}") from exc
