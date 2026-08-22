"""学生练习服务：题目脱敏下发、服务端判分、做题记录与错题本。

脱敏纪律（与 custom_learning ``_QUIZ_PRIVATE_FIELDS`` 同源教训）：
- 列表/详情默认**不带** answer/analysis，否则浏览器 F12 即抄答案；
- 判分提交后（``check_answer``）或显式解答模式（``with_solution``）才返回；
- 判分依据只保留在服务端，attempts 只存作答内容与判定结果。

判分规则 v1（纯规则，无 LLM）：
- 选择题：选项键精确匹配（忽略大小写/空白）；
- 判断题：对/错语义归一（对/正确/√/true/T vs 错/错误/×/false/F）；
- 填空题：NFKC 归一 + 空白折叠后精确匹配；可解析为数字时按数值相等（容差 1e-6）；
- 计算/作图/简答：不自判，is_correct=None，返回参考答案与解析供学生自评。
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.question_bank import images as qb_images
from backend.application.question_bank.service import question_to_dict
from backend.config.loggers import get_logger
from backend.db.models import QuestionBankAttempt, QuestionBankQuestion

logger = get_logger(__name__)

_SUBJECTIVE_TYPES = {"计算题", "作图题", "简答题"}
_MAX_ATTEMPT_ANSWER_CHARS = 4000
# 解答模式浏览的去重窗口：同 (user, question) 一小时内重复刷新只留一行。
_BROWSE_DEDUPE_SECONDS = 3600

_TRUE_TOKENS = {"对", "正确", "√", "T", "TRUE", "是", "Y", "YES"}
_FALSE_TOKENS = {"错", "错误", "×", "x", "F", "FALSE", "否", "N", "NO"}


def _normalize_text(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _parse_number(value: str):
    text = _normalize_text(value)
    try:
        return float(text.rstrip("%"))
    except ValueError:
        return None


def _grade_choice(expected: str, given: str) -> bool | None:
    expected_key = _normalize_text(expected).upper()
    given_key = _normalize_text(given).upper()
    if not expected_key or not given_key:
        return None
    return expected_key == given_key


def _grade_true_false(expected: str, given: str) -> bool | None:
    def _token(text: str) -> bool | None:
        cleaned = _normalize_text(text).upper()
        if not cleaned:
            return None
        if cleaned in _TRUE_TOKENS:
            return True
        if cleaned in _FALSE_TOKENS:
            return False
        if cleaned in {"A", "B"}:
            return cleaned == "A"
        return None

    expected_token = _token(expected)
    given_token = _token(given)
    if expected_token is None or given_token is None:
        return None
    return expected_token == given_token


def _grade_fill(expected: str, given: str) -> bool | None:
    expected_text = _normalize_text(expected)
    given_text = _normalize_text(given)
    if not expected_text or not given_text:
        return None
    if expected_text == given_text:
        return True
    expected_number = _parse_number(expected_text)
    given_number = _parse_number(given_text)
    if expected_number is not None and given_number is not None:
        return abs(expected_number - given_number) < 1e-6
    return False


def grade_answer(question: QuestionBankQuestion, given) -> bool | None:
    """判分：返回 True/False，主观题为 None（自评）。无法判定的客观题也返回 None。"""
    expected = str(question.answer or "").strip()
    if not expected:
        return None
    given_text = given if isinstance(given, str) else None
    if given_text is None:
        if isinstance(given, (int, float)):
            given_text = str(given)
        else:
            return None
    if question.question_type == "选择题":
        return _grade_choice(expected, given_text)
    if question.question_type == "判断题":
        return _grade_true_false(expected, given_text)
    if question.question_type == "填空题":
        return _grade_fill(expected, given_text)
    return None


def public_question(question: QuestionBankQuestion) -> dict:
    """学生侧题目下发：不含 answer/analysis（判分后/解答模式才返回）。"""
    return question_to_dict(question, with_solution=False)


async def practice_question_page(
    session: AsyncSession,
    *,
    question_type: str | None = None,
    difficulty: str | None = None,
    knowledge_point: str | None = None,
    chapter: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    from backend.application.question_bank.service import question_page

    rows, total = await question_page(
        session,
        question_type=question_type,
        difficulty=difficulty,
        knowledge_point=knowledge_point,
        chapter=chapter,
        status="published",
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    return [public_question(row) for row in rows], total


async def check_answer(
    session: AsyncSession,
    *,
    question: QuestionBankQuestion,
    user_id: str,
    username: str,
    given,
    mode: str = "practice",
    self_review: bool | None = None,
) -> dict:
    """判分并落一条做题记录。

    ``self_review`` 是主观题（计算/作图/简答）对照参考答案后的自评；
    客观题判分结果不为 None 时忽略自评（服务端判分优先）。
    """
    is_correct = grade_answer(question, given)
    if is_correct is None and self_review is not None:
        is_correct = bool(self_review)
    given_stored = given if isinstance(given, str) else str(given or "")
    session.add(
        QuestionBankAttempt(
            id=str(uuid.uuid4()),
            user_id=str(user_id)[:64],
            username=str(username or "")[:50],
            question_id=question.id,
            answer_json={"answer": given_stored[:_MAX_ATTEMPT_ANSWER_CHARS]},
            is_correct=is_correct,
            mode=mode if mode in {"practice", "browse"} else "practice",
        )
    )
    await session.commit()
    return {
        "question_id": question.id,
        "question_type": question.question_type,
        "is_self_review": question.question_type in _SUBJECTIVE_TYPES,
        "is_correct": is_correct,
        "answer": question.answer,
        "analysis": question.analysis,
        "checked_at": datetime.now().isoformat(),
    }


async def record_browse_attempt(
    session: AsyncSession,
    *,
    question_id: str,
    user_id: str,
    username: str,
) -> None:
    """解答模式浏览记录：mode="browse"、is_correct=None。

    只作教师分析信号（区分看过答案的学生）；错题本与正确率统计一律排除
    browse 行（``mode != 'browse'``），避免一次浏览把「最近一次作答为错」
    的判定冲掉。同一 (user, question) 的一小时内浏览去重——学生反复刷新
    解答模式不再每次落一行。
    """
    from datetime import timedelta

    recent_cutoff = datetime.now() - timedelta(seconds=_BROWSE_DEDUPE_SECONDS)
    recent = await session.scalar(
        select(QuestionBankAttempt.id)
        .where(
            QuestionBankAttempt.user_id == str(user_id)[:64],
            QuestionBankAttempt.question_id == question_id,
            QuestionBankAttempt.mode == "browse",
            QuestionBankAttempt.created_at >= recent_cutoff,
        )
        .limit(1)
    )
    if recent is not None:
        return
    session.add(
        QuestionBankAttempt(
            id=str(uuid.uuid4()),
            user_id=str(user_id)[:64],
            username=str(username or "")[:50],
            question_id=question_id,
            answer_json={"answer": "", "browse": True},
            is_correct=None,
            mode="browse",
        )
    )
    await session.commit()


async def list_my_attempts(
    session: AsyncSession,
    *,
    user_id: str,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    # browse 是浏览信号不是做题记录：学生侧列表只展示判分/作答行。
    base = select(QuestionBankAttempt).where(
        QuestionBankAttempt.user_id == user_id,
        QuestionBankAttempt.mode != "browse",
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = (
        await session.execute(
            base.order_by(QuestionBankAttempt.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return [_attempt_item(row) for row in rows], int(total)


async def list_wrong_questions(
    session: AsyncSession,
    *,
    user_id: str,
    limit: int = 50,
) -> tuple[list[dict], int]:
    """错题本：每题取最近一次**判分**作答（SQL 侧窗口函数，不受总量截断）。

    只统计 ``mode='practice'`` 且 ``is_correct`` 非 NULL 的作答——浏览记录
    （browse）与主观题自评缺口不算「最近一次判分」。返回 (items, total)。
    """
    ranked = (
        select(
            QuestionBankAttempt.question_id.label("question_id"),
            QuestionBankAttempt.is_correct.label("is_correct"),
            QuestionBankAttempt.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=QuestionBankAttempt.question_id,
                order_by=QuestionBankAttempt.created_at.desc(),
            )
            .label("rn"),
        )
        .where(
            QuestionBankAttempt.user_id == user_id,
            QuestionBankAttempt.mode != "browse",
            QuestionBankAttempt.is_correct.isnot(None),
        )
        .subquery()
    )
    latest_wrong = (
        select(
            ranked.c.question_id.label("question_id"),
            ranked.c.created_at.label("created_at"),
        )
        .where(ranked.c.rn == 1, ranked.c.is_correct.is_(False))
        .subquery()
    )
    total = (
        await session.scalar(select(func.count()).select_from(latest_wrong)) or 0
    )
    if not total:
        return [], 0
    rows = (
        await session.execute(
            select(latest_wrong.c.question_id, latest_wrong.c.created_at)
            .order_by(latest_wrong.c.created_at.desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return [], int(total)
    questions = (
        await session.execute(
            select(QuestionBankQuestion).where(
                QuestionBankQuestion.id.in_([row[0] for row in rows]),
                QuestionBankQuestion.status == "published",
            )
        )
    ).scalars().all()
    by_id = {question.id: question for question in questions}
    result: list[dict] = []
    for question_id, wrong_at in rows:
        question = by_id.get(question_id)
        if question is None:
            continue
        item = public_question(question)
        item["last_wrong_at"] = wrong_at.isoformat() if wrong_at else None
        result.append(item)
    return result, int(total)


def _attempt_item(row: QuestionBankAttempt) -> dict:
    answer_json = row.answer_json if isinstance(row.answer_json, dict) else {}
    return {
        "id": row.id,
        "question_id": row.question_id,
        "answer": str(answer_json.get("answer") or ""),
        "is_correct": None if row.is_correct is None else bool(row.is_correct),
        "mode": row.mode,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def build_image_urls(question: QuestionBankQuestion) -> list[str]:
    return [
        qb_images.build_qb_image_url(image.get("object_name") or "")
        for image in (question.images_json or [])
        if image.get("object_name")
    ]
