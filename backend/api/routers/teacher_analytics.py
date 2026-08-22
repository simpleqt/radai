"""教师提问分析路由（基础力学课程智能体专属）。

仅教师/管理员可访问；``agent_code`` 固定为 fmca，传入其他值直接 422。
提问列表/总览为纯 SQL 只读；``/insights`` 是进入教学分析即见的预制大屏数据
（确定性统计，无 LLM、无需教师手动配置时间窗）；分析报告为后台生成的快照。
完整对话查看以 ``course_question_records`` 的 (user_id, thread_id) 记录为
fmca 归属凭证——采集只在 fmca 提问入口落库，因此其他智能体（ne/csh/审计）
的会话永远不会有记录，教师无法借该接口越权查看。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, require_teacher
from backend.application.question_analytics import (
    TEACHER_ANALYTICS_AGENT_CODE,
    create_report,
    get_report,
    list_reports,
    overview_stats,
    question_page,
)
from backend.config.loggers import get_logger
from backend.db.connection import get_async_session
from backend.services.admin_audit import write_admin_audit

logger = get_logger(__name__)

router = APIRouter(prefix="/teacher/analytics", tags=["教师-提问分析"])


class CreateReportRequest(BaseModel):
    model_config = {"extra": "forbid"}

    agent_code: str = Field(default=TEACHER_ANALYTICS_AGENT_CODE, max_length=32)
    from_: datetime = Field(alias="from")
    to: datetime


def _is_admin(auth: ApiKeyAuth) -> bool:
    return (auth.role or "").lower() == "admin"


# 未收尾/超时的兜底文案前缀（stream_handler 追加到回答末尾或独立成条）。
# 这类 chunk 不是有效回答，教师视图中标记为 error 单独呈现，不与正文混排。
_ERROR_ANSWER_PREFIXES = (
    "本轮已执行检索和工具调用，但模型最终答案没有正常收尾",
    "当前流程在生成最终答案前超时",
)

_STUDENT_ROLES = frozenset({"user", "human"})
_AGENT_ROLES = frozenset({"ai", "assistant"})


def _normalize_question_text(text: str) -> str:
    return "".join(str(text or "").split())


def _split_error_tail(text: str) -> tuple[str, str | None]:
    """把混在回答正文里的兜底报错文案剥离为独立 error 条目。"""
    for prefix in _ERROR_ANSWER_PREFIXES:
        index = text.find(prefix)
        if index >= 0:
            return text[:index], text[index:]
    return text, None


def _build_conversation_turns(messages: list[dict]) -> list[dict]:
    """把检查点消息重组成教师视角的问答轮次。

    - 学生消息开启新一轮；与前一轮完全相同的连续提问合并为重发
      （repeat_count 计数）——失败重试不再刷出重复的问题墙；
    - 智能体消息分类：兜底报错文案 → error；轮内最后一条非报错消息 →
      answer（最终回答）；其余多轮工具调用的过程播报 → process；
    - 空内容消息丢弃；对话本应从学生提问开始，防御性地允许无提问轮次。
    """
    turns: list[dict] = []
    for message in messages:
        role = str(message.get("role") or "")
        content = message.get("content")
        text = content if isinstance(content, str) else str(content or "")
        if role in _STUDENT_ROLES:
            if not text.strip():
                continue
            normalized = _normalize_question_text(text)
            current = turns[-1] if turns else None
            if current is not None and current["normalized"] == normalized:
                current["repeat_count"] += 1
                continue
            turns.append(
                {
                    "question": text,
                    "normalized": normalized,
                    "repeat_count": 1,
                    "answers": [],
                }
            )
        elif role in _AGENT_ROLES:
            if not text.strip():
                continue
            if not turns:
                turns.append(
                    {"question": "", "normalized": "", "repeat_count": 1, "answers": []}
                )
            body, error_tail = _split_error_tail(text)
            if body.strip():
                turns[-1]["answers"].append(
                    {"content": body.rstrip(), "kind": "answer"}
                )
            if error_tail is not None:
                turns[-1]["answers"].append(
                    {
                        "content": error_tail.strip(),
                        "kind": "error",
                        # 独立成条 = 该轮没有任何正文；混排在正文尾部的
                        # 报错不否定正文本身。
                        "standalone": not body.strip(),
                    }
                )

    result: list[dict] = []
    for turn in turns:
        answers = turn["answers"]
        body_indexes = [
            index for index, answer in enumerate(answers)
            if answer["kind"] == "answer"
        ]
        last_body = body_indexes[-1] if body_indexes else -1
        # 最后一条正文之后还跟着独立报错 → 该轮以失败收尾，无最终回答。
        failed_tail = any(
            answer["kind"] == "error"
            and answer.get("standalone")
            and index > last_body
            for index, answer in enumerate(answers)
        )
        final_index = last_body if last_body >= 0 and not failed_tail else -1
        for index, answer in enumerate(answers):
            if answer["kind"] == "answer" and index != final_index:
                answer["kind"] = "process"
            answer.pop("standalone", None)
        result.append(
            {
                "question": turn["question"],
                "repeat_count": turn["repeat_count"],
                "has_answer": final_index >= 0,
                "answers": answers,
            }
        )
    return result


def _assert_fmca(agent_code: str) -> None:
    if str(agent_code or "").strip() != TEACHER_ANALYTICS_AGENT_CODE:
        raise HTTPException(
            status_code=422,
            detail=f"教师提问分析仅支持智能体: {TEACHER_ANALYTICS_AGENT_CODE}",
        )


async def _audit(
    session: AsyncSession,
    request: Request,
    auth: ApiKeyAuth,
    *,
    action: str,
    target_type: str,
    target_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """尽力而为的审计写入：失败只记日志并回滚，绝不让读接口 500。"""
    try:
        await write_admin_audit(
            session,
            request,
            auth,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("[TeacherAnalytics] 审计写入失败（不影响响应）: %s", exc)


def _to_item(row) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "username": row.username,
        "agent_code": row.agent_code,
        "thread_id": row.thread_id,
        "question": row.question,
        "question_len": row.question_len,
        "workflow": row.workflow,
        "model_name": row.model_name,
        "is_guest": bool(row.is_guest),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _report_item(report) -> dict:
    return {
        "id": report.id,
        "agent_code": report.agent_code,
        "created_by": report.created_by,
        "range_start": report.range_start.isoformat() if report.range_start else None,
        "range_end": report.range_end.isoformat() if report.range_end else None,
        "question_count": report.question_count,
        "status": report.status,
        "model_name": report.model_name,
        "error": report.error,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "finished_at": report.finished_at.isoformat() if report.finished_at else None,
    }


@router.get("/questions")
async def list_questions(
    request: Request,
    agent_code: str = Query(default=TEACHER_ANALYTICS_AGENT_CODE),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(require_teacher),
):
    _assert_fmca(agent_code)
    now = datetime.now()
    range_end = to or now
    range_start = from_ or datetime(2000, 1, 1)

    items, total = await question_page(
        session,
        range_start=range_start,
        range_end=range_end,
        keyword=keyword,
        user_id=user_id,
        limit=limit,
        offset=offset,
    )
    # 仅首页记审计，避免翻页噪声。
    if offset == 0:
        await _audit(
            session,
            request,
            auth,
            action="teacher.questions.list",
            target_type="agent",
            target_id=TEACHER_ANALYTICS_AGENT_CODE,
        )
    return {"items": [_to_item(row) for row in items], "total": total, "limit": limit, "offset": offset}


@router.get("/overview")
async def get_overview(
    request: Request,
    agent_code: str = Query(default=TEACHER_ANALYTICS_AGENT_CODE),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(require_teacher),
):
    _assert_fmca(agent_code)
    stats = await overview_stats(session)
    await _audit(
        session,
        request,
        auth,
        action="teacher.overview",
        target_type="agent",
        target_id=TEACHER_ANALYTICS_AGENT_CODE,
    )
    return stats


@router.post("/reports", status_code=201)
async def post_report(
    payload: CreateReportRequest,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(require_teacher),
):
    _assert_fmca(payload.agent_code)
    from backend.application.question_analytics.service import (
        ReportCreationCooldown,
    )

    try:
        report = await create_report(
            session,
            created_by=auth.user_id,
            range_start=payload.from_,
            range_end=payload.to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ReportCreationCooldown as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _audit(
        session,
        request,
        auth,
        action="teacher.report.create",
        target_type="report",
        target_id=report.id,
        detail={
            "agent_code": TEACHER_ANALYTICS_AGENT_CODE,
            "range_start": payload.from_.isoformat(),
            "range_end": payload.to.isoformat(),
        },
    )
    return _report_item(report)


@router.get("/reports")
async def list_report_records(
    request: Request,
    agent_code: str = Query(default=TEACHER_ANALYTICS_AGENT_CODE),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(require_teacher),
):
    _assert_fmca(agent_code)
    reports = await list_reports(session)
    await _audit(
        session,
        request,
        auth,
        action="teacher.reports.list",
        target_type="agent",
        target_id=TEACHER_ANALYTICS_AGENT_CODE,
    )
    # 列表只给本人创建的报告（admin 看全部）；详情/删除同规则。
    visible = [
        report
        for report in reports
        if _is_admin(auth) or report.created_by == auth.user_id
    ]
    return {"items": [_report_item(r) for r in visible]}


@router.get("/reports/{report_id}")
async def get_report_detail(
    report_id: str,
    request: Request,
    agent_code: str = Query(default=TEACHER_ANALYTICS_AGENT_CODE),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(require_teacher),
):
    _assert_fmca(agent_code)
    report = await get_report(session, report_id)
    if report is None or not (_is_admin(auth) or report.created_by == auth.user_id):
        # 他人报告与不存在一律 404，不泄露存在性。
        raise HTTPException(status_code=404, detail="报告不存在")
    item = _report_item(report)
    item["result"] = report.result_json
    return item


@router.get("/insights")
async def get_insights(
    request: Request,
    days: int = Query(30, ge=7, le=180),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(require_teacher),
):
    """预制大屏数据：进入教学分析即见的可视化（确定性统计，无需教师配置）。

    时间窗默认近 30 天（可传 7-180），由 URL 参数而非表单驱动；不触发 LLM。
    """
    from backend.application.question_analytics.collector import deterministic_stats
    from backend.application.question_analytics.practice import (
        practice_deterministic_stats,
    )

    now = datetime.now()
    range_start = now - timedelta(days=days)
    overview = await overview_stats(session)
    stats = await deterministic_stats(
        session, range_start=range_start, range_end=now, top_n=10
    )
    practice = await practice_deterministic_stats(
        session, range_start=range_start, range_end=now, top_n=10
    )
    await _audit(
        session,
        request,
        auth,
        action="teacher.insights",
        target_type="agent",
        target_id=TEACHER_ANALYTICS_AGENT_CODE,
        detail={"days": days},
    )
    return {
        "days": days,
        "overview": overview,
        "trend": stats["trend"],
        "top_questions": stats["top_questions"],
        "top_keywords": stats["top_keywords"][:20],
        "unique_questions": stats["unique_questions"],
        "question_count": stats["question_count"],
        "practice": practice,
    }


@router.get("/practice/overview")
async def get_practice_overview(
    request: Request,
    days: int = Query(30, ge=7, le=180),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(require_teacher),
):
    """班级练习总览：确定性练习统计 + 提问×做题交叉洞察 + 薄弱点抽题建议。

    题库为空时 ``stats.empty=true`` 且交叉/推荐为空列表——提问分析独立可用。
    """
    from backend.application.question_analytics.practice import practice_overview

    now = datetime.now()
    range_start = now - timedelta(days=days)
    payload = await practice_overview(
        session, range_start=range_start, range_end=now
    )
    await _audit(
        session,
        request,
        auth,
        action="teacher.practice.overview",
        target_type="agent",
        target_id=TEACHER_ANALYTICS_AGENT_CODE,
        detail={"days": days},
    )
    return {"days": days, **payload}


@router.get("/conversations/{thread_id}")
async def get_conversation_messages(
    thread_id: str,
    request: Request,
    user_id: str = Query(..., min_length=1, max_length=64),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(require_teacher),
):
    """查看某学生在基础力学智能体下某会话的完整对话记录。

    fmca 隔离凭证：必须存在 (user_id, thread_id, agent_code='fmca') 的提问
    采集记录。其他智能体的会话没有采集记录，一律 404。
    """
    from sqlalchemy import select

    from backend.db.models import CourseQuestionRecord

    if user_id.startswith("guest") or user_id == "guest":
        # 访客会话的 checkpoint 在 Guest API 进程独立的数据库中，
        # Agent API 进程无法读取；提问文本仍可在列表中查看。
        raise HTTPException(status_code=404, detail="访客会话不支持查看完整对话")

    record = (
        await session.execute(
            select(
                CourseQuestionRecord.thread_id,
                CourseQuestionRecord.username,
            )
            .where(
                CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
                CourseQuestionRecord.user_id == user_id,
                CourseQuestionRecord.thread_id == thread_id,
            )
            .limit(1)
        )
    ).first()
    if record is None:
        # 统一 404：不存在、非本学生、或非基础力学智能体的会话不泄露存在性。
        raise HTTPException(
            status_code=404,
            detail="会话不存在或不属于基础力学智能体",
        )
    # 凭证校验后立即释放本请求的 DB 事务：随后的会话目录读取与 checkpoint
    # 读取（可能 MB 级 SQLite blob）不应钉住 MySQL 连接；审计写入会开新事务。
    await session.rollback()

    title: str | None = None
    registered_images: dict = {}
    try:
        from backend.application.conversations.local_catalog import (
            get_local_conversation_catalog,
        )

        catalog = get_local_conversation_catalog()
        if catalog is not None:
            conv = await catalog.get(user_id=user_id, thread_id=thread_id)
            if conv is not None:
                title = conv.title
                registered_images = (conv.metadata_ or {}).get(
                    "registered_images"
                ) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TeacherAnalytics] 读取会话目录失败（降级无标题）: %s", exc)

    messages: list[dict] = []
    turns: list[dict] = []
    try:
        from backend.application.checkpointing.checkpointer import (
            get_checkpointer_from_request,
        )
        from backend.application.conversations.message_serializer import (
            serialize_checkpoint_messages,
        )
        from backend.core.thread_key import build_internal_thread_key

        internal_thread_id = build_internal_thread_key(user_id, thread_id)
        checkpointer = get_checkpointer_from_request(request)
        checkpoint = await checkpointer.aget(
            {"configurable": {"thread_id": internal_thread_id}}
        )
        if checkpoint:
            raw_messages = (checkpoint.get("channel_values") or {}).get(
                "messages", []
            )
            messages = serialize_checkpoint_messages(
                raw_messages,
                registered_images=registered_images,
                coerce_content_to_str=True,
            )
        turns = _build_conversation_turns(messages)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[TeacherAnalytics] 读取对话 checkpoint 失败: user=%s thread=%s: %s",
            user_id,
            thread_id,
            exc,
        )

    await _audit(
        session,
        request,
        auth,
        action="teacher.conversation.view",
        target_type="conversation",
        target_id=thread_id,
        detail={"user_id": user_id, "agent_code": TEACHER_ANALYTICS_AGENT_CODE},
    )
    return {
        "thread_id": thread_id,
        "user_id": user_id,
        "username": record.username or "",
        "title": title,
        "turns": turns,
    }


@router.delete("/reports/{report_id}")
async def remove_report(
    report_id: str,
    request: Request,
    agent_code: str = Query(default=TEACHER_ANALYTICS_AGENT_CODE),
    session: AsyncSession = Depends(get_async_session),
    auth: ApiKeyAuth = Depends(require_teacher),
):
    _assert_fmca(agent_code)
    report = await get_report(session, report_id)
    if report is None or not (_is_admin(auth) or report.created_by == auth.user_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    await session.delete(report)
    await session.commit()
    await _audit(
        session,
        request,
        auth,
        action="teacher.report.delete",
        target_type="report",
        target_id=report_id,
    )
    return {"success": True}
