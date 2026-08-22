"""课程评价对话式工具：建任务/绑材料/读网格/改分/确认得分/确认映射。"""

from __future__ import annotations

import uuid

from langchain_core.tools import tool

from backend.application.course_eval.agent_tools import _shared
from backend.config.loggers import get_logger

logger = get_logger(__name__)


def build_dialog_tools(runtime_context):
    """构建对话式 6 工具（均需登录教师身份）。"""

    @tool("create_semester_task")
    async def create_semester_task(
        course_name: str,
        course_code: str = "",
        semester: str = "",
        class_no: str = "",
    ) -> str:
        """创建（或复用）课程并新建学期任务。对话入口第一步：
        从用户描述中提取课程名称、学期（如 2024-2025-1）、教学班号后调用。
        course_code 可选：用户未提供时自动生成唯一代码（auto_generated=true）。"""
        try:
            operator = _shared._operator_id(runtime_context)
            if not (course_name and semester and class_no):
                return _shared._json({"error": "课程名称/学期/教学班号均不能为空"})
            auto_generated = not str(course_code or "").strip()
            if auto_generated:
                course_code = f"AUTO-{uuid.uuid4().hex[:8].upper()}"
            from sqlalchemy import select

            from backend.application.course_eval import task_service
            from backend.db.models.course_eval import CeCourse, CeSemesterTask

            is_admin = _shared._is_admin_role(runtime_context)
            async with await _shared._open_session() as session:
                # ``code`` is globally unique, so a course row must only be
                # reused by its owner: two teachers entering the same course
                # code would otherwise share one course_id, and
                # extract_syllabus (which writes ce_course_goals keyed by
                # course_id) would overwrite the other teacher's goals.
                course = (
                    await session.execute(
                        select(CeCourse).where(
                            CeCourse.name == course_name,
                            CeCourse.code == course_code,
                            CeCourse.is_deleted.is_(False),
                        )
                    )
                ).scalars().first()
                if course is not None and not is_admin:
                    course_owner = str(getattr(course, "owner_user_id", "") or "")
                    if course_owner != operator:
                        return _shared._json(
                            {
                                "error": (
                                    f"课程代码 {course_code} 已被其他教师使用，"
                                    "请改用其他课程代码（留空可自动生成唯一代码）。"
                                ),
                                "error_code": "course_code_owned_by_other",
                            }
                        )
                if course is None:
                    course = CeCourse(
                        code=course_code,
                        name=course_name,
                        owner_user_id=operator,
                    )
                    session.add(course)
                    await session.flush()
                # ce_semester_tasks 有 (course_id, semester, class_no) 唯一约束：
                # 已存在同课程同学期同班级任务时直接复用（幂等）
                existing_task = (
                    await session.execute(
                        select(CeSemesterTask).where(
                            CeSemesterTask.course_id == course.course_id,
                            CeSemesterTask.semester == semester,
                            CeSemesterTask.class_no == class_no,
                            CeSemesterTask.is_deleted.is_(False),
                        )
                    )
                ).scalars().first()
                if existing_task is not None:
                    # Idempotent reuse must still respect ownership: returning
                    # another teacher's semester_task_id both leaks that the
                    # combination exists and binds this session to a task every
                    # later tool call would reject.
                    if not is_admin and str(
                        getattr(existing_task, "owner_user_id", "") or ""
                    ) != operator:
                        return _shared._json(
                            {
                                "error": (
                                    "该课程/学期/教学班的任务归属其他教师，无法复用。"
                                ),
                                "error_code": "semester_task_owned_by_other",
                            }
                        )
                    await session.commit()
                    return _shared._json(
                        {
                            "semester_task_id": existing_task.semester_task_id,
                            "course_id": course.course_id,
                            "course_code": course.code,
                            "course_name": course.name,
                            "semester": existing_task.semester,
                            "class_no": existing_task.class_no,
                            "status": existing_task.status,
                            "reused": True,
                            "next": "已复用现有任务；对每个上传的文件调用 attach_material 绑定材料",
                        }
                    )
                task = await task_service.create_semester_task(
                    session,
                    course_id=course.course_id,
                    semester=semester,
                    class_no=class_no,
                    operator_id=operator,
                )
                await session.commit()
                return _shared._json(
                    {
                        "semester_task_id": task.semester_task_id,
                        "course_id": course.course_id,
                        "course_code": course.code,
                        "course_name": course.name,
                        "semester": task.semester,
                        "class_no": task.class_no,
                        "status": task.status,
                        "auto_generated": auto_generated,
                        "next": "任务已创建；对每个上传的文件调用 attach_material 绑定材料",
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 创建任务失败")
            return _shared._json({"error": str(exc)})

    @tool("attach_material")
    async def attach_material(
        semester_task_id: str, file_id: str, material_type: str = ""
    ) -> str:
        """把对话上传的文件绑定为任务材料。material_type 缺省时按文件名自动识别
        （含「成绩单」=grade_sheet、含「大纲」=syllabus、含「试卷」且 PDF=paper）。"""
        try:
            operator = _shared._operator_id(runtime_context)
            from backend.application.course_eval import material_service

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
                row, allowed, reason = await _shared._resolve_file_display(
                    session, file_id, operator
                )
                if not allowed:
                    return _shared._json(
                        {"error": f"无权访问该文件（{reason}），请确认文件已上传到本对话"}
                    )
                effective_type = material_type or _shared._auto_material_type(
                    row.display_name or "", row.mime_type
                )
                if effective_type == "other":
                    return _shared._json(
                        {
                            "error": "无法识别材料类型：文件名应含「成绩单」「大纲」，"
                            "试卷需为 PDF 且文件名含「试卷」；或显式传入 material_type"
                        }
                    )
                material = await material_service.register_material(
                    session,
                    task_id=semester_task_id,
                    material_type=effective_type,
                    file_id=file_id,
                    uploader_id=operator,
                )
                await session.commit()
                return _shared._json(
                    {
                        "material_id": material.material_id,
                        "material_type": effective_type,
                        "version_no": material.version_no,
                        "next": f"已绑定{effective_type}；继续绑定其余材料，完成后调用 "
                        "extract_grade_sheet/extract_paper/extract_syllabus 解析",
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 绑定材料失败")
            return _shared._json({"error": str(exc)})

    @tool("get_score_grid")
    async def get_score_grid(semester_task_id: str) -> str:
        """读取分数网格摘要（存疑单元格 + 行校验失败），供对话中核对与修正。"""
        try:
            from backend.application.course_eval.score_service import list_score_grid

            user_id, is_admin = _shared._viewer_scope(runtime_context)
            async with await _shared._open_session() as session:
                await _shared._assert_task_owner(
                    session, semester_task_id, user_id=user_id, is_admin=is_admin
                )
                grid = await list_score_grid(session, semester_task_id)
                name_by_id = {
                    s["student_id"]: s.get("name", "")
                    for s in grid.get("students", [])
                }
                section_label = {
                    s["section_id"]: (
                        f"第{s.get('order_no')}题·{s.get('question_type') or ''}".rstrip(
                            "·"
                        )
                    )
                    for s in grid.get("sections", [])
                }
                # 只列还处于待核对的格子：教师确认过（confirmed）的低置信度
                # 识别值已经拍板，不再反复要求核对
                cells = []
                for c in grid.get("cells", []):
                    if c.get("status") != "pending_review":
                        continue
                    cells.append(
                        {
                            "student_id": c["student_id"],
                            "student_name": name_by_id.get(c["student_id"], ""),
                            "section_id": c["section_id"],
                            "section_label": section_label.get(c["section_id"], ""),
                            "item_id": c.get("item_id", ""),
                            "score": str(c["score"]),
                            "raw_score": str(c["raw_score"])
                            if c.get("raw_score") is not None
                            else None,
                            "confidence": str(c.get("confidence") or ""),
                        }
                    )
                row_issues = [
                    {
                        "student_id": r["student_id"],
                        "student_name": name_by_id.get(r["student_id"], ""),
                        "sum_of_sections": str(r["sum_of_sections"]),
                        "reported_total": str(r["reported_total"]),
                    }
                    for r in grid.get("row_checks", [])
                    if not r.get("passed")
                ]
                return _shared._json(
                    {
                        "semester_task_id": semester_task_id,
                        "students": len(grid.get("students", [])),
                        "sections": len(grid.get("sections", [])),
                        "low_confidence_cells": cells,
                        "row_checks_failed": row_issues,
                        "next": (
                            "向用户展示存疑项（用学生姓名和第X题，不要出现内部 ID）；"
                            "用户指出修正时调用 update_scores，认可识别值时调用 confirm_scores"
                        ),
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 读取分数网格失败")
            return _shared._json({"error": str(exc)})

    @tool("update_scores")
    async def update_scores(
        semester_task_id: str,
        cells: list[dict],
    ) -> str:
        """按用户指正修正分数（source=manual、confidence=1.0、写审计、触发下游回退）。
        cells: [{student_id, section_id, item_id?, score}]"""
        try:
            operator = _shared._operator_id(runtime_context)
            from backend.application.course_eval import score_service

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
                updated = await score_service.update_scores(
                    session,
                    task_id=semester_task_id,
                    cells=cells,
                    operator_id=operator,
                )
                await session.commit()
                return _shared._json(
                    {
                        "semester_task_id": semester_task_id,
                        "updated": updated,
                        "next": "分数已修正并触发统计回退；可继续 run_validation 或推进流水线",
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 修正分数失败")
            return _shared._json({"error": str(exc)})

    @tool("confirm_scores")
    async def confirm_scores(
        semester_task_id: str,
        cells: list[dict] | None = None,
    ) -> str:
        """教师认可自动识别的得分时把待核对项置为已确认（不改分值）。
        cells 省略=确认全部待核对；否则 [{student_id, section_id, item_id?}]。
        教师给出的是**修正值**时用 update_scores，不要用本工具。"""
        try:
            operator = _shared._operator_id(runtime_context)
            from backend.application.course_eval import score_service

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
                confirmed = await score_service.confirm_scores(
                    session,
                    task_id=semester_task_id,
                    cells=cells,
                    operator_id=operator,
                )
                await session.commit()
                return _shared._json(
                    {
                        "semester_task_id": semester_task_id,
                        "confirmed": confirmed,
                        "next": (
                            "待核对得分已按教师确认置为 confirmed；"
                            "可继续 run_validation 或推进到映射环节"
                        ),
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 确认分数失败")
            return _shared._json({"error": str(exc)})

    @tool("apply_mappings")
    async def apply_mappings(
        semester_task_id: str,
        mappings: list[dict],
    ) -> str:
        """按用户确认的映射建议落库（整任务级重写，标 confirmed_by、触发下游回退）。
        mappings: [{goal_id, section_id, item_id?, weight}]"""
        try:
            operator = _shared._operator_id(runtime_context)
            from backend.application.course_eval import mapping_service

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
                updated = await mapping_service.apply_mappings(
                    session,
                    task_id=semester_task_id,
                    items=mappings,
                    operator_id=operator,
                )
                await session.commit()
                return _shared._json(
                    {
                        "semester_task_id": semester_task_id,
                        "updated": updated,
                        "next": "映射已确认落库；可 transition_task 至 computed 并调用 "
                        "compute_statistics",
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 确认映射失败")
            return _shared._json({"error": str(exc)})

    return [
        create_semester_task,
        attach_material,
        get_score_grid,
        update_scores,
        confirm_scores,
        apply_mappings,
    ]
