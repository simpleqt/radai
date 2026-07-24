"""学习偏好测评 API。"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.db.connection import get_async_session
from backend.db.models import LearningStyleAttempt, LearningStyleProfile, User
from backend.services.learning_style_assessment import (
    QUESTIONNAIRE_VERSION,
    get_questionnaire,
    score_answers,
    validate_answers,
)

router = APIRouter(prefix="/learning-style", tags=["学习偏好测评"])


class SaveAnswersRequest(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)
    current_dimension: int = Field(default=0, ge=0, le=3)


class ProfilePreferencesRequest(BaseModel):
    apply_to_ai: bool


def _serialize_attempt(attempt: LearningStyleAttempt) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "questionnaire_version": attempt.questionnaire_version,
        "status": attempt.status,
        "answers": attempt.answers or {},
        "scores": attempt.scores,
        "result": attempt.result,
        "current_dimension": attempt.current_dimension,
        "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
        "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
        "updated_at": attempt.updated_at.isoformat() if attempt.updated_at else None,
    }


def _serialize_profile(profile: LearningStyleProfile) -> dict[str, Any]:
    return {
        "user_id": profile.user_id,
        "latest_attempt_id": profile.latest_attempt_id,
        "scores": profile.scores,
        "result": profile.result,
        "apply_to_ai": bool(profile.apply_to_ai),
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


async def _owned_attempt(
    attempt_id: str,
    user_id: str,
    session: AsyncSession,
) -> LearningStyleAttempt:
    result = await session.execute(
        select(LearningStyleAttempt).where(
            LearningStyleAttempt.attempt_id == attempt_id,
            LearningStyleAttempt.user_id == user_id,
        )
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=404, detail="测评记录不存在")
    return attempt


@router.get("/questionnaire")
async def questionnaire():
    """获取公开题库。"""
    return get_questionnaire()


@router.get("/attempts/current")
async def current_attempt(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(LearningStyleAttempt)
        .where(
            LearningStyleAttempt.user_id == user.user_id,
            LearningStyleAttempt.status == "in_progress",
            LearningStyleAttempt.questionnaire_version == QUESTIONNAIRE_VERSION,
        )
        .order_by(LearningStyleAttempt.updated_at.desc())
        .limit(1)
    )
    attempt = result.scalar_one_or_none()
    return {"attempt": _serialize_attempt(attempt) if attempt else None}


@router.post("/attempts")
async def create_attempt(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    existing_result = await session.execute(
        select(LearningStyleAttempt)
        .where(
            LearningStyleAttempt.user_id == user.user_id,
            LearningStyleAttempt.status == "in_progress",
            LearningStyleAttempt.questionnaire_version == QUESTIONNAIRE_VERSION,
        )
        .order_by(LearningStyleAttempt.updated_at.desc())
        .limit(1)
    )
    existing_attempt = existing_result.scalar_one_or_none()
    if existing_attempt:
        return {"attempt": _serialize_attempt(existing_attempt)}

    attempt = LearningStyleAttempt(
        user_id=user.user_id,
        questionnaire_version=QUESTIONNAIRE_VERSION,
        status="in_progress",
        answers={},
        current_dimension=0,
    )
    session.add(attempt)
    await session.flush()
    return {"attempt": _serialize_attempt(attempt)}


@router.put("/attempts/{attempt_id}/answers")
async def save_answers(
    attempt_id: str,
    payload: SaveAnswersRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    attempt = await _owned_attempt(attempt_id, user.user_id, session)
    if attempt.status != "in_progress":
        raise HTTPException(status_code=409, detail="已完成的测评不能修改")
    try:
        attempt.answers = validate_answers(payload.answers)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    attempt.current_dimension = payload.current_dimension
    attempt.updated_at = datetime.now()
    await session.flush()
    return {"attempt": _serialize_attempt(attempt)}


@router.post("/attempts/{attempt_id}/submit")
async def submit_attempt(
    attempt_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    attempt = await _owned_attempt(attempt_id, user.user_id, session)
    if attempt.status == "completed" and attempt.result:
        profile_result = await session.execute(
            select(LearningStyleProfile).where(LearningStyleProfile.user_id == user.user_id)
        )
        profile = profile_result.scalar_one_or_none()
        return {
            "attempt": _serialize_attempt(attempt),
            "profile": _serialize_profile(profile) if profile else None,
        }

    try:
        result_payload = score_answers(attempt.answers or {})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    now = datetime.now()
    attempt.status = "completed"
    attempt.scores = result_payload["scores"]
    attempt.result = result_payload
    attempt.current_dimension = 3
    attempt.completed_at = now
    attempt.updated_at = now

    profile_result = await session.execute(
        select(LearningStyleProfile).where(LearningStyleProfile.user_id == user.user_id)
    )
    profile = profile_result.scalar_one_or_none()
    if profile is None:
        profile = LearningStyleProfile(
            user_id=user.user_id,
            latest_attempt_id=attempt.attempt_id,
            scores=result_payload["scores"],
            result=result_payload,
            apply_to_ai=False,
        )
        session.add(profile)
    else:
        profile.latest_attempt_id = attempt.attempt_id
        profile.scores = result_payload["scores"]
        profile.result = result_payload
        profile.updated_at = now

    await session.flush()
    return {"attempt": _serialize_attempt(attempt), "profile": _serialize_profile(profile)}


@router.get("/profile")
async def get_profile(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(LearningStyleProfile).where(LearningStyleProfile.user_id == user.user_id)
    )
    profile = result.scalar_one_or_none()
    return {"profile": _serialize_profile(profile) if profile else None}


@router.patch("/profile/preferences")
async def update_profile_preferences(
    payload: ProfilePreferencesRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(LearningStyleProfile).where(LearningStyleProfile.user_id == user.user_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="请先完成学习偏好测评")
    profile.apply_to_ai = payload.apply_to_ai
    profile.updated_at = datetime.now()
    await session.flush()
    return {"profile": _serialize_profile(profile)}
