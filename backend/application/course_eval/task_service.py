"""学期任务状态机与编排（M-01）。

状态机是模块级常量表驱动，不允许散落 if-else：
- transition() 是状态迁移唯一入口，非法跃迁抛 CourseEvalStateError（HTTP 409）。
- 三个回退触发点（改分数/重新识别、改映射、改口径配置）统一调用
  invalidate_downstream()，在同一事务内作废快照并标记报告 stale。
- 归档是软锁定：archived -> report_draft 允许回退，回退须走 transition 并写审计。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete as sqlalchemy_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.course_eval.errors import (
    CourseEvalStateError,
    TaskArchived,
    TaskNotFound,
)
from backend.config.loggers import get_logger
from backend.db.models.course_eval import (
    CeAuditLog,
    CeCourse,
    CeReport,
    CeSemesterTask,
    CeStatsSnapshot,
)

logger = get_logger(__name__)

# 状态机常量表：当前状态 -> 合法目标集。allowed_transitions 直接读它。
_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"materials_ready"}),
    "materials_ready": frozenset({"extracting", "draft"}),
    "extracting": frozenset({"needs_review", "mapping_pending", "materials_ready"}),
    "needs_review": frozenset({"mapping_pending", "extracting"}),
    "mapping_pending": frozenset({"computed", "needs_review"}),
    "computed": frozenset({"report_draft", "mapping_pending", "needs_review"}),
    "report_draft": frozenset(
        {"archived", "computed", "mapping_pending", "needs_review"}
    ),
    "archived": frozenset({"report_draft"}),
}

_INITIAL_STATUS = "draft"
_ARCHIVED = "archived"


def allowed_transitions(current: str) -> frozenset[str]:
    """返回当前状态的合法目标集，供前端置灰按钮。"""
    return _TRANSITIONS.get(current, frozenset())


def assert_can_edit_data(task: CeSemesterTask) -> None:
    """已归档任务拒绝改数据；如需修改必须先经 transition 回退。"""
    if task.status == _ARCHIVED:
        raise TaskArchived()


async def _write_audit(
    session: AsyncSession,
    *,
    semester_task_id: str | None,
    operator_id: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict | None = None,
) -> None:
    session.add(
        CeAuditLog(
            semester_task_id=semester_task_id,
            operator_id=operator_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail_json=detail,
        )
    )


async def get_task(
    session: AsyncSession, task_id: str, *, for_update: bool = False
) -> CeSemesterTask:
    stmt = select(CeSemesterTask).where(
        CeSemesterTask.semester_task_id == task_id,
        CeSemesterTask.is_deleted.is_(False),
    )
    if for_update:
        # populate_existing 必不可少:工具入口的无锁属主校验已把任务对象装进
        # 身份映射,默认行为下 FOR UPDATE 拿到行锁、读到最新行,ORM 却返回
        # 旧属性对象——状态机会按持锁前的旧 status 校验(已归档任务可被并发
        # 请求"合法"迁走)。populate_existing 强制用锁定读的最新值刷新对象。
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    task = (await session.execute(stmt)).scalar_one_or_none()
    if task is None:
        raise TaskNotFound()
    return task


async def get_course(session: AsyncSession, course_id: str) -> CeCourse:
    from backend.application.course_eval.errors import CourseNotFound

    course = (
        await session.execute(
            select(CeCourse).where(
                CeCourse.course_id == course_id,
                CeCourse.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if course is None:
        raise CourseNotFound()
    return course


async def create_semester_task(
    session: AsyncSession,
    *,
    course_id: str,
    semester: str,
    class_no: str,
    operator_id: str,
    config: dict | None = None,
) -> CeSemesterTask:
    """建任务，初始状态 draft，写审计。"""
    await get_course(session, course_id)
    task = CeSemesterTask(
        course_id=course_id,
        semester=semester,
        class_no=class_no,
        status=_INITIAL_STATUS,
        owner_user_id=operator_id,
        config_json=config,
    )
    session.add(task)
    await session.flush()
    await _write_audit(
        session,
        semester_task_id=task.semester_task_id,
        operator_id=operator_id,
        action="task.create",
        target_type="semester_task",
        target_id=task.semester_task_id,
        detail={"semester": semester, "class_no": class_no},
    )
    return task


async def transition(
    session: AsyncSession,
    task_id: str,
    target: str,
    *,
    operator_id: str,
    reason: str = "",
) -> CeSemesterTask:
    """状态机唯一入口。非法跃迁抛 CourseEvalStateError（带 allowed_actions）。"""
    task = await get_task(session, task_id, for_update=True)
    allowed = _TRANSITIONS.get(task.status, frozenset())
    if target not in allowed:
        raise CourseEvalStateError(task.status, target, allowed)
    previous_status = task.status
    task.status = target
    task.updated_at = datetime.now()
    await _write_audit(
        session,
        semester_task_id=task.semester_task_id,
        operator_id=operator_id,
        action="task.transition",
        target_type="semester_task",
        target_id=task.semester_task_id,
        # 先捕获旧状态：task.status 已被改为 target，直接读会让审计的 from==to。
        detail={"from": previous_status, "to": target, "reason": reason},
    )
    logger.info(
        "学期任务状态迁移: task_id=%s from=%s to=%s operator=%s",
        task_id,
        previous_status,
        target,
        operator_id,
    )
    return task


async def invalidate_downstream(
    session: AsyncSession,
    task_id: str,
    *,
    from_stage: str,
    operator_id: str,
) -> None:
    """回退触发点统一入口：作废快照 + 报告标记 stale（不删除）。

    由 score_service.update_scores / mapping_service.confirm / config_service.update
    在同一事务内调用，不允许把回退逻辑散落在路由里。
    """
    task = await get_task(session, task_id, for_update=True)
    assert_can_edit_data(task)

    # 作废全部计算快照：单条 DELETE，不把 LONGTEXT 快照正文整行拉进内存。
    delete_result = await session.execute(
        sqlalchemy_delete(CeStatsSnapshot).where(
            CeStatsSnapshot.semester_task_id == task_id
        )
    )

    # 报告标记 stale 而非删除（教师改了一个分数不应丢掉已写好的定性文字）
    reports = (
        (
            await session.execute(
                select(CeReport).where(CeReport.semester_task_id == task_id)
            )
        )
        .scalars()
        .all()
    )
    for report in reports:
        report.stale = True
        report.updated_at = datetime.now()

    await _write_audit(
        session,
        semester_task_id=task_id,
        operator_id=operator_id,
        action="task.invalidate_downstream",
        target_type="semester_task",
        target_id=task_id,
        detail={"from_stage": from_stage},
    )
    logger.info(
        "回退触发作废下游: task_id=%s from_stage=%s snapshots=%d reports=%d",
        task_id,
        from_stage,
        delete_result.rowcount or 0,
        len(reports),
    )
