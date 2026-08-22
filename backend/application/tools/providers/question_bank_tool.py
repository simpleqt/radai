"""题库检索工具：按 ID 精确取题（讲解主路径）与关键词列举。

分工纪律（fmca prompt 同步声明）：
- 题目本体只来自本工具（结构化题库，图片入库时强绑定），**严禁从知识库检索
  题目**——知识库 multi_kb_search 只负责概念/公式/推导讲解支撑，这从链路上
  消除了「检索到的文本分块与试题图片不一致」的问题；
- ``question_id`` 命中即整题返回（题干/选项/答案/解析/题图），不经任何向量/
  关键词检索；学生说「再来几道类似的题」时用关键词列举并引导去练习模式做题。
"""

from __future__ import annotations

from typing import Annotated

from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT

from backend.application.agent_executors.image_url_registry import register_image_url
from backend.application.question_bank.images import build_qb_image_url
from backend.config.loggers import get_logger
from backend.core.agents.state import ThreadState

logger = get_logger(__name__)

_MAX_LIST_LIMIT = 5
_STEM_EXCERPT = 120


def _normalize_question_id(value: str | None) -> str:
    cleaned = str(value or "").strip()
    if cleaned[:3].upper() == "QB-":
        return cleaned[3:]
    return cleaned


async def _fetch_question_by_id(question_id: str):
    from backend.db.connection import async_session_context
    from backend.db.models import QuestionBankQuestion

    async with async_session_context() as session:
        return await session.get(QuestionBankQuestion, question_id)


async def _search_questions(
    *,
    keywords: str | None,
    knowledge_point: str | None,
    question_type: str | None,
    difficulty: str | None,
    limit: int,
):
    from sqlalchemy import select

    from backend.db.connection import async_session_context
    from backend.db.models import QuestionBankQuestion

    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    async with async_session_context() as session:
        stmt = select(QuestionBankQuestion).where(
            QuestionBankQuestion.status == "published"
        )
        if knowledge_point:
            stmt = stmt.where(
                QuestionBankQuestion.knowledge_points_json.like(
                    f"%{_escape(knowledge_point)}%", escape="\\"
                )
            )
        if question_type:
            stmt = stmt.where(QuestionBankQuestion.question_type == question_type)
        if difficulty:
            stmt = stmt.where(QuestionBankQuestion.difficulty == difficulty)
        if keywords:
            clause = f"%{_escape(keywords)}%"
            stmt = stmt.where(
                QuestionBankQuestion.stem.like(clause, escape="\\")
                | QuestionBankQuestion.analysis.like(clause, escape="\\")
            )
        rows = (
            await session.execute(
                stmt.order_by(QuestionBankQuestion.created_at.desc()).limit(limit)
            )
        ).scalars().all()
        return list(rows)


def _question_lines(question, *, with_solution: bool = True) -> list[str]:
    lines = [
        f"题型：{question.question_type}",
        f"难度：{question.difficulty or '未标注'}",
        f"知识点：{'、'.join(question.knowledge_points_json or []) or '未标注'}",
    ]
    if question.chapter:
        lines.append(f"章节：{question.chapter}")
    if question.source_name:
        lines.append(f"来源：{question.source_name}")
    lines.append("题干：")
    lines.append(str(question.stem or ""))
    options = question.options_json or []
    if options:
        lines.append("选项：")
        lines.extend(
            f"{option.get('key', '')}. {option.get('text', '')}" for option in options
        )
    if with_solution:
        if question.answer:
            lines.append(f"参考答案：{question.answer}")
        if question.analysis:
            lines.append("解析：")
            lines.append(str(question.analysis))
    return lines


def _register_images(question) -> list[str]:
    tokens: list[str] = []
    for index, image in enumerate(question.images_json or [], start=1):
        object_name = str(image.get("object_name") or "")
        if not object_name:
            continue
        caption = str(image.get("caption") or f"题图{index}")
        tokens.append(register_image_url(build_qb_image_url(object_name), caption))
    return tokens


