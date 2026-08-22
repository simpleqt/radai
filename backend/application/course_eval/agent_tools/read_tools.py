"""课程评价只读工具（真实读库，不写任何业务表）。"""

from __future__ import annotations

from langchain_core.tools import tool

from backend.application.course_eval.agent_tools import _shared
from backend.config.loggers import get_logger

logger = get_logger(__name__)


def build_read_tools(runtime_context) -> list:
    """构建 6 个只读工具。"""

    @tool("list_course_eval_tasks")
    async def list_course_eval_tasks(course_id: str = "", limit: int = 20) -> str:
        """列出当前用户可访问的课程评价学期任务（只读）。"""
        try:
            from sqlalchemy import select

            from backend.db.models.course_eval import CeSemesterTask

            user_id, is_admin = _shared._viewer_scope(runtime_context)
            async with await _shared._open_session() as session:
                base = select(CeSemesterTask).where(
                    CeSemesterTask.is_deleted.is_(False)
                )
                if not is_admin:
                    # 非管理员只列自己拥有的任务，杜绝跨教师枚举他人任务与学生 PII
                    base = base.where(CeSemesterTask.owner_user_id == user_id)
                if course_id:
                    base = base.where(CeSemesterTask.course_id == course_id)
                rows = (
                    (
                        await session.execute(
                            base.order_by(CeSemesterTask.created_at.desc())
                            .limit(min(max(limit, 1), 100))
                        )
                    )
                    .scalars()
                    .all()
                )
                items = [
                    {
                        "semester_task_id": t.semester_task_id,
                        "course_id": t.course_id,
                        "semester": t.semester,
                        "class_no": t.class_no,
                        "status": t.status,
                    }
                    for t in rows
                ]
                return _shared._json({"items": items, "total": len(items)})
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 列出任务失败")
            return _shared._json({"error": str(exc)})

    @tool("get_course_stats")
    async def get_course_stats(semester_task_id: str) -> str:
        """读取指定学期任务的成绩分布、得分率快照（只读，须传 semester_task_id）。"""
        try:
            from backend.application.course_eval.report_service import latest_snapshot

            user_id, is_admin = _shared._viewer_scope(runtime_context)
            async with await _shared._open_session() as session:
                await _shared._assert_task_owner(
                    session, semester_task_id, user_id=user_id, is_admin=is_admin
                )
                data = await latest_snapshot(session, semester_task_id)
                return _shared._json(
                    _shared._read_snapshot(data)
                    | {"semester_task_id": semester_task_id}
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 读取统计失败")
            return _shared._json({"error": str(exc)})

    @tool("get_achievement")
    async def get_achievement(semester_task_id: str) -> str:
        """读取达成度结果（只读）。"""
        try:
            from backend.application.course_eval.report_service import (
                compute_achievement,
            )

            user_id, is_admin = _shared._viewer_scope(runtime_context)
            async with await _shared._open_session() as session:
                await _shared._assert_task_owner(
                    session, semester_task_id, user_id=user_id, is_admin=is_admin
                )
                items = await compute_achievement(session, semester_task_id)
                return _shared._json(
                    {"semester_task_id": semester_task_id, "items": items}
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 读取达成度失败")
            return _shared._json({"error": str(exc)})

    @tool("draft_report_narrative")
    async def draft_report_narrative(
        semester_task_id: str, section: str = "problems"
    ) -> str:
        """起草报告定性文字（只产出草稿，**不得包含数字**；数字由渲染器从快照填入）。"""
        try:
            from backend.application.course_eval.report.narrative_drafter import (
                assert_no_digits,
            )

            # 起草是纯 LLM 产物、不读任务数据，但工具签名带 semester_task_id：
            # 仍按属主收敛校验，避免出现一个"接受任意任务 id 却不校验"的工具
            # （授权完整性缺口，也让审计日志里的任务归属自洽）。
            user_id, is_admin = _shared._viewer_scope(runtime_context)
            if str(semester_task_id or "").strip():
                async with await _shared._open_session() as session:
                    await _shared._assert_task_owner(
                        session,
                        semester_task_id,
                        user_id=user_id,
                        is_admin=is_admin,
                    )
            prompt = (
                f"请为课程评价报告的「{section}」部分起草一段定性分析文字，"
                "要求：不出现任何具体数字、百分比、分数；基于课程目标的达成情况给出"
                "问题分析与改进建议；300 字以内。只输出正文。"
            )
            draft = await _shared._llm_json_call(
                prompt + "\n请以 JSON 返回 {\"draft\": \"...\"}"
            )
            text = str(draft.get("draft", "")).strip()
            if text and not assert_no_digits(text):
                raise ValueError("草稿含数字，已拒绝（数字必须由渲染器从快照填入）")
            return _shared._json({"section": section, "draft": text})
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 起草失败")
            return _shared._json({"error": str(exc)})

    @tool("check_report_quality")
    async def check_report_quality(semester_task_id: str) -> str:
        """报告质检（只读；QC-01~QC-10 + 02a/b/c 规则集）。"""
        try:
            from backend.application.course_eval.quality_service import (
                QcContext,
                run_quality_checks,
            )

            user_id, is_admin = _shared._viewer_scope(runtime_context)
            async with await _shared._open_session() as session:
                from backend.application.course_eval.report_service import (
                    latest_snapshot,
                )

                await _shared._assert_task_owner(
                    session, semester_task_id, user_id=user_id, is_admin=is_admin
                )
                data = await latest_snapshot(session, semester_task_id)
                if data is None:
                    return _shared._json({"error": "暂无统计快照，请先执行计算"})
                ctx = QcContext(
                    numbers={
                        "average": str(
                            data.get("final_score_stats", {}).get("average", "")
                        ),
                        "pass_rate": str(
                            data.get("final_score_stats", {}).get("pass_rate", "")
                        ),
                    }
                )
                issues = run_quality_checks(ctx)
                return _shared._json(
                    {
                        "semester_task_id": semester_task_id,
                        "issues": [i.__dict__ for i in issues],
                        "passed": not issues,
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 质检失败")
            return _shared._json({"error": str(exc)})

    @tool("load_previous_reports")
    async def load_previous_reports(course_id: str = "") -> str:
        """历史达成度与措施摘要（只读；**不返回历史报告正文**，防套话复用 §6.4）。"""
        try:
            from sqlalchemy import select

            from backend.db.models.course_eval import CeCourse, CeCourseGoal

            user_id, is_admin = _shared._viewer_scope(runtime_context)
            if not str(course_id or "").strip():
                return _shared._json({"error": "course_id 不能为空"})
            async with await _shared._open_session() as session:
                # 课程档案按教师隔离：没有这层校验，任一登录教师用猜到/枚举到的
                # course_id 就能读他人课程的目标与考核权重结构。
                if not is_admin:
                    owner = (
                        await session.execute(
                            select(CeCourse.owner_user_id).where(
                                CeCourse.course_id == course_id,
                                CeCourse.is_deleted.is_(False),
                            )
                        )
                    ).scalar_one_or_none()
                    if owner is None or str(owner or "") != user_id:
                        from backend.application.course_eval.errors import (
                            CourseEvalAccessDenied,
                        )

                        raise CourseEvalAccessDenied()
                goal_rows = (
                    (
                        await session.execute(
                            select(CeCourseGoal).where(
                                CeCourseGoal.course_id == course_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return _shared._json(
                    {
                        "course_id": course_id,
                        "goals": [
                            {
                                "goal_code": g.goal_code,
                                "weight_exam": str(g.weight_exam)
                                if g.weight_exam is not None
                                else None,
                            }
                            for g in goal_rows
                        ],
                        "note": "仅返回目标与权重摘要，历史报告正文不入生成上下文",
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("[CourseEvalTool] 读取历史报告失败")
            return _shared._json({"error": str(exc)})

    return [
        list_course_eval_tasks,
        get_course_stats,
        get_achievement,
        draft_report_narrative,
        check_report_quality,
        load_previous_reports,
    ]
