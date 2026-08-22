"""课程评价流水线工具（状态迁移 / 校验 / 映射建议 / 统计 / 报告 / PDF 导出）。

写工具全部走 course_eval service 层（task_service.transition /
report_service.compute 等），状态机护栏 + 审计 + assert_can_edit_data 天然生效。
"""

from __future__ import annotations

import json
import re

from langchain_core.tools import tool

from backend.application.course_eval.agent_tools import _shared
from backend.config.loggers import get_logger

logger = get_logger(__name__)


def build_pipeline_tools(runtime_context):
    """构建流水线 6 工具，按 (transition_task, run_validation,
    suggest_goal_mapping, compute_statistics, generate_report,
    export_report_pdf) 顺序返回。"""

    @tool("transition_task")
    async def transition_task(semester_task_id: str, target: str) -> str:
        """推进学期任务状态机（唯一迁移入口）。合法目标取决于当前状态；
        非法迁移会被拒绝并返回 allowed_actions。"""
        try:
            operator = _shared._operator_id(runtime_context)
            from backend.application.course_eval import task_service
            from backend.application.course_eval.errors import CourseEvalStateError

            async with await _shared._open_session() as session:
                # 写工具:加锁读必须是事务第一条语句(先普通读会提前建立 RR 读
                # 视图,持锁后仍看旧快照);populate_existing 见 get_task 注释。
                await _shared._assert_task_owner(
                    session,
                    semester_task_id,
                    user_id=operator,
                    is_admin=_shared._is_admin_role(runtime_context),
                    for_update=True,
                )
                try:
                    updated = await task_service.transition(
                        session, semester_task_id, target, operator_id=operator
                    )
                except CourseEvalStateError as exc:
                    return _shared._json(
                        {
                            "error": f"非法状态迁移 {exc.current} -> {exc.target}",
                            "current": exc.current,
                            "allowed_actions": sorted(exc.allowed),
                        }
                    )
                await session.commit()
                return _shared._json(
                    {
                        "semester_task_id": semester_task_id,
                        "status": updated.status,
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 状态迁移失败")
            return _shared._json({"error": str(exc)})

    @tool("run_validation")
    async def run_validation(semester_task_id: str) -> str:
        """对已提取数据执行确定性校验规则（GS-*/PS-*），返回 issue 清单。
        有 error 级 issue 时建议停在 needs_review 待教师处理。"""
        try:
            from sqlalchemy import select

            from backend.application.course_eval.extraction.validation import (
                ValidationContext,
                run_rules,
            )
            from backend.db.models.course_eval import (
                CeExamPaper,
                CeExamSection,
                CeScoreRecord,
                CeStudent,
            )

            async with await _shared._open_session() as session:
                task = await _shared._load_task(
                    session, semester_task_id, runtime_context=runtime_context
                )
                students = (
                    (
                        await session.execute(
                            select(CeStudent).where(
                                CeStudent.semester_task_id == semester_task_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                records = (
                    (
                        await session.execute(
                            select(CeScoreRecord).where(
                                CeScoreRecord.semester_task_id == semester_task_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                papers = (
                    (
                        await session.execute(
                            select(CeExamPaper).where(
                                CeExamPaper.semester_task_id == semester_task_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                sections = []
                for paper in papers:
                    sections.extend(
                        (
                            await session.execute(
                                select(CeExamSection).where(
                                    CeExamSection.paper_id == paper.paper_id
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                ctx = ValidationContext(
                    records=[
                        {
                            "student_no": s.student_no,
                            "regular": s.regular_score,
                            "final": s.final_score,
                            "total": s.total_score,
                            "flag": s.flag or "",
                        }
                        for s in students
                    ],
                    summary={
                        "expected": task.expected_students,
                        "absent": task.absent_students,
                        "violation": task.violation_students,
                    },
                    score_records=[
                        {
                            "student_id": r.student_id,
                            "section_id": r.section_id,
                            "item_id": r.item_id,
                            "score": r.score,
                            "full_score": r.full_score or 0,
                        }
                        for r in records
                    ],
                    sections=[
                        {
                            "section_id": s.section_id,
                            "order_no": s.order_no,
                            "question_type": s.question_type,
                            "total_score": s.total_score,
                            "applicable_majors": s.applicable_majors,
                        }
                        for s in sections
                    ],
                )
                issues = run_rules(ctx)
                errors = [i for i in issues if i.level == "error"]
                return _shared._json(
                    {
                        "semester_task_id": semester_task_id,
                        "issues": [i.__dict__ for i in issues],
                        "error_count": len(errors),
                        "passed": not issues,
                        "next": (
                            "校验通过；如需迁移可调用 transition_task，或调用 "
                            "suggest_goal_mapping 生成映射建议"
                            if not issues
                            else "存在 issue；建议停在 needs_review 由教师确认后继续"
                        ),
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 校验失败")
            return _shared._json({"error": str(exc)})

    @tool("suggest_goal_mapping")
    async def suggest_goal_mapping(semester_task_id: str) -> str:
        """生成试题-目标映射建议（LLM 语义匹配，**不落库**；教师经 REST PUT /mappings 确认）。"""
        try:
            from sqlalchemy import select

            from backend.db.models.course_eval import (
                CeCourseGoal,
                CeExamPaper,
                CeExamSection,
            )

            async with await _shared._open_session() as session:
                task = await _shared._load_task(
                    session, semester_task_id, runtime_context=runtime_context
                )
                goals = (
                    (
                        await session.execute(
                            select(CeCourseGoal).where(
                                CeCourseGoal.course_id == task.course_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                papers = (
                    (
                        await session.execute(
                            select(CeExamPaper).where(
                                CeExamPaper.semester_task_id == semester_task_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                from backend.db.models.course_eval import CeScoreRecord

                scored_sections = {
                    row[0]
                    for row in (
                        await session.execute(
                            select(CeScoreRecord.section_id).where(
                                CeScoreRecord.semester_task_id == semester_task_id
                            )
                        )
                    ).all()
                }
                sections: list[dict] = []
                skipped: list[str] = []
                for paper in papers:
                    rows = (
                        (
                            await session.execute(
                                select(CeExamSection)
                                .where(CeExamSection.paper_id == paper.paper_id)
                                .order_by(CeExamSection.order_no)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for s in rows:
                        # 专业专属且本班全员无得分 = 本班不考，不该占目标支撑分
                        if s.applicable_majors and s.section_id not in scored_sections:
                            skipped.append(f"第{s.order_no}题（{s.question_type}）")
                            continue
                        sections.append(
                            {
                                "section_id": s.section_id,
                                "order_no": s.order_no,
                                "question_type": s.question_type,
                                "total_score": str(s.total_score or 0),
                            }
                        )
                goal_payload = [
                    {
                        "goal_id": g.goal_id,
                        "goal_code": g.goal_code,
                        "weight_exam": str(g.weight_exam or 0),
                        "description": g.description or "",
                    }
                    for g in goals
                ]
                if not sections or not goal_payload:
                    return _shared._json(
                        {
                            "error": "缺少大题结构或课程目标，请先 extract_paper / extract_syllabus",
                        }
                    )
                prompt = (
                    "你是课程达成度评价助手。请把试卷大题映射到课程目标，给出权重（每题权重和=1）。\n"
                    "下列大题都是本教学班实际考的题（本班不考的专业专属题已剔除），"
                    "order_no 是试卷题号。\n"
                    "判定唯一依据是课程目标的 description 语义：考查基本概念/单一知识点"
                    "的题对应偏「基础/掌握」的目标，综合分析/工程应用的题对应偏"
                    "「综合/应用/解决问题」的目标。相同输入必须给出相同映射。\n"
                    f"大题:\n{json.dumps(sections, ensure_ascii=False)}\n"
                    f"目标:\n{json.dumps(goal_payload, ensure_ascii=False)}\n"
                    '请以 JSON 返回 {"mappings":[{"section_id":..,"goal_id":..,"weight":..}]}'
                )
                result = await _shared._llm_json_call(prompt, temperature=0.0)
                # 防幻觉：只接受真实存在的 section_id/goal_id，并给每条建议
                # 带上教师可读标签（第X题·题型 / 目标名），确认卡不再出现内部 ID
                section_by_id = {s["section_id"]: s for s in sections}
                goal_by_id = {g["goal_id"]: g for g in goal_payload}
                suggestions: list[dict] = []
                dropped = 0
                for item in result.get("mappings", []) or []:
                    sec = section_by_id.get(str(item.get("section_id", "")))
                    goal = goal_by_id.get(str(item.get("goal_id", "")))
                    if sec is None or goal is None:
                        dropped += 1
                        continue
                    suggestions.append(
                        {
                            "section_id": sec["section_id"],
                            "goal_id": goal["goal_id"],
                            "weight": item.get("weight", 1),
                            "section_label": (
                                f"第{sec['order_no']}题·{sec['question_type']}"
                            ),
                            "goal_label": goal["goal_code"],
                        }
                    )
                warnings: list[str] = []
                if dropped:
                    warnings.append(
                        f"{dropped} 条建议引用了不存在的大题/目标编号，已丢弃"
                    )
                unmapped = [
                    f"第{s['order_no']}题·{s['question_type']}"
                    for s in sections
                    if s["section_id"]
                    not in {m["section_id"] for m in suggestions}
                ]
                if unmapped:
                    warnings.append(
                        "以下大题没有得到映射建议，请教师补充："
                        + "、".join(unmapped)
                    )
                return _shared._json(
                    {
                        "semester_task_id": semester_task_id,
                        "suggestions": suggestions,
                        "warnings": warnings,
                        "excluded_sections": (
                            f"{'、'.join(skipped)}本班不考，未纳入映射" if skipped else ""
                        ),
                        "note": (
                            "建议未落库；教师确认后调用 apply_mappings，mappings 参数"
                            "必须逐字复用本返回中的 section_id/goal_id，不得手工改写"
                        ),
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 映射建议失败")
            return _shared._json({"error": str(exc)})

    @tool("compute_statistics")
    async def compute_statistics(semester_task_id: str) -> str:
        """计算统计快照（成绩分布/得分率），落 ce_stats_snapshots。"""
        try:
            operator = _shared._operator_id(runtime_context)
            from backend.application.course_eval.report_service import compute

            async with await _shared._open_session() as session:
                # 写工具:加锁读必须是事务第一条语句(先普通读会提前建立 RR 读
                # 视图,持锁后仍看旧快照);populate_existing 见 get_task 注释。
                await _shared._assert_task_owner(
                    session,
                    semester_task_id,
                    user_id=operator,
                    is_admin=_shared._is_admin_role(runtime_context),
                    for_update=True,
                    # 归档=软锁定：归档后不得重算快照（见 _load_task 注释）。
                    require_editable=True,
                )
                snap = await compute(
                    session, task_id=semester_task_id, operator_id=operator
                )
                await session.commit()
                return _shared._json(
                    {
                        "semester_task_id": semester_task_id,
                        "snapshot_id": snap.snapshot_id,
                        "next": "快照已生成；可调用 generate_report 生成报告草稿",
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 计算统计失败")
            return _shared._json({"error": str(exc)})

    @tool("generate_report")
    async def generate_report(semester_task_id: str) -> str:
        """生成报告草稿（引用不可变统计快照），落 ce_reports / ce_report_versions。"""
        try:
            operator = _shared._operator_id(runtime_context)
            from sqlalchemy import select

            from backend.application.course_eval.report_service import latest_snapshot
            from backend.db.models.course_eval import CeReport, CeReportVersion

            async with await _shared._open_session() as session:
                # 写工具:加锁读必须是事务第一条语句(先普通读会提前建立 RR 读
                # 视图,持锁后仍看旧快照);populate_existing 见 get_task 注释。
                await _shared._assert_task_owner(
                    session,
                    semester_task_id,
                    user_id=operator,
                    is_admin=_shared._is_admin_role(runtime_context),
                    for_update=True,
                    # 归档=软锁定：归档后不得再产出新报告版本。
                    require_editable=True,
                )
                data = await latest_snapshot(session, semester_task_id)
                if data is None:
                    return _shared._json(
                        {"error": "暂无统计快照，请先 compute_statistics"}
                    )
                from backend.db.models.course_eval import CeReportTemplate

                template = (
                    (
                        await session.execute(
                            select(CeReportTemplate).where(
                                CeReportTemplate.template_type == "paper_analysis",
                                CeReportTemplate.active.is_(True),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if template is None:
                    return _shared._json(
                        {"error": "报告模板未注册，暂无法渲染 docx；可先由教师在报告编辑页录入"}
                    )
                report = CeReport(
                    semester_task_id=semester_task_id,
                    template_id=template.template_id,
                    report_type="paper_analysis",
                    status="draft",
                    current_version_no=1,
                    stats_snapshot_id=data.get("snapshot_id"),
                    created_by=operator,
                )
                session.add(report)
                await session.flush()
                # 附表 1 的课程目标达成度也要落进草稿，否则报告缺最关键指标
                from backend.application.course_eval.report_service import (
                    compute_achievement,
                )

                achievements = await compute_achievement(session, semester_task_id)
                version = CeReportVersion(
                    report_id=report.report_id,
                    version_no=1,
                    content_json=json.dumps(
                        {
                            "scope": data.get("scope"),
                            "final_score_stats": data.get("final_score_stats"),
                            "section_rates": data.get("section_rates"),
                            "goal_achievements": achievements,
                            "blocking_issues": data.get("blocking_issues"),
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                    created_by=operator,
                )
                session.add(version)

                # ── DOCX 渲染（学院模板锚点填充）：失败不阻断草稿 ──
                from backend.application.course_eval import render_service

                docx_info: dict = {"rendered": False}
                render_result, render_fail, filename = (
                    await render_service.render_paper_analysis_docx(
                        session,
                        task_id=semester_task_id,
                        template=template,
                        snapshot=data,
                        version_no=1,
                        achievements=achievements,
                    )
                )
                if render_result is None:
                    docx_info["reason"] = render_fail
                else:
                    registered = await _shared._register_report_artifact(
                        runtime_context,
                        filename=filename,
                        payload=render_result.docx_bytes,
                    )
                    if registered is None:
                        docx_info["reason"] = (
                            "docx 已渲染但登记下载文件失败，请稍后重试"
                        )
                    else:
                        file_id, download_url = registered
                        version.docx_file_key = file_id
                        docx_info = {
                            "rendered": True,
                            "filename": filename,
                            "file_id": file_id,
                            "download_url": download_url,
                            "warnings": render_result.warnings,
                        }
                await session.commit()
                return _shared._json(
                    {
                        "semester_task_id": semester_task_id,
                        "report_id": report.report_id,
                        "status": report.status,
                        "goal_achievements": achievements,
                        "blocking_issues": data.get("blocking_issues"),
                        "docx": docx_info,
                        "next": (
                            "报告草稿与试卷分析表 docx 已生成；"
                            "把下载链接（Markdown 链接格式）给教师，"
                            "并说明定性分析段（不及格原因/结论/改进措施）需教师补写；"
                            "然后 transition_task 至 report_draft"
                            if docx_info.get("rendered")
                            else "报告草稿已落库（docx 未渲染，原因见 docx.reason）；"
                            "可 transition_task 至 report_draft 进入报告编辑"
                        ),
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 生成报告失败")
            return _shared._json({"error": str(exc)})

    @tool("export_report_pdf")
    async def export_report_pdf(semester_task_id: str) -> str:
        """把已生成的《试卷分析表》最新版 DOCX 转成 PDF 并返回下载链接。

        只做格式转换：不改报告数据、不新建报告版本。需先 generate_report 渲染出
        docx；PDF 内容与该 docx 一致（定性段仍是草稿，教师补写后应以 docx 为准）。"""
        try:
            operator = _shared._operator_id(runtime_context)
            from sqlalchemy import select

            from backend.db.models.course_eval import CeReport, CeReportVersion

            # LibreOffice 冷启动是秒级的，必须在会话之外转换：包在会话里会让
            # 同任务的写工具长时间等锁（与 MinerU OCR 同类约束）。这里只在
            # 会话内完成鉴权与取字节。
            async with await _shared._open_session() as session:
                await _shared._assert_task_owner(
                    session,
                    semester_task_id,
                    user_id=operator,
                    is_admin=_shared._is_admin_role(runtime_context),
                )
                version = (
                    (
                        await session.execute(
                            select(CeReportVersion)
                            .join(
                                CeReport,
                                CeReport.report_id == CeReportVersion.report_id,
                            )
                            .where(
                                CeReport.semester_task_id == semester_task_id,
                                CeReportVersion.docx_file_key.isnot(None),
                                CeReportVersion.docx_file_key != "",
                            )
                            .order_by(CeReportVersion.version_no.desc())
                        )
                    )
                    .scalars()
                    .first()
                )
                if version is None:
                    return _shared._json(
                        {
                            "error": "该任务还没有已渲染的试卷分析表 docx，请先 generate_report"
                        }
                    )
                docx_bytes, display_name = await _shared._read_material_bytes(
                    session, str(version.docx_file_key), operator
                )
                version_no = int(version.version_no)

            from backend.application.blocking import run_blocking
            from backend.services.document_conversion import convert_docx_to_pdf

            pdf_bytes = await run_blocking(
                convert_docx_to_pdf, docx_bytes, timeout_seconds=120
            )
            if pdf_bytes is None:
                return _shared._json(
                    {
                        "error": "PDF 转换不可用（LibreOffice 缺失或转换失败），"
                        "请继续使用 docx 下载链接"
                    }
                )

            pdf_filename = re.sub(r"\.docx$", "", str(display_name or "试卷分析表"))
            registered = await _shared._register_report_artifact(
                runtime_context,
                filename=f"{pdf_filename}.pdf",
                payload=pdf_bytes,
            )
            if registered is None:
                return _shared._json(
                    {"error": "PDF 已生成但登记下载文件失败，请稍后重试"}
                )
            file_id, download_url = registered
            return _shared._json(
                {
                    "semester_task_id": semester_task_id,
                    "version_no": version_no,
                    "filename": f"{pdf_filename}.pdf",
                    "file_id": file_id,
                    "download_url": download_url,
                    "next": "把 PDF 下载链接以 Markdown 链接格式给教师；"
                    "若教师还要补写定性段，请提示以 docx 版本为编辑底稿",
                }
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 导出报告 PDF 失败")
            return _shared._json({"error": str(exc)})

    return (
        transition_task,
        run_validation,
        suggest_goal_mapping,
        compute_statistics,
        generate_report,
        export_report_pdf,
    )