@tool("search_question_bank", parse_docstring=True)
async def search_question_bank_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    question_id: str | None = None,
    keywords: str | None = None,
    knowledge_point: str | None = None,
    question_type: str | None = None,
    difficulty: str | None = None,
    limit: int = 5,
) -> Command:
    """Search the fundamental-mechanics question bank (structured, figure-bound).

    Use ``question_id`` (the ``QB-`` number, e.g. QB-abc123) to fetch one exact
    question with its figures for explanation — this is the primary path and
    never goes through knowledge-base retrieval. Use ``keywords`` only to list
    similar questions; direct the student to the sidebar「习题练习」mode for
    actually practising questions.

    Args:
        question_id: Exact question id (``QB-`` prefix optional) to fetch one
            full question (stem, options, answer, analysis, figures).
        keywords: Optional free-text keywords to list matching questions.
        knowledge_point: Optional knowledge-point filter (must match the
            teacher annotation, e.g. 平面力系的平衡).
        question_type: Optional type filter: 选择题/填空题/判断题/计算题/作图题/简答题.
        difficulty: Optional difficulty filter: easy/medium/hard.
        limit: Max questions to list (1-5).
    """
    try:
        limit = max(1, min(int(limit or 5), _MAX_LIST_LIMIT))
        if question_id:
            normalized = _normalize_question_id(question_id)
            question = await _fetch_question_by_id(normalized)
            if question is None or question.status != "published":
                return Command(
                    update={
                        "messages": [
                            ToolMessage(
                                content=(
                                    f"未找到试题 QB-{normalized}。题目可能已下线；"
                                    "请让学生提供完整题号，或改用 keywords 列举相似题。"
                                ),
                                tool_call_id=tool_call_id,
                            )
                        ]
                    }
                )
            tokens = _register_images(question)
            lines = [f"试题 QB-{question.id}（题库原题，含教师标注与题图）"]
            lines.extend(_question_lines(question))
            if tokens:
                lines.append("题图（原图，随题强绑定）：")
                lines.extend(f"![题图{index}]({token})" for index, token in enumerate(tokens, start=1))
            lines.append(
                "讲解要求：以上题目内容为确定性上下文，讲解时不要改写题干与数值；"
                "若上方列出题图，讲解开头必须先逐字引用第一行题图标记"
                "（以 ![题图1] 开头的那一行，不要省略、不要改写成其他链接），再逐步讲解；"
                "概念/公式/推导可再调用 multi_kb_search 引用知识库，但不要用知识库检索本题。"
            )
            return Command(
                update={
                    "messages": [
                        ToolMessage(content="\n".join(lines), tool_call_id=tool_call_id)
                    ]
                }
            )

        if not (keywords or knowledge_point or question_type or difficulty):
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=(
                                "Error: 请提供 question_id（精确讲解单题）或 "
                                "keywords/knowledge_point 筛选条件（列举相似题）。"
                            ),
                            tool_call_id=tool_call_id,
                        )
                    ]
                }
            )

        rows = await _search_questions(
            keywords=keywords,
            knowledge_point=knowledge_point,
            question_type=question_type,
            difficulty=difficulty,
            limit=limit,
        )
        if not rows:
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content="题库中没有匹配的已发布题目。",
                            tool_call_id=tool_call_id,
                        )
                    ]
                }
            )
        lines = [f"题库命中 {len(rows)} 题（仅摘要；讲解单题请传 question_id）："]
        for index, question in enumerate(rows, start=1):
            excerpt = str(question.stem or "").replace("\n", " ")[:_STEM_EXCERPT]
            lines.append(
                f"{index}. QB-{question.id}（{question.question_type}｜"
                f"{question.difficulty or '难度未标注'}｜"
                f"{'、'.join(question.knowledge_points_json or []) or '知识点未标注'}）：{excerpt}"
            )
        lines.append(
            "做题请引导学生前往侧边栏「习题练习」（题目原样渲染、图必随题、可作答判分）；"
            "讲解某题时调用本工具并传 question_id。"
        )
        return Command(
            update={
                "messages": [
                    ToolMessage(content="\n".join(lines), tool_call_id=tool_call_id)
                ]
            }
        )
    except Exception:  # noqa: BLE001
        # 异常细节只进日志；原始异常文本进模型上下文可能带出 SQL/内部路径。
        logger.exception("[question-bank] 题库检索失败")
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            "Error: 题库检索暂时不可用，请稍后重试；"
                            "可先引导学生到侧边栏「习题练习」按题号作答。"
                        ),
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
