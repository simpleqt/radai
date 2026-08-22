"""分析报告生命周期编排：生成/查询/删除。

报告是快照：一次生成写入 result_json，之后反复查看不再重跑模型。
生成逻辑放在 FastAPI 后台任务里执行，接口先返回 running 状态。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.question_analytics import clustering
from backend.application.question_analytics.collector import (
    TEACHER_ANALYTICS_AGENT_CODE,
    deterministic_stats,
    fetch_question_ids,
    fetch_questions,
    fetch_questions_by_ids,
)
from backend.config.loggers import get_logger
from backend.db.models import CourseQuestionReport

logger = get_logger(__name__)

_REPORT_STATUS_RUNNING = "running"
_REPORT_STATUS_DONE = "done"
_REPORT_STATUS_FAILED = "failed"

_MAX_WINDOW_DAYS = 180
_MAX_QUESTIONS = int(os.environ.get("TEACHER_ANALYTICS_MAX_QUESTIONS", "5000") or "5000")
# running 报告超过该时长视为进程重启遗留的卡死行，允许新建报告前先置 failed。
_STALE_RUNNING_MINUTES = int(
    os.environ.get("TEACHER_ANALYTICS_STALE_RUNNING_MINUTES", "60") or "60"
)
# 生成任务的整体截止时间：必须小于 stale 回收窗口，否则慢端点下合法仍在生成
# 的报告会被 stale 回收置 failed 并双开第二个生成协程（单飞被打破、双倍模型
# 消耗）。默认 = stale 窗口 - 10 分钟余量，下限 5 分钟。
_GENERATION_TIMEOUT_SECONDS = max(
    300.0,
    float(
        os.environ.get(
            "TEACHER_ANALYTICS_GENERATION_TIMEOUT_SECONDS",
            str(max(300, _STALE_RUNNING_MINUTES * 60 - 600)),
        )
    ),
)
# 同一教师两次创建报告的最小间隔（进程内计时；Agent API 单 worker）。
_USER_CREATE_COOLDOWN_SECONDS = 30

# 串行化「running 检查 + 插入 + 提交」，避免并发创建双双通过单飞检查。
_creation_lock: asyncio.Lock | None = None
# 后台生成任务的强引用集合：事件循环对任务只持弱引用，不持引用会被 GC 中途丢弃。
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
_USER_LAST_CREATE: dict[str, float] = {}


class ReportCreationCooldown(RuntimeError):
    """同一教师创建报告过于频繁。``retry_after_seconds`` 供 429 Retry-After。"""

    def __init__(self, message: str, *, retry_after_seconds: int = 1) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))


def _creation_guard() -> asyncio.Lock:
    global _creation_lock
    if _creation_lock is None:
        _creation_lock = asyncio.Lock()
    return _creation_lock


def _spawn_background(coro) -> None:
    """创建并持有后台任务引用，异常退出时记 error 而非静默消失。"""
    task = asyncio.get_running_loop().create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _done(done: asyncio.Task[None]) -> None:
        _BACKGROUND_TASKS.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            logger.error(
                "分析报告后台任务异常退出",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(_done)


async def shutdown(timeout_seconds: float = 10.0) -> None:
    """停机时取消在途报告任务并标记失败，避免 running 行等 stale 回收。"""
    tasks = [t for t in _BACKGROUND_TASKS if not t.done()]
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
    for task in pending:
        logger.warning("分析报告后台任务停机超时未退出: %s", task.get_name())


def _window_span_days(range_start: datetime, range_end: datetime) -> int:
    return max(1, (range_end - range_start).days)


async def _count_questions(
    session: AsyncSession, *, range_start: datetime, range_end: datetime
) -> int:
    from sqlalchemy import func

    from backend.db.models import CourseQuestionRecord

    return int(
        await session.scalar(
            select(func.count())
            .select_from(CourseQuestionRecord)
            .where(
                CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
                CourseQuestionRecord.created_at >= range_start,
                CourseQuestionRecord.created_at < range_end,
            )
        )
        or 0
    )


def _classify_failure(exc: BaseException) -> str:
    """失败原因分类文案：入库只存分类，不存原始异常文本（防内部细节外泄）。"""
    if isinstance(exc, asyncio.TimeoutError):
        return "生成超时（超过整体时限）"
    name = type(exc).__name__
    if "Operational" in name or "DBAPI" in name or "DatabaseError" in name:
        return f"数据库异常（{name}）"
    if "Timeout" in name or "timeout" in name.lower():
        return f"模型或下游服务超时（{name}）"
    return f"生成失败（{name}）"


async def _mark_report_failed(report_id: str, reason: str) -> None:
    from backend.db.connection import async_session_context

    try:
        async with async_session_context() as session:
            report = await session.get(CourseQuestionReport, report_id)
            if report is None:
                return
            if report.status != _REPORT_STATUS_RUNNING:
                # 与写回段同款守卫：整体超时的取消窗口内 COMMIT 可能已落库
                # （done），迟到的失败标记不得覆盖已完成结果。
                return
            report.status = _REPORT_STATUS_FAILED
            report.error = str(reason)[:500]
            report.finished_at = datetime.now()
            await session.commit()
    except Exception as inner:  # noqa: BLE001
        logger.error("[TeacherAnalytics] 报告失败态落库失败: %s", inner)


async def _generate_report(report_id: str) -> None:
    """三段式生成：读快照（短会话）→ LLM 聚类（不持会话）→ 短会话写回。

    LLM 聚类每批最长 60s、最多 63 批，绝不能在一个打开的 DB 事务里执行
    （连接会被钉住、读视图长期持有）；写回前重读状态，已被 stale 回收置
    failed 的行不会被「迟到完成」覆盖成 done。两段会话都走
    ``async_session_context``（测试的统一 patch 点）。
    """
    from backend.application.question_analytics import practice as practice_stats
    from backend.db.connection import async_session_context

    async with async_session_context() as session:
        report = await session.get(CourseQuestionReport, report_id)
        if report is None:
            return
        range_start = report.range_start
        range_end = report.range_end

        stats = await deterministic_stats(
            session, range_start=range_start, range_end=range_end
        )
        practice_summary = await practice_stats.practice_deterministic_stats(
            session, range_start=range_start, range_end=range_end
        )
        window_question_texts = [
            item["question"]
            for item in await fetch_questions(
                session,
                range_start=range_start,
                range_end=range_end,
                limit=_MAX_QUESTIONS,
            )
        ]
        cross = practice_stats.cross_insights(
            practice_summary, question_texts=window_question_texts
        )
        weak_points = [
            item["knowledge_point"]
            for item in cross
            if item["category"] != "asked_only"
        ]
        recommended = await practice_stats.recommended_exercises(
            session, weak_knowledge_points=weak_points
        )

        total = stats["question_count"]
        questions: list[dict] = []
        sampled = False
        if total > _MAX_QUESTIONS:
            sampled = True
            all_ids = await fetch_question_ids(
                session, range_start=range_start, range_end=range_end
            )
            sampled_ids = uniform_sample_ids(all_ids, _MAX_QUESTIONS)
            questions = await fetch_questions_by_ids(session, sampled_ids)
        else:
            questions = await fetch_questions(
                session,
                range_start=range_start,
                range_end=range_end,
                limit=_MAX_QUESTIONS,
            )

    llm = await clustering.run_llm_clustering(questions, weak_points=weak_points)
    result = _serialize_result(
        stats,
        llm,
        sampled=sampled,
        sampled_total=total,
        questions=questions,
        practice_summary=practice_summary,
        cross_insights=cross,
        recommended_exercises=recommended,
    )

    async with async_session_context() as session:
        report = await session.get(CourseQuestionReport, report_id)
        if report is None:
            return
        if report.status != _REPORT_STATUS_RUNNING:
            # 已被 stale 回收/删除：迟到的结果不覆盖失败态。
            logger.warning(
                "[TeacherAnalytics] 报告 %s 状态已变为 %s，丢弃迟到结果",
                report_id,
                report.status,
            )
            return
        report.result_json = result
        report.question_count = stats["question_count"]
        report.model_name = llm.get("model_name")
        report.status = _REPORT_STATUS_DONE
        report.finished_at = datetime.now()
        await session.commit()


async def _run_generation(report_id: str) -> None:
    """后台生成报告：整体截止时间内完成，超时/异常落失败态（分类文案）。"""
    try:
        await asyncio.wait_for(
            _generate_report(report_id), timeout=_GENERATION_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[TeacherAnalytics] 报告生成失败: %s", exc)
        await _mark_report_failed(report_id, _classify_failure(exc))


def uniform_sample_ids(ids: list[str], limit: int) -> list[str]:
    """等距抽取 limit 个 id（确定性、含首尾，保证覆盖窗口两端）。"""
    if len(ids) <= limit:
        return list(ids)
    if limit == 1:
        return [ids[-1]]
    step = (len(ids) - 1) / (limit - 1)
    return [ids[round(index * step)] for index in range(limit)]


def _cluster_samples(
    clusters: list[dict], id_to_text: dict[str, str], *, per_cluster: int = 5
) -> None:
    """给每个簇附加最多 per_cluster 条代表问题原文，供前端下钻展示。"""
    for cluster in clusters:
        samples: list[str] = []
        for record_id in cluster.get("ids") or []:
            text = id_to_text.get(str(record_id))
            if text:
                samples.append(text[:200])
            if len(samples) >= per_cluster:
                break
        cluster["samples"] = samples


def _serialize_result(
    stats: dict,
    llm: dict,
    *,
    sampled: bool,
    sampled_total: int,
    questions: list[dict],
    practice_summary: dict | None = None,
    cross_insights: list[dict] | None = None,
    recommended_exercises: list[dict] | None = None,
) -> dict:
    clusters = [dict(cluster) for cluster in (llm.get("clusters") or [])]
    _cluster_samples(clusters, {item["id"]: item["question"] for item in questions})
    result = {
        "trend": stats["trend"],
        "top_questions": stats["top_questions"],
        "top_keywords": stats["top_keywords"],
        "unique_questions": stats["unique_questions"],
        "clusters": clusters,
        "summary": llm.get("summary"),
        "llm_status": llm.get("llm_status") or "degraded",
        "sampled": sampled,
        "sampled_total": sampled_total,
    }
    if stats.get("stats_truncated"):
        # 文本侧统计超过取样上限：高频问/关键词基于最近 2 万条提问计算，
        # 教师需要能分辨口径（question_count 仍是精确计数）。
        result["stats_truncated"] = True
    if practice_summary is not None:
        result["practice_summary"] = practice_summary
    if cross_insights is not None:
        result["cross_insights"] = cross_insights
    if recommended_exercises is not None:
        result["recommended_exercises"] = recommended_exercises
    return result


async def create_report(
    session: AsyncSession,
    *,
    created_by: str,
    range_start: datetime,
    range_end: datetime,
) -> CourseQuestionReport:
    """创建报告记录并调度后台生成。返回 running 状态的报告。"""
    if range_start >= range_end:
        raise ValueError("时间窗起点必须早于终点")
    if _window_span_days(range_start, range_end) > _MAX_WINDOW_DAYS:
        raise ValueError(f"时间窗跨度不能超过 {_MAX_WINDOW_DAYS} 天")

    import time

    now_mono = time.monotonic()
    last = _USER_LAST_CREATE.get(created_by)
    if last is not None and now_mono - last < _USER_CREATE_COOLDOWN_SECONDS:
        wait_seconds = int(_USER_CREATE_COOLDOWN_SECONDS - (now_mono - last))
        raise ReportCreationCooldown(
            f"创建过于频繁，请 {wait_seconds} 秒后再试",
            retry_after_seconds=wait_seconds,
        )

    async with _creation_guard():
        # 先回收卡死的 running 行（进程重启遗留），否则单飞检查会永久 409。
        stale_cutoff = datetime.now() - timedelta(minutes=_STALE_RUNNING_MINUTES)
        stale_rows = (
            await session.execute(
                select(CourseQuestionReport).where(
                    CourseQuestionReport.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
                    CourseQuestionReport.status == _REPORT_STATUS_RUNNING,
                    CourseQuestionReport.created_at < stale_cutoff,
                )
            )
        ).scalars().all()
        for row in stale_rows:
            row.status = _REPORT_STATUS_FAILED
            row.error = "生成超时（进程可能重启），已自动回收"
            row.finished_at = datetime.now()
        if stale_rows:
            await session.commit()

        running = await session.scalar(
            select(CourseQuestionReport)
            .where(
                CourseQuestionReport.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
                CourseQuestionReport.status == _REPORT_STATUS_RUNNING,
            )
            .limit(1)
        )
        if running is not None:
            raise RuntimeError("已有分析任务正在生成，请稍后再试")

        report = CourseQuestionReport(
            id=str(uuid.uuid4()),
            agent_code=TEACHER_ANALYTICS_AGENT_CODE,
            created_by=created_by,
            range_start=range_start,
            range_end=range_end,
            question_count=0,
            status=_REPORT_STATUS_RUNNING,
        )
        session.add(report)
        await session.commit()

    _USER_LAST_CREATE[created_by] = now_mono
    _spawn_background(_run_generation(report.id))
    return report


_REPORT_LIST_LIMIT = 100
# 列表不需要 result_json（可能数 MB/行），显式投影排除该列，避免整学期报告
# 全量加载进内存再丢弃。
_REPORT_LIST_COLUMNS = (
    CourseQuestionReport.id,
    CourseQuestionReport.agent_code,
    CourseQuestionReport.created_by,
    CourseQuestionReport.range_start,
    CourseQuestionReport.range_end,
    CourseQuestionReport.question_count,
    CourseQuestionReport.status,
    CourseQuestionReport.model_name,
    CourseQuestionReport.error,
    CourseQuestionReport.created_at,
    CourseQuestionReport.finished_at,
)


async def list_reports(session: AsyncSession) -> list:
    rows = await session.execute(
        select(*_REPORT_LIST_COLUMNS)
        .where(CourseQuestionReport.agent_code == TEACHER_ANALYTICS_AGENT_CODE)
        .order_by(CourseQuestionReport.created_at.desc())
        .limit(_REPORT_LIST_LIMIT)
    )
    return list(rows.all())


async def get_report(session: AsyncSession, report_id: str) -> CourseQuestionReport | None:
    report = await session.get(CourseQuestionReport, report_id)
    if report is None or report.agent_code != TEACHER_ANALYTICS_AGENT_CODE:
        return None
    return report


async def delete_report(session: AsyncSession, report_id: str) -> bool:
    report = await get_report(session, report_id)
    if report is None:
        return False
    await session.delete(report)
    await session.commit()
    return True
