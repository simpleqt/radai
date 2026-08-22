"""Tests for the course-eval task state machine (T1-04).

遍历 _TRANSITIONS 全矩阵断言非法迁移全部抛错；断言三个回退触发点都会作废快照。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.application.course_eval import task_service
from backend.application.course_eval.errors import CourseEvalStateError, TaskArchived
from backend.db.models.course_eval import (
    CeAuditLog,
    CeCourse,
    CeMaterial,
    CeReport,
    CeSemesterTask,
    CeStatsSnapshot,
)


@pytest.fixture
async def engine_and_factory():
    # SQLite :memory: 按连接隔离；StaticPool 让所有 session 共享同一内存库
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        for table in (
            CeCourse.__table__,
            CeSemesterTask.__table__,
            CeStatsSnapshot.__table__,
            CeReport.__table__,
            CeAuditLog.__table__,
            CeMaterial.__table__,
        ):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, factory
    await engine.dispose()


@pytest.fixture
async def seeded(engine_and_factory):
    engine, factory = engine_and_factory
    async with factory() as session:
        course = CeCourse(
            code="2021L1609",
            name="材料力学",
            owner_user_id="u1",
        )
        session.add(course)
        await session.flush()
        task = CeSemesterTask(
            course_id=course.course_id,
            semester="2024-2025-1",
            class_no="990103-4",
            status="draft",
            owner_user_id="u1",
        )
        session.add(task)
        await session.flush()
        result = {"course_id": course.course_id, "task_id": task.semester_task_id}
        await session.commit()  # 跨 session 可见需要提交
        return result


def test_transitions_table_is_module_level_constant():
    """_TRANSITIONS 是模块级常量，allowed_transitions 直接读它（T1-04 DoD 第 1 条）。"""
    from backend.application.course_eval.task_service import _TRANSITIONS

    assert isinstance(_TRANSITIONS, dict)
    assert _TRANSITIONS["draft"] == frozenset({"materials_ready"})
    assert "archived" in _TRANSITIONS


def test_allowed_transitions_reads_table():
    for state, targets in task_service._TRANSITIONS.items():
        assert task_service.allowed_transitions(state) == targets


def test_initial_status_is_draft():
    assert task_service._INITIAL_STATUS == "draft"


async def test_full_matrix_illegal_transitions_rejected(engine_and_factory, seeded):
    """全矩阵：对每个状态遍历所有目标，凡不在允许集的一律抛 CourseEvalStateError。"""
    engine, factory = engine_and_factory
    for current, allowed in task_service._TRANSITIONS.items():
        for target in task_service._TRANSITIONS:
            if target in allowed:
                continue
            async with factory() as session:
                task = CeSemesterTask(
                    course_id=seeded["course_id"],
                    semester="s",
                    class_no="c",
                    status=current,
                    owner_user_id="u1",
                )
                session.add(task)
                await session.flush()
                with pytest.raises(CourseEvalStateError) as exc_info:
                    await task_service.transition(
                        session, task.semester_task_id, target, operator_id="u1"
                    )
                assert exc_info.value.current == current
                assert exc_info.value.allowed == allowed


async def test_legal_transition_advances(engine_and_factory, seeded):
    engine, factory = engine_and_factory
    async with factory() as session:
        task = await task_service.get_task(session, seeded["task_id"])
        task.status = "materials_ready"
        await session.flush()
        updated = await task_service.transition(
            session, task.semester_task_id, "extracting", operator_id="u1"
        )
        assert updated.status == "extracting"


async def test_invalidate_downstream_clears_snapshots_and_marks_stale(
    engine_and_factory, seeded
):
    """三个回退触发点统一调 invalidate_downstream：作废快照 + 报告标记 stale。"""
    engine, factory = engine_and_factory
    async with factory() as session:
        task = await task_service.get_task(session, seeded["task_id"])
        task_id = task.semester_task_id
        task.status = "computed"
        await session.flush()
        snapshot = CeStatsSnapshot(
            semester_task_id=task_id,
            result_json="{}",
        )
        report = CeReport(
            semester_task_id=task_id,
            template_id="t1",
            report_type="paper_analysis",
            status="draft",
        )
        session.add_all([snapshot, report])
        await session.flush()

        await task_service.invalidate_downstream(
            session, task_id, from_stage="score.update", operator_id="u1"
        )
        await session.flush()
        session.expire_all()

        from sqlalchemy import func, select

        snap_count = (
            await session.execute(
                select(func.count())
                .select_from(CeStatsSnapshot)
                .where(CeStatsSnapshot.semester_task_id == task_id)
            )
        ).scalar_one()
        assert snap_count == 0, "快照应被作废删除"

        stale_report = (
            await session.execute(
                select(CeReport).where(CeReport.semester_task_id == task_id)
            )
        ).scalar_one()
        assert stale_report.stale is True, "报告应标记 stale 而非删除"


async def test_archived_rejects_edit(engine_and_factory, seeded):
    """归档后改数据抛 TaskArchived（409 语义）；可经 transition 回退。"""
    engine, factory = engine_and_factory
    async with factory() as session:
        task = await task_service.get_task(session, seeded["task_id"])
        task.status = "archived"
        await session.flush()
        with pytest.raises(TaskArchived):
            task_service.assert_can_edit_data(task)

        # 软锁定：archived -> report_draft 允许回退
        updated = await task_service.transition(
            session, task.semester_task_id, "report_draft", operator_id="u1"
        )
        assert updated.status == "report_draft"
        task_service.assert_can_edit_data(updated)  # 不再抛错
