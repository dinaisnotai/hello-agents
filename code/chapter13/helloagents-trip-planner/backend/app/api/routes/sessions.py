"""Persistent trip-session and conversational replanning routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from ...models.conversation import (
    ChatMessageRequest,
    ChatMessageResponse,
    TripSessionListResponse,
    TripSessionResponse,
)
from ...models.schemas import TripRequest
from ...models.schemas import ReplanRequest
from ...services.trip_conversation_service import get_trip_conversation_service

router = APIRouter(prefix="/trip/sessions", tags=["Trip Sessions"])
logger = logging.getLogger("uvicorn.error")


@router.post("", response_model=TripSessionResponse, summary="Create a persisted trip session")
async def create_session(request: TripRequest):
    try:
        service = get_trip_conversation_service()
        detail = await run_in_threadpool(service.create_session, request)
        return TripSessionResponse(message="旅行会话创建成功", data=detail)
    except Exception as exc:
        logger.exception("Trip session creation failed")
        raise HTTPException(status_code=500, detail=f"旅行会话创建失败: {exc}") from exc


@router.get("", response_model=TripSessionListResponse, summary="List trip sessions")
async def list_sessions(limit: int = Query(default=50, ge=1, le=100)):
    service = get_trip_conversation_service()
    sessions = await run_in_threadpool(service.repository.list, limit)
    return TripSessionListResponse(data=sessions)


@router.get("/{session_id}", response_model=TripSessionResponse, summary="Get a trip session")
async def get_session(session_id: str):
    service = get_trip_conversation_service()
    detail = await run_in_threadpool(service.repository.get, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="旅行会话不存在")
    return TripSessionResponse(data=detail)
-

@router.post(
    "/{session_id}/messages",
    response_model=ChatMessageResponse,
    summary="Modify the current trip through a chat message",
)
async def send_message(session_id: str, request: ChatMessageRequest):
    service = get_trip_conversation_service()
    try:
        detail, patch, assistant_message = await run_in_threadpool(
            service.send_message,
            session_id,
            request.content,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="旅行会话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Trip conversational replanning failed")
        raise HTTPException(status_code=500, detail=f"行程修改失败: {exc}") from exc
    return ChatMessageResponse(
        assistant_message=assistant_message,
        applied_patch=patch,
        data=detail,
    )


@router.post(
    "/{session_id}/replan",
    response_model=TripSessionResponse,
    summary="Persist manual plan edits as a new version",
)
async def replan_session(session_id: str, request: ReplanRequest):
    service = get_trip_conversation_service()
    try:
        detail = await run_in_threadpool(
            service.replan_existing,
            session_id,
            request,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="旅行会话不存在") from exc
    except Exception as exc:
        logger.exception("Persisted trip replanning failed")
        raise HTTPException(status_code=500, detail=f"重规划失败: {exc}") from exc
    return TripSessionResponse(message="行程已重新计算并保存", data=detail)
