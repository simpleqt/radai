"""课程评价材料解析工具（成绩单 / 答题试卷 / 教学大纲）。

试卷 OCR 领域逻辑在 ``backend.application.course_eval.paper_ocr``；
本模块的闭包以裸名引用 ``ocr_paper_structure`` 等符号，
测试可通过 ``monkeypatch.setattr(extraction_tools, "ocr_paper_structure", ...)`` 替换。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from langchain_core.tools import tool

from backend.application.course_eval import task_service
from backend.application.course_eval.agent_tools import _shared
from backend.application.course_eval.paper_ocr import (
    _STRUCTURE_BODY_PAGES,
    PaperOcrResult,
    align_covers_to_students,
    build_cover_cells,
    ocr_paper_structure,
    resolve_applicable_orders,
    section_full_scores_by_major,
)
from backend.config.loggers import get_logger

logger = get_logger(__name__)


def build_extraction_tools(runtime_context):
    """构建 extract_grade_sheet / extract_paper / extract_syllabus 三个写工具。"""

    @tool("extract_grade_sheet")
    async def extract_grade_sheet(
        semester_task_id: str, material_id: str
    ) -> str:
        """解析成绩单 PDF 并落学生成绩（确定性坐标法）。须先上传成绩单材料
        （material_id 从任务资料列表获取），且任务处于 materials_ready 或之后状态。"""
        try:
            operator = _shared._operator_id(runtime_context)
            from sqlalchemy import select

            from backend.application.course_eval.extraction.grade_sheet_parser import (
                parse_grade_sheet,
            )
            from backend.db.models.course_eval import CeMaterial, CeStudent

            async with await _shared._open_session() as session:
                # 任务行锁作为事务第一条语句:串行化同任务的并发解析,并保证
                # 后续学生表读写的读视图在持锁后建立(见 _load_task 注释)。
                await _shared._load_task(
                    session,
                    semester_task_id,
                    runtime_context=runtime_context,
                    for_update=True,
                    # 归档=软锁定：归档后不得再改学生成绩。
                    require_editable=True,
                )
                material = (
                    await session.execute(
                        select(CeMaterial).where(
                            CeMaterial.material_id == material_id,
                            CeMaterial.semester_task_id == semester_task_id,
                            CeMaterial.material_type == "grade_sheet",
                            CeMaterial.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if material is None:
                    return _shared._json(
                        {"error": "未找到该任务的成绩单材料，请先上传（文件名含「成绩单」）"}
                    )
                data, _name = await _shared._read_material_bytes(
                    session, material.file_id, operator
                )
                result = await asyncio.to_thread(parse_grade_sheet, data)

                existing = {
                    s.student_no: s
                    for s in (
                        await session.execute(
                            select(CeStudent).where(
                                CeStudent.semester_task_id == semester_task_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                }
                written = 0
                for rec in result.records:
                    row = existing.get(rec.student_no)
                    if row is None:
                        row = CeStudent(
                            semester_task_id=semester_task_id,
                            student_no=rec.student_no,
                            name=rec.name,
                            source="grade_sheet",
                        )
                        session.add(row)
                    row.regular_score = rec.regular
                    row.final_score = rec.final
                    row.total_score = rec.total
                    row.flag = rec.flag or ""
                    row.in_grade_sheet = True
                    written += 1
                task = await _shared._load_task(
                    session, semester_task_id, runtime_context=runtime_context
                )
                if result.summary.get("expected"):
                    task.expected_students = int(result.summary["expected"])
                if result.summary.get("absent"):
                    task.absent_students = int(result.summary["absent"])
                if result.summary.get("violation"):
                    task.violation_students = int(result.summary["violation"])
                if written:
                    await task_service.invalidate_downstream(
                        session,
                        semester_task_id,
                        from_stage="extract.grade_sheet",
                        operator_id=_shared._operator_id(runtime_context),
                    )
                await session.commit()
                return _shared._json(
                    {
                        "written": written,
                        "records": len(result.records),
                        "summary": result.summary,
                        "meta": result.meta,
                        "next": "成绩单已入库；可执行 run_validation 校验，或 extract_paper 解析试卷",
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 成绩单解析失败")
            # 身份拒绝文案原样透出（访客调用的可预期路径）；其余异常
            # 可能嵌入学生姓名/成绩行，返回固定分类文案。
            if "登录教师身份" in str(exc):
                return _shared._json({"error": str(exc)})
            return _shared._json({"error": "成绩单解析失败，请检查文件后重试"})

    @tool("extract_paper")
    async def extract_paper(semester_task_id: str, material_id: str) -> str:
        """解析答题试卷 PDF：一次 MinerU OCR 同时取大题结构（题型/满分）与封面得分表。
        必须在 extract_grade_sheet 成功之后调用（封面得分要按总分对到成绩单学生上），
        不要与它并行。各题之和与封面总分自洽的页才 confirmed，其余进待确认清单。"""
        try:
            operator = _shared._operator_id(runtime_context)
            from sqlalchemy import select

            from backend.application.course_eval.extraction.paper_layout import (
                detect_tables,
                is_cover,
            )
            from backend.application.course_eval.extraction.pdf_pager import (
                render_pages,
            )
            from backend.db.models.course_eval import (
                CeExamItem,
                CeExamPaper,
                CeExamSection,
                CeMaterial,
            )

            # 读材料与 OCR 都在事务之外完成：MinerU 要跑几十秒，若把它包在写事务里，
            # 可重复读快照会让后面读不到并行工具（extract_grade_sheet）刚提交的学生名单。
            async with await _shared._open_session() as read_session:
                await _shared._assert_task_owner(
                    read_session,
                    semester_task_id,
                    user_id=operator,
                    is_admin=_shared._is_admin_role(runtime_context),
                )
                material = (
                    await read_session.execute(
                        select(CeMaterial).where(
                            CeMaterial.material_id == material_id,
                            CeMaterial.semester_task_id == semester_task_id,
                            CeMaterial.material_type == "paper",
                            CeMaterial.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if material is None:
                    return _shared._json(
                        {"error": "未找到该任务的试卷材料，请先上传（文件名含「试卷」且为 PDF）"}
                    )
                data, _name = await _shared._read_material_bytes(
                    read_session, material.file_id, operator
                )
                # 上一轮已确认的大题结构是真值（§6.3.4），用它的题数约束得分表列对齐
                declared_count = len(
                    [
                        s
                        for s in (
                            await read_session.execute(
                                select(CeExamSection)
                                .join(
                                    CeExamPaper,
                                    CeExamPaper.paper_id == CeExamSection.paper_id,
                                )
                                .where(
                                    CeExamPaper.semester_task_id == semester_task_id
                                )
                            )
                        )
                        .scalars()
                        .all()
                        if s.question_type != "待核对" and s.total_score
                    ]
                ) or None

            pages = await asyncio.to_thread(render_pages, data)
            covers = [
                i
                for i, page in enumerate(pages)
                if await asyncio.to_thread(is_cover, page.gray)
            ]
            table_pages = [
                i
                for i, page in enumerate(pages)
                if len(await asyncio.to_thread(detect_tables, page.gray)) > 0
            ]

            # ── MinerU 单通道：一次 OCR 同时拿大题结构与封面得分 ──
            from backend.application.course_eval.extraction.paper_layout import (
                segment_students,
            )

            auto_warnings: list[str] = []
            ocr_result = PaperOcrResult({}, [], 0, [])
            cover_indices: list[int] = []
            try:
                ranges, _seg_issues = await asyncio.to_thread(segment_students, pages)
                cover_indices = [
                    r.cover_page for r in ranges if r.cover_page is not None
                ]
                body_pages: list[int] = []
                if ranges:
                    first = ranges[0]
                    body_pages = list(
                        range(
                            first.cover_page + 1,
                            min(
                                first.end_page,
                                first.cover_page + _STRUCTURE_BODY_PAGES,
                            )
                            + 1,
                        )
                    )
                if cover_indices:
                    ocr_result = await ocr_paper_structure(
                        pdf_bytes=data,
                        cover_pages=cover_indices,
                        body_pages=body_pages,
                        declared_section_count=declared_count,
                    )
                    auto_warnings.extend(ocr_result.warnings)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[CourseEvalTool] 试卷 OCR 失败（降级，不影响版面解析）: %s", exc
                )
                auto_warnings.append(f"自动识别暂不可用（{exc}），可在对话中补录")

            async with await _shared._open_session() as session:
                # 写事务第一条语句锁任务行:防两次并发 extract_paper 双建试卷,
                # 并与 update_scores 互斥——否则本事务旧快照算出的"stale 待删"
                # 集合会按主键删掉教师并发写入的 manual 修正(见 _load_task 注释)。
                await _shared._load_task(
                    session,
                    semester_task_id,
                    runtime_context=runtime_context,
                    for_update=True,
                    # 归档=软锁定：归档后不得重写试卷结构与逐题得分。
                    require_editable=True,
                )
                existing_paper = (
                    await session.execute(
                        select(CeExamPaper).where(
                            CeExamPaper.semester_task_id == semester_task_id
                        )
                    )
                ).scalars().first()
                if existing_paper is None:
                    existing_paper = CeExamPaper(semester_task_id=semester_task_id)
                    session.add(existing_paper)
                    await session.flush()
                paper = existing_paper

                existing_sections = {
                    s.order_no: s
                    for s in (
                        await session.execute(
                            select(CeExamSection).where(
                                CeExamSection.paper_id == paper.paper_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                }
                # ── 大题结构落库：题型与满分来自试卷正文的大题标题 ──
                section_defs = ocr_result.section_defs
                section_count = (
                    ocr_result.section_count or len(existing_sections) or 0
                )
                for definition in section_defs:
                    sec = existing_sections.get(definition.order_no)
                    if sec is None:
                        sec = CeExamSection(
                            paper_id=paper.paper_id,
                            order_no=definition.order_no,
                            report_order_no=definition.order_no,
                            question_type=definition.question_type,
                        )
                        session.add(sec)
                        existing_sections[definition.order_no] = sec
                    elif sec.question_type in ("", "待核对"):
                        sec.question_type = definition.question_type
                    if definition.total_score is not None and not sec.total_score:
                        sec.total_score = Decimal(definition.total_score)
                    if definition.score_pattern and not sec.score_pattern:
                        sec.score_pattern = definition.score_pattern
                    if definition.applicable_major and not sec.applicable_majors:
                        sec.applicable_majors = [definition.applicable_major]
                # 清掉上一轮遗留的空占位大题（无题型无满分且没有得分记录）
                from backend.db.models.course_eval import CeScoreRecord, CeStudent

                for order_no, sec in list(existing_sections.items()):
                    if section_count and order_no <= section_count:
                        continue
                    if sec.question_type not in ("", "待核对") or sec.total_score:
                        continue
                    has_records = (
                        await session.execute(
                            select(CeScoreRecord.record_id).where(
                                CeScoreRecord.section_id == sec.section_id
                            )
                        )
                    ).first()
                    if has_records is None:
                        await session.delete(sec)
                        existing_sections.pop(order_no, None)
                await session.flush()
                if not existing_sections:
                    auto_warnings.append(
                        "未能识别出大题结构，请在对话中说明各大题题型与满分"
                    )
                totals_by_major = section_full_scores_by_major(section_defs)
                if totals_by_major:
                    paper.total_score_by_major = {
                        k: str(v) for k, v in totals_by_major.items()
                    }

                section_ids = [s.section_id for s in existing_sections.values()]
                item_count = (
                    await session.execute(
                        select(CeExamItem).where(
                            CeExamItem.section_id.in_(section_ids)
                        )
                    )
                ).scalars().all().__len__() if section_ids else 0
                paper.item_count_total = item_count

                # ── 封面得分落库 ──
                students_rows = (
                    await session.execute(
                        select(CeStudent).where(
                            CeStudent.semester_task_id == semester_task_id
                        ).order_by(CeStudent.student_no)
                    )
                ).scalars().all()
                applicable_orders, skip_warnings = resolve_applicable_orders(ocr_result)
                auto_warnings.extend(skip_warnings)
                students_missing = bool(ocr_result.cover_rows) and not students_rows
                if students_missing:
                    auto_warnings.append(
                        "成绩单学生名单尚未入库，封面得分无法挂到学生上："
                        "请先 extract_grade_sheet，再重跑 extract_paper"
                    )
                auto_cells: list[dict] = []
                if ocr_result.cover_rows and students_rows:
                    cover_to_student, align_warnings = align_covers_to_students(
                        ocr_result.cover_rows, len(cover_indices), students_rows
                    )
                    auto_warnings.extend(align_warnings)
                    auto_cells, cell_warnings = build_cover_cells(
                        cover_rows=ocr_result.cover_rows,
                        cover_to_student=cover_to_student,
                        cover_pages=cover_indices,
                        sections_by_order={
                            order: sec.section_id
                            for order, sec in existing_sections.items()
                        },
                        full_scores={
                            order: Decimal(sec.total_score or 0)
                            for order, sec in existing_sections.items()
                        },
                        applicable_orders=applicable_orders,
                    )
                    auto_warnings.extend(cell_warnings)
                    for student_id in set(cover_to_student.values()):
                        student = next(
                            (s for s in students_rows if s.student_id == student_id),
                            None,
                        )
                        if student is not None:
                            student.in_paper_sample = True

                # 重跑时先清掉上一轮的自动识别结果，教师手工修正（manual）保留
                deleted_any = False
                if auto_cells:
                    stale = (
                        (
                            await session.execute(
                                select(CeScoreRecord).where(
                                    CeScoreRecord.semester_task_id
                                    == semester_task_id,
                                    CeScoreRecord.source != "manual",
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    manual_keys = {
                        (r.student_id, r.section_id, r.item_id)
                        for r in (
                            await session.execute(
                                select(CeScoreRecord).where(
                                    CeScoreRecord.semester_task_id
                                    == semester_task_id,
                                    CeScoreRecord.source == "manual",
                                )
                            )
                        )
                        .scalars()
                        .all()
                    }
                    for record in stale:
                        await session.delete(record)
                    deleted_any = bool(stale)
                    await session.flush()
                else:
                    manual_keys = set()

                written = 0
                for cell in auto_cells:
                    if (cell["student_id"], cell["section_id"], "") in manual_keys:
                        continue
                    session.add(
                        CeScoreRecord(
                            semester_task_id=semester_task_id,
                            student_id=cell["student_id"],
                            section_id=cell["section_id"],
                            item_id="",
                            full_score=cell.get("full_score") or Decimal(0),
                            score=cell["score"],
                            raw_score=cell.get("raw_score"),
                            source=cell.get("source", "ocr_table"),
                            channel_agree=cell.get("channel_agree"),
                            confidence=cell.get("confidence"),
                            status=cell.get("status", "pending_review"),
                            page_ref=cell.get("page_ref"),
                            extract_version=1,
                        )
                    )
                    written += 1
                if written or deleted_any:
                    await task_service.invalidate_downstream(
                        session,
                        semester_task_id,
                        from_stage="extract.paper",
                        operator_id=_shared._operator_id(runtime_context),
                    )
                await session.commit()
                pending = sum(
                    1 for c in auto_cells if c.get("status") != "confirmed"
                )
                return _shared._json(
                    {
                        "pages": len(pages),
                        "cover_pages": covers,
                        "score_table_pages": table_pages,
                        "sections": [
                            {
                                "order_no": order,
                                "question_type": sec.question_type,
                                "total_score": sec.total_score,
                                "applicable_majors": sec.applicable_majors,
                                "counted_in_sample": order in applicable_orders,
                            }
                            for order, sec in sorted(existing_sections.items())
                        ],
                        "paper_sample_n": len(
                            {c["student_id"] for c in auto_cells}
                        ),
                        "score_cells": written,
                        "pending_review_cells": pending,
                        "auto_extract_warnings": auto_warnings,
                        "next": (
                            f"试卷结构与封面得分已入库（{written} 条，其中 {pending} 条待核对）。"
                            "请用 get_score_grid 把待核对项摆给教师确认，"
                            "必要时 update_scores 修正，然后 run_validation 校验。"
                            if written
                            else (
                                "试卷结构已入库，但成绩单学生名单还没入库，封面得分无处挂靠："
                                "请先调用 extract_grade_sheet，成功后重跑 extract_paper。"
                                if students_missing
                                else "试卷结构已入库；自动识别未得到得分，"
                                "请用 update_scores 在对话中补录样本试卷各题得分，"
                                "否则课程目标达成度无法计算。"
                            )
                        ),
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 试卷解析失败")
            return _shared._json({"error": str(exc)})

    @tool("extract_syllabus")
    async def extract_syllabus(semester_task_id: str, material_id: str) -> str:
        """从教学大纲材料抽取课程目标与考核权重（LLM 结构化抽取，经校验后落 ce_course_goals）。
        输出未通过校验时转人工，不入库（§6.4 硬规则）。"""
        try:
            operator = _shared._operator_id(runtime_context)
            from sqlalchemy import select

            from backend.application.course_eval.extraction.syllabus_parser import (
                SyllabusExtract,
                extract_with_retry,
            )
            from backend.db.models.course_eval import (
                CeCourseGoal,
                CeMaterial,
            )

            # 授权前置 + 读材料在独立短事务里完成:属主/归档校验必须先于任何
            # 材料读取与 LLM 调用(此前先查材料再校验,非属主能从"材料不存在"
            # 与 AccessDenied 的差异探测他人任务),而 LLM 调用不能持任务行锁。
            async with await _shared._open_session() as session:
                await _shared._load_task(
                    session,
                    semester_task_id,
                    runtime_context=runtime_context,
                    require_editable=True,
                )
                material = (
                    await session.execute(
                        select(CeMaterial).where(
                            CeMaterial.material_id == material_id,
                            CeMaterial.semester_task_id == semester_task_id,
                            CeMaterial.material_type == "syllabus",
                            CeMaterial.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if material is None:
                    return _shared._json(
                        {"error": "未找到该任务的大纲材料，请先上传（文件名含「大纲」）"}
                    )
                data, _name = await _shared._read_material_bytes(
                    session, material.file_id, operator
                )

            # pymupdf/docx 全文解析是纯 CPU 工作,几十页 PDF 百毫秒级、大文件
            # 秒级;留在事件循环上会冻结单 worker Agent API 的全部会话。
            text = await asyncio.to_thread(_shared._extract_text_bytes, data)

            async with await _shared._open_session() as session:

                async def llm_call(prompt_text: str) -> dict:
                    return await _shared._llm_json_call(
                        "从以下课程教学大纲文本抽取结构化信息。\n\n文本:\n"
                        + prompt_text[:12000]
                        + "\n\n请以 JSON 返回："
                        '{"course":{"name":..,"code":..},"score_formula":{"regular_weight":..,'
                        '"exam_weight":..},"goals":[{"code":"目标1","description":..,'
                        '"weight_homework":..,"weight_class":..,"weight_exam":..}]}',
                        temperature=0.0,
                    )

                parsed, issues = await extract_with_retry(text, SyllabusExtract, llm_call)
                if parsed is None:
                    return _shared._json(
                        {
                            "error": "大纲结构化抽取未通过校验，已转人工",
                            "issues": [i.__dict__ for i in issues],
                        }
                    )
                # 写阶段的第一条语句才是加锁读:ce_course_goals 按 course_id 组织,
                # 并发的同课程大纲抽取会竞态覆盖彼此的目标。
                task = await _shared._load_task(
                    session,
                    semester_task_id,
                    runtime_context=runtime_context,
                    for_update=True,
                    require_editable=True,
                )
                existing = {
                    g.goal_code: g
                    for g in (
                        await session.execute(
                            select(CeCourseGoal).where(
                                CeCourseGoal.course_id == task.course_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                }
                goals = parsed.goals if isinstance(parsed.goals, list) else []
                written = 0
                for g in goals:
                    goal = existing.get(getattr(g, "code", ""))
                    if goal is None:
                        goal = CeCourseGoal(
                            course_id=task.course_id,
                            goal_code=getattr(g, "code", ""),
                            source="syllabus_draft",
                        )
                        session.add(goal)
                    goal.description = getattr(g, "description", None)
                    goal.weight_homework = getattr(g, "weight_homework", None)
                    goal.weight_class = getattr(g, "weight_class", None)
                    goal.weight_exam = getattr(g, "weight_exam", None)
                    written += 1
                if written:
                    await task_service.invalidate_downstream(
                        session,
                        semester_task_id,
                        from_stage="extract.syllabus",
                        operator_id=_shared._operator_id(runtime_context),
                    )
                await session.commit()
                return _shared._json(
                    {
                        "written": written,
                        "course": parsed.course.__dict__ if parsed.course else None,
                        "score_formula": (
                            parsed.score_formula.__dict__
                            if parsed.score_formula
                            else None
                        ),
                        "next": "课程目标已入库；映射建议请调用 suggest_goal_mapping。"
                        "向教师汇报时用短句或表格转述目标名称与权重，"
                        "严禁把本 JSON 原文或代码块写进正文",
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 大纲解析失败")
            return _shared._json({"error": str(exc)})

    return extract_grade_sheet, extract_paper, extract_syllabus
