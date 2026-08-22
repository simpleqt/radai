"""提问旁路采集与确定性统计。

采集只在提问入口 fire-and-forget 调用，异常不向调用方传播，也不阻塞回答流。
只采集 ``agent_code=fmca`` 的学生提问；teacher/admin 的试用提问不入库。
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.loggers import get_logger
from backend.db.models import CourseQuestionRecord, User

logger = get_logger(__name__)

# 采集任务的强引用集合：事件循环对任务只持弱引用，不持引用会被 GC 中途丢弃，
# 表现为随机漏采（keywords 路由的 _track_keyword_task 同款纪律）。
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

# 教师分析能力仅属于基础力学课程智能体。
TEACHER_ANALYTICS_AGENT_CODE = "fmca"

# 提问分析角色门禁：teacher/admin 的试用提问不进入学情数据。
_EXCLUDED_ROLES = {"teacher", "admin"}

_MAX_QUESTION_CHARS = 2000

# 文本侧统计的取样上限（趋势是 SQL 聚合不受影响）：窗口提问量再大，
# 大屏/报告的高频问与关键词最多按最近这么多数量的提问计算。
_MAX_STATS_ROWS = 20000

# 停用词：与 RAG 关键词统计语义一致的常见中文虚词/符号词。
_STOPWORDS = {
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有",
    "看", "好", "自己", "这", "那", "什么", "为什么", "怎么", "如何", "多少",
    "哪个", "哪些", "请问", "一下", "关于", "吗", "呢", "吧", "啊", "哦", "呀",
    "这个", "那个", "如果", "或者", "但是", "因为", "所以", "还是", "可以",
    "应该", "需要", "知道", "请", "帮", "求", "解释", "讲解", "分析", "讲解一下",
}

# 单字虚词/疑问/代词/副词/助词：RAG 分词器为检索召回会并入中文 bigram，其中
# 混入「么是/是合/何分/力与」这类功能字噪声。二字中文 token 只要含任一功能字即
# 视为噪声跳过；力/分/解/合等实义字不在此列，故不会误伤「分力/分解/合力」。
_FUNCTION_CHARS = frozenset(
    "的了是在我有和与或就都也又很太更最不没别上下这那哪什"
    "么怎样何为因所以但而且还要去来说看会能可应该需知道请帮求"
    "让给对从向把被用之其者于吗呢吧啊哦呀啦哟我你他她它们咱谁几多少每各本某此该另"
)


def should_collect_question(agent_code: str, role: str) -> bool:
    """是否采集该条提问（fmca 专属 + 排除教师/管理员）。"""
    normalized_agent = str(agent_code or "").strip()
    normalized_role = str(role or "user").strip().lower()
    if normalized_agent != TEACHER_ANALYTICS_AGENT_CODE:
        return False
    if normalized_role in _EXCLUDED_ROLES:
        return False
    return True


def _normalize_question_text(text: str) -> str:
    """归一问句用于精确去重与高频统计（去空白/全半角/标点）。"""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.lower()
    normalized = re.sub(r"[\s\u3000]+", "", normalized)
    normalized = re.sub(r"[\u0021-\u002f\u003a-\u0040\u005b-\u0060\u007b-\u007e\u3000-\u303f\uff00-\uffef\u2018\u2019\u201c\u201d\u2026]", "", normalized)
    return normalized


def tokenize_question(text: str) -> list[str]:
    """切词（同步兜底版）：复用 RAG 分词器的 off-loop 调用见 ``tokenize_batch``。

    该函数仅作为单条兜底，真实统计走 ``tokenize_batch`` 批量分词，避免逐条跨
    executor 排队。
    """
    from backend.core.text.tokenization import tokenize_mixed_text

    return _filter_stopwords(tokenize_mixed_text(text, require_hanlp=False))


def _filter_stopwords(tokens: list[str]) -> list[str]:
    result: list[str] = []
    for token in tokens:
        normalized = str(token or "").strip()
        if not normalized or normalized in _STOPWORDS or len(normalized) < 2:
            continue
        if (
            len(normalized) == 2
            and all("\u4e00" <= ch <= "\u9fff" for ch in normalized)
            and any(ch in _FUNCTION_CHARS for ch in normalized)
        ):
            continue
        result.append(normalized)
    return result


async def tokenize_batch(texts: list[str]) -> list[list[str]]:
    """批量切词：走 RAG 专用分词 executor（HanLP 若已加载则用，否则正则/bigram 兜底）。"""
    from backend.core.text.tokenization import run_tokenization, tokenize_mixed_texts

    try:
        results = await run_tokenization(
            tokenize_mixed_texts,
            texts,
            require_hanlp=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TeacherAnalytics] 批量分词失败，回退单条分词: %s", exc)
        return [tokenize_question(text) for text in texts]
    return [_filter_stopwords(list(tokens)) for tokens in results]


async def record_question(
    *,
    question: str,
    user_id: str,
    agent_code: str,
    role: str,
    thread_id: str | None,
    workflow: str | None,
    model_name: str | None,
    is_guest: bool = False,
) -> None:
    """异步落一条提问快照。使用独立会话，失败仅记日志。"""
    if not should_collect_question(agent_code, role):
        return

    raw_question = str(question or "")
    if not raw_question.strip():
        return

    from backend.db.connection import async_session_context

    stored_question = raw_question[:_MAX_QUESTION_CHARS]
    try:
        async with async_session_context() as session:
            username = ""
            if user_id and user_id not in {"guest"} and not user_id.startswith("guest_"):
                # 学生姓名不匿名：优先 display_name，否则 username。
                result = await session.execute(
                    select(User.display_name, User.username).where(
                        User.user_id == user_id
                    )
                )
                row = result.first()
                if row is not None:
                    username = str(row[0] or row[1] or "")
            session.add(
                CourseQuestionRecord(
                    user_id=user_id,
                    username=username[:50],
                    agent_code=TEACHER_ANALYTICS_AGENT_CODE,
                    thread_id=str(thread_id or "")[:100],
                    question=stored_question,
                    question_len=len(raw_question),
                    workflow=(str(workflow or "")[:32] or None),
                    model_name=(str(model_name or "")[:128] or None),
                    is_guest=bool(is_guest) or user_id.startswith("guest"),
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("提问采集失败（不影响对话）: %s", exc)


def schedule_question_collection(
    *,
    question: str,
    user_id: str,
    agent_code: str,
    role: str,
    thread_id: str | None,
    workflow: str | None,
    model_name: str | None,
    is_guest: bool = False,
) -> None:
    """在事件循环上旁路调度提问采集（fire-and-forget，失败不抛出）。

    采集门禁（fmca 专属 + 排除 teacher/admin）在 record_question 内统一生效。
    """
    if not should_collect_question(agent_code, role):
        return

    async def _guarded() -> None:
        try:
            await record_question(
                question=question,
                user_id=user_id,
                agent_code=agent_code,
                role=role,
                thread_id=thread_id,
                workflow=workflow,
                model_name=model_name,
                is_guest=is_guest,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("提问采集调度失败（不影响对话）: %s", exc)

    try:
        task = asyncio.get_running_loop().create_task(_guarded())
    except RuntimeError:
        # 无运行中的事件循环（如测试直连同步上下文）则静默跳过。
        return
    _BACKGROUND_TASKS.add(task)

    def _done(done: asyncio.Task[None]) -> None:
        _BACKGROUND_TASKS.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            logger.error(
                "提问采集后台任务异常退出",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(_done)


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，让 % 与 _ 按字面匹配。"""
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


