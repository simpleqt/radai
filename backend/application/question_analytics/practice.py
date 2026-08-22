"""教学分析融合：练习维度确定性统计 + 提问×做题交叉洞察 + 按薄弱点抽题。

数据源是 ``question_bank_attempts`` join ``question_bank_questions`` 的标注
（知识点/难度/章节不冗余存储，聚合时实时 join，题目编辑后按最新标注重算）。
全部为确定性 SQL/Python 统计，无 LLM——与 ``collector.deterministic_stats``
同一纪律：先进大屏，报告生成也复用同一份数字。

交叉洞察的三类判定（样本量门槛防小样本误读）：
- ``asked_and_wrong``：提问中提到该知识点 且 错题率达标 → 共性薄弱点，优先讲解；
- ``asked_only``：提到但练习表现尚可/无练习 → 概念澄清型提问，讲概念即可；
- ``wrong_only``：没人问但错得多 → 沉默的薄弱点，最易被纯提问分析漏掉。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.loggers import get_logger
from backend.db.models import (
    CourseQuestionRecord,
    QuestionBankAttempt,
    QuestionBankQuestion,
)

logger = get_logger(__name__)

# 交叉洞察的样本量与错误率门槛：低于门槛的知识点不进薄弱点结论。
_MIN_ATTEMPTS_FOR_WEAK = 3
_WEAK_ERROR_RATE = 0.4
_MAX_KPS = 20
_MAX_RECOMMENDED_PER_KP = 5
_DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2}


def _rate(numerator: int, denominator: int, digits: int = 4) -> float | None:
    """对外展示的比率一律 ROUND_HALF_UP（与 course_eval stats_engine 同纪律），
    不用 Python round（银行家舍入会让 0.125 显示成 0.12）。"""
    if not denominator:
        return None
    quantum = Decimal(1).scaleb(-digits)
    value = (Decimal(int(numerator)) / Decimal(int(denominator))).quantize(
        quantum, rounding=ROUND_HALF_UP
    )
    return float(value)


async def practice_deterministic_stats(
    session: AsyncSession,
    *,
    range_start: datetime,
    range_end: datetime,
    top_n: int = 10,
) -> dict:
    """练习维度确定性统计（窗口内），题库为空时返回 attempt_count=0 的占位。

    - ``mode='browse'``（解答模式浏览）不计入做题统计，单独计 ``browse_count``；
    - 聚合不取题目 stem（Text 大列），高频错题的题干摘要二次按 id 补取。
    """
    rows = (
        await session.execute(
            select(
                QuestionBankAttempt.user_id,        # 0
                QuestionBankAttempt.question_id,    # 1
                QuestionBankAttempt.is_correct,     # 2
                QuestionBankAttempt.created_at,     # 3
                QuestionBankQuestion.question_type,  # 4
                QuestionBankQuestion.difficulty,    # 5
                QuestionBankQuestion.knowledge_points_json,  # 6
                QuestionBankQuestion.id,            # 7
            )
            .join(
                QuestionBankQuestion,
                QuestionBankQuestion.id == QuestionBankAttempt.question_id,
            )
            .where(
                QuestionBankAttempt.created_at >= range_start,
                QuestionBankAttempt.created_at < range_end,
                # browse 是「看过答案」信号，不是做题：正确率/错误率分母不含它。
                QuestionBankAttempt.mode != "browse",
            )
        )
    ).all()
    browse_count = int(
        await session.scalar(
            select(func.count())
            .select_from(QuestionBankAttempt)
            .where(
                QuestionBankAttempt.created_at >= range_start,
                QuestionBankAttempt.created_at < range_end,
                QuestionBankAttempt.mode == "browse",
            )
        )
        or 0
    )

    attempt_count = len(rows)
    # is_correct 是 Integer 列（0/1，None=未判定）：真值判断，禁止 `is True/False`。
    graded = [row for row in rows if row[2] is not None]
    correct = sum(1 for row in graded if row[2])
    correct_rate = _rate(correct, len(graded))
    daily_attempts: Counter[str] = Counter()
    daily_correct: Counter[str] = Counter()
    kp_attempts: Counter[str] = Counter()
    kp_wrong: Counter[str] = Counter()
    difficulty_attempts: Counter[str] = Counter()
    difficulty_wrong: Counter[str] = Counter()
    question_wrong: Counter[str] = Counter()
    question_type_by_id: dict[str, str] = {}
    students_seen: set[str] = set()

    for row in rows:
        day = str(row[3].date()) if row[3] else ""
        daily_attempts[day] += 1
        if row[2] is not None and row[2]:
            daily_correct[day] += 1
        knowledge_points = row[6] or []
        for kp in knowledge_points:
            kp_attempts[str(kp)] += 1
            if row[2] is not None and not row[2]:
                kp_wrong[str(kp)] += 1
        difficulty = row[5] or "unknown"
        difficulty_attempts[difficulty] += 1
        if row[2] is not None and not row[2]:
            difficulty_wrong[difficulty] += 1
        if row[2] is not None and not row[2]:
            question_wrong[str(row[7])] += 1
        question_type_by_id[str(row[7])] = str(row[4] or "")
        students_seen.add(str(row[0]))

    days = sorted(daily_attempts.keys())
    trend = [
        {
            "date": day,
            "attempts": daily_attempts[day],
            "correct": daily_correct[day],
        }
        for day in days
    ]
    by_knowledge_point = [
        {
            "knowledge_point": kp,
            "attempts": kp_attempts[kp],
            "wrong": kp_wrong.get(kp, 0),
            "error_rate": _rate(kp_wrong.get(kp, 0), kp_attempts[kp]),
        }
        for kp, _count in kp_attempts.most_common(_MAX_KPS)
    ]
    by_difficulty = [
        {
            "difficulty": difficulty,
            "attempts": difficulty_attempts[difficulty],
            "wrong": difficulty_wrong.get(difficulty, 0),
            "error_rate": _rate(
                difficulty_wrong.get(difficulty, 0), difficulty_attempts[difficulty]
            ),
        }
        for difficulty in sorted(
            difficulty_attempts, key=lambda name: _DIFFICULTY_ORDER.get(name, 9)
        )
    ]
    top_wrong_ids = [question_id for question_id, _count in question_wrong.most_common(top_n)]
    stem_by_id: dict[str, str] = {}
    difficulty_by_id: dict[str, str | None] = {}
    kp_by_id: dict[str, list] = {}
    if top_wrong_ids:
        meta_rows = (
            await session.execute(
                select(
                    QuestionBankQuestion.id,
                    QuestionBankQuestion.stem,
                    QuestionBankQuestion.difficulty,
                    QuestionBankQuestion.knowledge_points_json,
                ).where(QuestionBankQuestion.id.in_(top_wrong_ids))
            )
        ).all()
        for row_id, stem, difficulty, knowledge_points in meta_rows:
            stem_by_id[str(row_id)] = str(stem or "")[:120]
            difficulty_by_id[str(row_id)] = difficulty
            kp_by_id[str(row_id)] = [str(kp) for kp in (knowledge_points or [])]
    top_wrong_questions = [
        {
            "question_id": question_id,
            "stem": stem_by_id.get(question_id, ""),
            "knowledge_points": kp_by_id.get(question_id, []),
            "difficulty": difficulty_by_id.get(question_id),
            "question_type": question_type_by_id.get(question_id, ""),
            "wrong_count": question_wrong[question_id],
        }
        for question_id in top_wrong_ids
    ]
    return {
        "attempt_count": attempt_count,
        "graded_count": len(graded),
        "correct_rate": correct_rate,
        "browse_count": browse_count,
        "active_students": len(students_seen),
        "trend": trend,
        "by_knowledge_point": by_knowledge_point,
        "by_difficulty": by_difficulty,
        "top_wrong_questions": top_wrong_questions,
        "empty": attempt_count == 0,
    }


async def _window_question_texts(
    session: AsyncSession,
    *,
    range_start: datetime,
    range_end: datetime,
    limit: int = 5000,
) -> list[str]:
    rows = (
        await session.execute(
            select(CourseQuestionRecord.question)
            .where(
                CourseQuestionRecord.agent_code == "fmca",
                CourseQuestionRecord.created_at >= range_start,
                CourseQuestionRecord.created_at < range_end,
            )
            .order_by(CourseQuestionRecord.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return [str(text or "") for text in rows]


def _mention_counts(knowledge_points: list[str], question_texts: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kp in knowledge_points:
        counts[kp] = sum(1 for text in question_texts if kp and kp in text)
    return counts


def cross_insights(
    practice_stats: dict,
    *,
    question_texts: list[str],
) -> list[dict]:
    """提问×做题交叉：给每个有练习数据的知识点分类，附提问佐证量。"""
    by_kp = practice_stats.get("by_knowledge_point") or []
    if not by_kp:
        return []
    names = [item["knowledge_point"] for item in by_kp]
    mentions = _mention_counts(names, question_texts)
    result: list[dict] = []
    for item in by_kp:
        attempts = item["attempts"]
        error_rate = item["error_rate"]
        mention_count = mentions.get(item["knowledge_point"], 0)
        weak = attempts >= _MIN_ATTEMPTS_FOR_WEAK and (error_rate or 0) >= _WEAK_ERROR_RATE
        if weak and mention_count > 0:
            category = "asked_and_wrong"
        elif weak:
            category = "wrong_only"
        elif mention_count > 0:
            category = "asked_only"
        else:
            continue
        result.append(
            {
                "knowledge_point": item["knowledge_point"],
                "category": category,
                "attempts": attempts,
                "error_rate": error_rate,
                "question_mentions": mention_count,
            }
        )
    order = {"asked_and_wrong": 0, "wrong_only": 1, "asked_only": 2}
    result.sort(key=lambda item: (order[item["category"]], -(item["error_rate"] or 0)))
    return result[:_MAX_KPS]


async def recommended_exercises(
    session: AsyncSession,
    *,
    weak_knowledge_points: list[str],
    per_kp: int = _MAX_RECOMMENDED_PER_KP,
) -> list[dict]:
    """按薄弱知识点从已发布题库抽题（难度递进），供报告「建议讲解+配套习题」。"""
    if not weak_knowledge_points:
        return []
    rows = (
        await session.execute(
            select(QuestionBankQuestion)
            .where(
                QuestionBankQuestion.status == "published",
                QuestionBankQuestion.knowledge_points_json.isnot(None),
            )
            # 稳定排序：无 ORDER BY 的 limit 让不同次报告对同一知识点推荐不同题。
            .order_by(
                QuestionBankQuestion.created_at.desc(),
                QuestionBankQuestion.id.desc(),
            )
            .limit(2000)
        )
    ).scalars().all()
    by_kp: dict[str, list[QuestionBankQuestion]] = defaultdict(list)
    for question in rows:
        for kp in question.knowledge_points_json or []:
            if str(kp) in weak_knowledge_points:
                by_kp[str(kp)].append(question)
    result: list[dict] = []
    for kp in weak_knowledge_points:
        candidates = sorted(
            by_kp.get(kp, []),
            key=lambda question: _DIFFICULTY_ORDER.get(question.difficulty or "medium", 1),
        )
        if not candidates:
            continue
        result.append(
            {
                "knowledge_point": kp,
                "questions": [
                    {
                        "id": question.id,
                        "question_type": question.question_type,
                        "difficulty": question.difficulty,
                        "stem": str(question.stem or "")[:120],
                    }
                    for question in candidates[:per_kp]
                ],
            }
        )
    return result


async def practice_overview(
    session: AsyncSession,
    *,
    range_start: datetime,
    range_end: datetime,
) -> dict:
    """教师端班级练习总览（insights/报告共用）。"""
    stats = await practice_deterministic_stats(
        session, range_start=range_start, range_end=range_end
    )
    question_texts = await _window_question_texts(
        session, range_start=range_start, range_end=range_end
    )
    insights = cross_insights(stats, question_texts=question_texts)
    weak = [item["knowledge_point"] for item in insights if item["category"] != "asked_only"]
    return {
        "stats": stats,
        "cross_insights": insights,
        "recommended": await recommended_exercises(session, weak_knowledge_points=weak),
    }
