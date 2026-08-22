"""试卷分析表 DOCX 渲染编排（M-07 接线）。

- 模板字节解析顺序：ce_report_templates.file_key 绝对路径 → 模板目录
  （env COURSE_EVAL_TEMPLATE_DIR，缺省 <BACKEND_HOME>/course_eval/templates）
  下的 <template_type>.docx。模板是学院公文表格，不入 Git，由运维放到运行时目录。
- 渲染输入全部来自库内真值（任务/课程/大题/统计快照），数字经 stats_engine.fmt
  统一格式化；渲染失败不阻断报告草稿，降级为「草稿已存、docx 未渲染」。
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.course_eval.report.narrative_drafter import assert_no_digits
from backend.application.course_eval.report.paper_analysis_renderer import (
    RenderResult,
    SectionView,
    TaskView,
    render_paper_analysis,
    render_qualitative_sections,
)
from backend.config.loggers import get_logger
from backend.core.config.paths import get_paths
from backend.db.models.course_eval import (
    CeCourse,
    CeExamPaper,
    CeExamSection,
    CeReportTemplate,
    CeSemesterTask,
)

logger = get_logger(__name__)


def template_search_paths(template: CeReportTemplate) -> list[Path]:
    """模板文件候选路径（按优先级）。"""
    candidates: list[Path] = []
    file_key = str(template.file_key or "").strip()
    if file_key.startswith("/"):
        candidates.append(Path(file_key))
    template_dir = os.getenv("COURSE_EVAL_TEMPLATE_DIR", "").strip()
    base = (
        Path(template_dir)
        if template_dir
        else get_paths().base_dir / "course_eval" / "templates"
    )
    if file_key and not file_key.startswith("/"):
        candidates.append(base / file_key)
    candidates.append(base / f"{template.template_type}.docx")
    return candidates


async def load_template_bytes(template: CeReportTemplate) -> tuple[bytes | None, str]:
    """读取模板 docx 字节。找不到时返回 (None, 说明)。"""
    paths = template_search_paths(template)
    for path in paths:
        if await asyncio.to_thread(path.is_file):
            return await asyncio.to_thread(path.read_bytes), str(path)
    return None, "；".join(str(p) for p in paths)


def _fmt_exam_date(value: date | None) -> str:
    if value is None:
        return ""
    return f"{value.year} 年 {value.month} 月 {value.day} 日"


def _grade_text(raw: str | None) -> str:
    grade = str(raw or "").strip()
    if grade and not grade.endswith("级"):
        grade += "级"
    return grade


async def build_render_inputs(
    session: AsyncSession, *, task_id: str, snapshot: dict
) -> tuple[TaskView, list[SectionView], str]:
    """从库内真值组装渲染值对象（大题结构 × 快照得分率按报告序号对齐）。"""
    task = (
        await session.execute(
            select(CeSemesterTask).where(
                CeSemesterTask.semester_task_id == task_id,
                CeSemesterTask.is_deleted.is_(False),
            )
        )
    ).scalar_one()
    course = (
        await session.execute(
            select(CeCourse).where(CeCourse.course_id == task.course_id)
        )
    ).scalar_one_or_none()

    task_view = TaskView(
        course_name=(course.name if course else "") or "",
        grade=_grade_text(task.grade),
        major=str(task.major or ""),
        class_no=str(task.class_no or ""),
        teacher_name=str(task.teacher_name or ""),
        analyzer_name=str(task.analyzer_name or ""),
        hours=str(course.hours) if course and course.hours else "",
        credit=str(course.credit).rstrip("0").rstrip(".")
        if course and course.credit
        else "",
        exam_mode=str(course.exam_mode or "") if course else "",
        course_nature=str(course.course_nature or "") if course else "",
        exam_date=_fmt_exam_date(task.exam_date),
        exam_duration=(
            f"{task.exam_duration_minutes} 分钟"
            if task.exam_duration_minutes
            else ""
        ),
        expected_students=(
            str(task.expected_students) if task.expected_students else ""
        ),
    )

    sections = (
        (
            await session.execute(
                select(CeExamSection)
                .join(CeExamPaper, CeExamPaper.paper_id == CeExamSection.paper_id)
                .where(CeExamPaper.semester_task_id == task_id)
                .order_by(CeExamSection.order_no)
            )
        )
        .scalars()
        .all()
    )
    rates_by_order: dict[int, dict] = {
        int(r["report_order_no"]): r
        for r in snapshot.get("section_rates", [])
        if r.get("report_order_no") is not None
    }
    views: list[SectionView] = []
    for sec in sections:
        order = int(sec.report_order_no or sec.order_no)
        rate_row = rates_by_order.get(order)
        # 有得分数据时以快照得分率的序号为准（排除本班不考的专业专属题）；
        # 还没有得分数据时呈现全部大题结构，得分列留白待补
        if rates_by_order and rate_row is None:
            continue
        views.append(
            SectionView(
                report_order_no=order,
                question_type=str(sec.question_type or ""),
                score_pattern=str(sec.score_pattern or ""),
                total_score=Decimal(sec.total_score) if sec.total_score else None,
                item_count=int(sec.item_count) if sec.item_count else None,
                avg_score=(
                    Decimal(str(rate_row["avg_score"]))
                    if rate_row and rate_row.get("avg_score") not in (None, "")
                    else None
                ),
                rate=(
                    Decimal(str(rate_row["rate"]))
                    if rate_row and rate_row.get("rate") not in (None, "")
                    else None
                ),
            )
        )
    # 报告视图重编号（§模型注释：按专业过滤后重编号）：本班不考的题剔除后，
    # 表 1/表 3 的列从「一」连续排布，与学院人工表习惯一致
    views.sort(key=lambda v: v.report_order_no)
    views = [
        replace(view, report_order_no=index)
        for index, view in enumerate(views, start=1)
    ]
    return task_view, views, str(task.semester or "")


def _rate_word(rate: Decimal | None) -> str:
    if rate is None:
        return "缺少数据"
    if rate < Decimal("60"):
        return "偏低"
    if rate < Decimal("75"):
        return "中等"
    return "较好"


_CN_DIGITS = "零一二三四五六七八九"


def _digits_to_cn(text: str) -> str:
    """阿拉伯数字转中文数字（「目标1」→「目标一」）。

    起草稿要求模型原样引用达成结论，goal 名里的阿拉伯数字会撞上
    assert_no_digits 校验，所以上下文本身必须先无数字化。
    """
    return re.sub(r"\d", lambda m: _CN_DIGITS[int(m.group())], text)


def qualitative_context(
    task_view: TaskView, sections: list[SectionView], achievements: list[dict]
) -> str:
    """起草提示的定性上下文：只用文字化程度词，不含任何数字（§6.4 铁律）。"""
    lines = [f"课程：{_digits_to_cn(task_view.course_name)}"]
    for sec in sections:
        lines.append(
            f"{_digits_to_cn(sec.question_type or '大题')}得分率{_rate_word(sec.rate)}"
        )
    for item in achievements or []:
        raw = item.get("achievement")
        try:
            reached = raw is not None and Decimal(str(raw)) >= Decimal("0.6")
        except Exception:  # noqa: BLE001
            reached = False
        lines.append(
            _digits_to_cn(str(item.get("goal_code", "课程目标")))
            + ("已达成" if reached else "未达成（低于合格线）")
        )
    return "；".join(lines)


async def draft_qualitative_texts(
    context: str,
) -> tuple[dict[str, object], list[str]]:
    """一次 LLM 调用起草四/五/六段定性文字，逐字段做无数字校验。

    - 打 tool-internal-llm 标签：嵌套模型流不得漏进主对话管道（幽灵正文根因）；
    - 含数字的字段整体重试，重试尽后该字段留白并告警（不阻断报告）。
    """
    from backend.core.models.factory import create_scoped_chat_model_async

    warnings: list[str] = []
    prompt = (
        "你是课程考试试卷分析报告的撰写助手。根据以下定性事实，起草报告的三个"
        "段落。硬性要求：**不得出现任何阿拉伯数字或百分比**（数字由系统另行填入），"
        "用「得分率偏低/中等/较好」「多数学生/部分学生」这类定性表述；从教与学两个"
        "方面分析；语言务实，不写空话；每条不超过八十字。"
        "**事实里的达成结论（已达成/未达成）必须原样引用，严禁改写、弱化或概括**"
        "（例如事实说某目标未达成，结论就不得写「均已达成」）。\n"
        f"事实：{context}\n"
        "请以 JSON 返回：{\"fail_reasons\": [\"从教的方面…\", \"从学的方面…\"], "
        '"conclusion": "考试分析结论一段", "improvements": ["改进意见一", "改进意见二"]}'
    )
    drafts: dict[str, object] = {
        "fail_reasons": [],
        "conclusion": "",
        "improvements": [],
    }
    try:
        model = await create_scoped_chat_model_async("chat", temperature=0.0)
        for _attempt in range(2):
            resp = await model.ainvoke(
                prompt,
                config={
                    "tags": ["tool-internal-llm", "course-eval-internal"],
                    "metadata": {"purpose": "course_eval_internal_narrative"},
                },
            )
            from backend.application.course_eval.agent_tools._shared import (
                _llm_json_parse,
            )

            data = _llm_json_parse(str(resp.content))
            fail_reasons = [str(x).strip() for x in data.get("fail_reasons") or []][:2]
            conclusion = str(data.get("conclusion") or "").strip()
            improvements = [str(x).strip() for x in data.get("improvements") or []][:2]
            all_texts = [*fail_reasons, conclusion, *improvements]
            if all(assert_no_digits(t) for t in all_texts if t):
                drafts = {
                    "fail_reasons": fail_reasons,
                    "conclusion": conclusion,
                    "improvements": improvements,
                }
                break
        else:
            warnings.append("定性段落草稿多次含数字被拒，四/五/六段留白待教师撰写")
    except Exception as exc:  # noqa: BLE001 - 起草失败必须降级为留白
        logger.warning("[CourseEvalRender] 定性段落起草失败，留白: %s", exc)
        warnings.append("定性段落起草失败（四/五/六段留白待教师撰写）")
    return drafts, warnings


def report_filename(task_view: TaskView, semester: str, version_no: int) -> str:
    """下载文件名：课程 + 学期 + 版本，去掉文件系统敏感字符。"""
    course = re.sub(r"[\\/:*?\"<>|]", "", task_view.course_name) or "课程"
    return f"{course}试卷分析表_{semester}_v{version_no}.docx"


async def render_paper_analysis_docx(
    session: AsyncSession,
    *,
    task_id: str,
    template: CeReportTemplate,
    snapshot: dict,
    version_no: int = 1,
    achievements: list[dict] | None = None,
) -> tuple[RenderResult | None, str, str]:
    """渲染试卷分析表。返回 (结果, 失败说明, 下载文件名)。

    模板文件缺失时结果为 None（草稿照常落库，降级说明放失败说明）；
    模板锚点与结构不符（TemplateAnchorMismatch）任其抛出——那是模板配置错误，
    必须暴露而不是出一份错表。
    """
    template_bytes, source = await load_template_bytes(template)
    if template_bytes is None:
        return None, (
            "模板文件未配置：请把学院空白模板 docx 放到模板目录"
            f"（已尝试 {source}）或在 ce_report_templates.file_key 填绝对路径"
        ), ""
    task_view, sections, semester = await build_render_inputs(
        session, task_id=task_id, snapshot=snapshot
    )
    result = await asyncio.to_thread(
        render_paper_analysis,
        template_bytes,
        task=task_view,
        sections=sections,
        final_score_stats=snapshot.get("final_score_stats"),
    )

    # 四/五/六段自动起草初稿（教师可改），七段写数据需关注事项
    achievements = achievements if achievements is not None else []
    drafts, draft_warnings = await draft_qualitative_texts(
        qualitative_context(task_view, sections, achievements)
    )
    result.warnings.extend(draft_warnings)
    other_notes = [str(x) for x in (snapshot.get("blocking_issues") or [])]

    def _fill_qualitative(docx_bytes: bytes) -> bytes:
        import io

        from docx import Document

        doc = Document(io.BytesIO(docx_bytes))
        render_qualitative_sections(
            doc,
            fail_reasons=list(drafts.get("fail_reasons") or []),
            conclusion=str(drafts.get("conclusion") or ""),
            improvements=list(drafts.get("improvements") or []),
            other_notes=other_notes,
            warnings=result.warnings,
        )
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    result.docx_bytes = await asyncio.to_thread(_fill_qualitative, result.docx_bytes)
    logger.info(
        "[CourseEvalRender] 试卷分析表渲染完成: task=%s template=%s sections=%d warnings=%d",
        task_id,
        source,
        len(sections),
        len(result.warnings),
    )
    return result, "", report_filename(task_view, semester, version_no)