async def question_page(
    session: AsyncSession,
    *,
    range_start: datetime,
    range_end: datetime,
    keyword: str | None = None,
    user_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[CourseQuestionRecord], int]:
    """教师视角的提问分页列表（仅 fmca，时间倒序）。"""
    stmt = select(CourseQuestionRecord).where(
        CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
        CourseQuestionRecord.created_at >= range_start,
        CourseQuestionRecord.created_at < range_end,
    )
    if keyword:
        stmt = stmt.where(
            CourseQuestionRecord.question.like(
                f"%{_escape_like(keyword)}%", escape="\\"
            )
        )
    if user_id:
        stmt = stmt.where(CourseQuestionRecord.user_id == user_id)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        await session.execute(
            stmt.order_by(CourseQuestionRecord.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows), int(total)


async def overview_stats(
    session: AsyncSession,
) -> dict:
    """轻量实时统计：总量 / 今日 / 近 7 日 / 近 30 日 / 活跃学生数。"""
    base = select(func.count()).select_from(CourseQuestionRecord).where(
        CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE
    )
    now = datetime.now()

    async def _count_where(*clauses):
        stmt = select(func.count()).select_from(CourseQuestionRecord).where(
            CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
            *clauses,
        )
        return int(await session.scalar(stmt) or 0)

    total = int(await session.scalar(base) or 0)
    today_start = datetime(now.year, now.month, now.day)
    today = await _count_where(CourseQuestionRecord.created_at >= today_start)
    seven_start = today_start - timedelta(days=6)
    last_7 = await _count_where(CourseQuestionRecord.created_at >= seven_start)
    thirty_start = today_start - timedelta(days=29)
    last_30 = await _count_where(CourseQuestionRecord.created_at >= thirty_start)

    active_students = (
        await session.scalar(
            select(func.count(func.distinct(CourseQuestionRecord.user_id)))
            .where(
                CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
                CourseQuestionRecord.created_at >= thirty_start,
            )
        )
        or 0
    )
    return {
        "agent_code": TEACHER_ANALYTICS_AGENT_CODE,
        "total": total,
        "today": today,
        "last_7_days": last_7,
        "last_30_days": last_30,
        "active_students_30_days": int(active_students),
    }


async def daily_trend(
    session: AsyncSession,
    *,
    range_start: datetime,
    range_end: datetime,
) -> list[dict]:
    """按天聚合提问量（SQL DATE 分组）。"""
    rows = await session.execute(
        select(
            func.date(CourseQuestionRecord.created_at).label("day"),
            func.count().label("cnt"),
        )
        .where(
            CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
            CourseQuestionRecord.created_at >= range_start,
            CourseQuestionRecord.created_at < range_end,
        )
        .group_by(func.date(CourseQuestionRecord.created_at))
        .order_by(func.date(CourseQuestionRecord.created_at))
    )
    return [
        {"date": str(row[0]), "count": int(row[1])}
        for row in rows.all()
    ]


async def fetch_questions(
    session: AsyncSession,
    *,
    range_start: datetime,
    range_end: datetime,
    limit: int,
) -> list[dict]:
    """取窗口内问题清单（用于 LLM 聚类），按时间升序。"""
    rows = (
        await session.execute(
            select(CourseQuestionRecord.id, CourseQuestionRecord.question)
            .where(
                CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
                CourseQuestionRecord.created_at >= range_start,
                CourseQuestionRecord.created_at < range_end,
            )
            .order_by(CourseQuestionRecord.created_at.asc())
            .limit(limit)
        )
    ).all()
    return [{"id": str(row[0]), "question": str(row[1] or "")} for row in rows]


async def fetch_question_ids(
    session: AsyncSession,
    *,
    range_start: datetime,
    range_end: datetime,
) -> list[str]:
    """取窗口内全部问题 id（按时间升序）；仅 id 列，供均匀抽样使用。"""
    rows = (
        await session.execute(
            select(CourseQuestionRecord.id).where(
                CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
                CourseQuestionRecord.created_at >= range_start,
                CourseQuestionRecord.created_at < range_end,
            ).order_by(CourseQuestionRecord.created_at.asc())
        )
    ).scalars().all()
    return [str(value) for value in rows]


async def fetch_questions_by_ids(
    session: AsyncSession, ids: list[str]
) -> list[dict]:
    """按抽样后的 id 集合取问题清单（保持传入 id 的顺序无关，按时间升序返回）。"""
    if not ids:
        return []
    rows = (
        await session.execute(
            select(CourseQuestionRecord.id, CourseQuestionRecord.question)
            .where(
                CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
                CourseQuestionRecord.id.in_(ids),
            )
            .order_by(CourseQuestionRecord.created_at.asc())
        )
    ).all()
    return [{"id": str(row[0]), "question": str(row[1] or "")} for row in rows]


async def deterministic_stats(
    session: AsyncSession,
    *,
    range_start: datetime,
    range_end: datetime,
    top_n: int = 20,
) -> dict:
    """确定性统计：趋势 + 归一化高频 + 关键词词频（无模型，永远可用）。

    趋势走 SQL 按天分组（精确）；文本侧（高频问/关键词）按时间倒序取样，
    上限 ``_MAX_STATS_ROWS`` 防止窗口增长后每次大屏全量拉行，超限时返回
    ``stats_truncated`` 标记、``question_count`` 仍为精确计数。
    """
    count_stmt = (
        select(func.count())
        .select_from(CourseQuestionRecord)
        .where(
            CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
            CourseQuestionRecord.created_at >= range_start,
            CourseQuestionRecord.created_at < range_end,
        )
    )
    question_count = int(await session.scalar(count_stmt) or 0)
    rows = (
        await session.execute(
            select(
                CourseQuestionRecord.id,
                CourseQuestionRecord.question,
            )
            .where(
                CourseQuestionRecord.agent_code == TEACHER_ANALYTICS_AGENT_CODE,
                CourseQuestionRecord.created_at >= range_start,
                CourseQuestionRecord.created_at < range_end,
            )
            .order_by(CourseQuestionRecord.created_at.desc())
            .limit(_MAX_STATS_ROWS)
        )
    ).all()

    normalized_counter: Counter[str] = Counter()
    keyword_counter: Counter[str] = Counter()
    question_texts: list[str] = []
    for row in rows:
        question_text = row[1]
        question_texts.append(question_text)
        normalized = _normalize_question_text(question_text)
        if normalized:
            normalized_counter[normalized] += 1

    for tokens in await tokenize_batch(question_texts):
        for token in tokens:
            keyword_counter[token] += 1

    trend = await daily_trend(session, range_start=range_start, range_end=range_end)

    top_questions = [
        {"question": q, "count": c}
        for q, c in normalized_counter.most_common(top_n)
    ]
    top_keywords = [
        {"keyword": k, "count": c}
        for k, c in keyword_counter.most_common(50)
    ]
    return {
        "trend": trend,
        "top_questions": top_questions,
        "top_keywords": top_keywords,
        "question_count": question_count,
        "unique_questions": len(normalized_counter),
        "stats_truncated": question_count > len(rows),
    }
