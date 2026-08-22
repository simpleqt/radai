"""学生练习路由（基础力学课程智能体题库）。

登录用户可访问（student/user/teacher/admin 同权——教师试做也是正常使用），
游客无凭证被底层 verify_api_key 拒绝。题目下发一律脱敏（不带 answer/analysis），
判分提交后才返回；做题记录仅本人可读。手写解答可拍照上传，走
``/check-visual`` 由视觉模型评阅并讲解。
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.api.rate_limit import positive_int_env, request_rate_limiter
from backend.application.question_bank.practice import (
    check_answer,
    list_my_attempts,
    list_wrong_questions,
    practice_question_page,
    public_question,
    record_browse_attempt,
)
from backend.application.question_bank.visual_grading import (
    VisualGradingError,
    build_visual_check_response,
    grade_visual_answer,
    grading_semaphore,
    prepare_grading_image,
    record_visual_attempt,
)
from backend.config.loggers import get_logger
from backend.db.connection import get_async_session
from backend.db.models import QuestionBankQuestion, User

logger = get_logger(__name__)

router = APIRouter(prefix="/practice", tags=["学生-习题练习"])

_MAX_VISUAL_IMAGES = 3
# 与 visual_grading._MAX_IMAGE_BYTES 同源的读取上限：必须在 read() 时生效，
# 否则 5MiB 校验之前整个文件已经进了内存（Starlette spool 文件同样会被
# .read() 全量读回进程）。
_MAX_VISUAL_READ = 5 * 1024 * 1024 + 1
# 视觉判分每次调用一次视觉模型（默认 120s 超时），必须有独立的每用户频率
# 上限，不能只依赖全局 600/min 的认证限流。
_VISUAL_RATE_PER_MINUTE = positive_int_env(
    "PRACTICE_VISUAL_GRADING_RATE_PER_MINUTE", 5
)


class CheckAnswerRequest(BaseModel):
    model_config = {"extra": "forbid"}

    answer: str = Field(max_length=4000)
    self_review: bool | None = None


async def _resolve_username(session: AsyncSession, user_id: str) -> str:
    row = (
        await session.execute(
            select(User.display_name, User.username).where(User.user_id == user_id)
        )
    ).first()
    if row is None:
        return ""
    return str(row[0] or row[1] or "")


async def _get_published_question(
    session: AsyncSession, question_id: str
) -> QuestionBankQuestion:
    question = await session.get(QuestionBankQuestion, question_id)
    if question is None or question.status != "published":
        # 统一 404：不存在与未发布不泄露存在性。
        raise HTTPException(status_code=404, detail="题目不存在或未发布")
    return question


@router.get("/questions", summary="练习题目列表（脱敏，不含答案解析）")
async def list_practice_questions(
    question_type: str | None = Query(default=None),
    difficulty: str | None = Query(default=None),
    knowledge_point: str | None = Query(default=None, max_length=128),
    chapter: str | None = Query(default=None, max_length=128),
    keyword: str | None = Query(default=None, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    items, total = await practice_question_page(
        session,
        question_type=question_type,
        difficulty=difficulty,
        knowledge_point=knowledge_point,
        chapter=chapter,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/questions/{question_id}", summary="题目详情（解答模式显式请求解析）")
async def get_practice_question(
    question_id: str,
    with_solution: bool = Query(default=False),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    question = await _get_published_question(session, question_id)
    item = public_question(question)
    if with_solution:
        item["answer"] = question.answer
        item["analysis"] = question.analysis
        # 解答模式浏览落一条 mode="browse" 的 attempt（is_correct=None），
        # 供教师分析区分「看过答案的学生」；失败不影响下发。
        try:
            username = await _resolve_username(session, auth.user_id)
            await record_browse_attempt(
                session,
                question_id=question.id,
                user_id=auth.user_id,
                username=username,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("[practice] 解答模式浏览记录落库失败: %s", exc)
    return item


@router.post("/questions/{question_id}/check", summary="提交作答并判分")
async def check_practice_answer(
    question_id: str,
    payload: CheckAnswerRequest,
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    question = await _get_published_question(session, question_id)

    async def _run_check():
        username = await _resolve_username(session, auth.user_id)
        return await check_answer(
            session,
            question=question,
            user_id=auth.user_id,
            username=username,
            given=payload.answer,
            mode="practice",
            self_review=payload.self_review,
        )

    # 内容寻址幂等：网关超时重放/双击同一作答直接回缓存，不再双写
    # attempt；修改答案自然生成新 key。
    return await _cached_grade_response(
        f"{auth.user_id}|{question_id}|check|{payload.answer.strip()}|{(payload.self_review or "").strip()}",
        producer=_run_check,
    )


@router.post(
    "/questions/{question_id}/check-visual",
    summary="上传手写解答图片，视觉模型判分并讲解",
)
async def check_practice_answer_visual(
    question_id: str,
    answer: str = Form(default="", max_length=4000),
    images: list[UploadFile] | None = File(default=None),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    # 每用户独立频率闸门（Redis 滑窗 + 断连回退），早于任何 DB/文件/模型工作。
    allowed, retry_after = await request_rate_limiter.allow(
        f"practice-visual:{auth.user_id}", _VISUAL_RATE_PER_MINUTE
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"手写判分提交过于频繁，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )

    question = await _get_published_question(session, question_id)
    uploads = list(images or [])
    if not uploads:
        raise HTTPException(status_code=422, detail="请至少上传一张手写解答图片")
    if len(uploads) > _MAX_VISUAL_IMAGES:
        raise HTTPException(
            status_code=422, detail=f"单次最多上传 {_MAX_VISUAL_IMAGES} 张图片"
        )

    # 图片预处理 + 视觉模型调用是重 IO（默认 120s）：先释放本请求的 DB 事务
    # （连接回池），期间不持有 MySQL 连接与读视图；落记录时再开新事务。
    # rollback 会 expire 会话内 ORM 对象，先 expunge 保住已加载的题目快照。
    session.expunge(question)
    await session.rollback()

    try:
        async with grading_semaphore():
            prepared = []
            for image in uploads:
                content = await image.read(_MAX_VISUAL_READ)
                if len(content) >= _MAX_VISUAL_READ:
                    raise HTTPException(
                        status_code=413, detail="单张图片超过 5 MiB 上限"
                    )
                prepared.append(
                    await prepare_grading_image(
                        content, image.content_type or "image/png"
                    )
                )

            # 图片字节哈希参与幂等 key：同题同答同图的重放（120s 级视觉
            # 判分最常见的网关超时场景）直接回缓存，不再重跑模型/落 attempt。
            image_digest = hashlib.sha256(
                "".join(image.base64 for image in prepared).encode()
            ).hexdigest()
            cache_key_material = (
                f"{auth.user_id}|{question_id}|check-visual|{answer.strip()}|{image_digest}"
            )
            cached_visual = await _read_grade_cache(cache_key_material)
            if cached_visual is not None:
                return _refresh_checked_at(cached_visual)

            try:
                grading = await grade_visual_answer(
                    question, given_text=answer, images=prepared
                )
            except VisualGradingError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VisualGradingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    username = await _resolve_username(session, auth.user_id)
    await record_visual_attempt(
        session,
        question=question,
        user_id=auth.user_id,
        username=username,
        given_text=answer,
        grading=grading,
        image_count=len(prepared),
    )
    response = build_visual_check_response(
        question, grading=grading, image_count=len(prepared)
    )
    await _write_grade_cache(cache_key_material, response)
    return response


async def _read_grade_cache(key_material: str):
    """读判分结果缓存；Redis 不可用/未命中返回 None（静默直通）。"""
    import json as _json

    from backend.db.redis import get_redis

    cache_key = f"practice:grade:{hashlib.sha256(key_material.encode()).hexdigest()}"
    try:
        cached = await get_redis().get(cache_key)
        if cached:
            return _json.loads(cached)
    except Exception:
        return None
    return None


async def _write_grade_cache(key_material: str, response: Any) -> None:
    import json as _json

    from backend.db.redis import get_redis

    cache_key = f"practice:grade:{hashlib.sha256(key_material.encode()).hexdigest()}"
    try:
        payload = (
            response.model_dump_json()
            if isinstance(response, BaseModel)
            else _json.dumps(response, ensure_ascii=False, default=str)
        )
        await get_redis().setex(cache_key, 3600, payload)
    except Exception:
        pass


def _refresh_checked_at(cached):
    """缓存命中的响应把 checked_at 重写为本次时间（判分时刻不应冻结）。"""

    import copy as _copy
    from datetime import datetime as _datetime

    try:
        refreshed = _copy.deepcopy(cached)
        if isinstance(refreshed, dict):
            refreshed["checked_at"] = _datetime.now().isoformat()
        return refreshed
    except Exception:
        return cached


async def _cached_grade_response(key_material: str, *, producer):
    cached = await _read_grade_cache(key_material)
    if cached is not None:
        return _refresh_checked_at(cached)
    result = await producer()
    await _write_grade_cache(key_material, result)
    return result


@router.get("/attempts", summary="我的做题记录（仅本人）")
async def my_attempts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    items, total = await list_my_attempts(
        session, user_id=auth.user_id, limit=limit, offset=offset
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/attempts/wrong", summary="我的错题本（仅本人）")
async def my_wrong_questions(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    items, total = await list_wrong_questions(
        session, user_id=auth.user_id, limit=limit
    )
    return {"items": items, "total": total, "limit": limit}
