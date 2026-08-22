"""Tests for mapping service coverage checks (T3-02).

validate_coverage 覆盖三条规则（题目全归属、目标全支撑、权重和为 1）+
大纲期望偏差；每条规则正反各一例。
"""

from __future__ import annotations

from decimal import Decimal

from backend.application.course_eval.mapping_service import validate_coverage

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.application.course_eval import mapping_service, task_service
from backend.db.models.course_eval import (
    CeAuditLog,
    CeCourse,
    CeCourseGoal,
    CeExamPaper,
    CeExamSection,
    CeGoalMapping,
    CeReport,
    CeSemesterTask,
    CeStatsSnapshot,
)

SECTIONS = [
    {
        "section_id": "sec1",
        "order_no": 1,
        "report_order_no": 1,
        "question_type": "填空题",
        "total_score": 20,
    },
    {
        "section_id": "sec2",
        "order_no": 2,
        "report_order_no": 2,
        "question_type": "证明题",
        "total_score": 10,
    },
    {
        "section_id": "sec3",
        "order_no": 3,
        "report_order_no": 3,
        "question_type": "作图题",
        "total_score": 20,
    },
    {
        "section_id": "sec4",
        "order_no": 4,
        "report_order_no": 4,
        "question_type": "计算题",
        "total_score": 50,
    },
]
ITEMS = [
    {"item_id": "i1", "section_id": "sec1", "order_no": 1, "full_score": 2},
    {"item_id": "i2", "section_id": "sec1", "order_no": 2, "full_score": 2},
]
GOALS = [
    {"goal_id": "g1", "goal_code": "目标1", "weight_exam": 30},
    {"goal_id": "g2", "goal_code": "目标2", "weight_exam": 30},
]


def _full_mappings() -> list[dict]:
    return [
        {"section_id": "sec1", "item_id": "", "goal_id": "g1", "weight": 1},
        {"section_id": "sec2", "item_id": "", "goal_id": "g1", "weight": 1},
        {"section_id": "sec3", "item_id": "", "goal_id": "g1", "weight": 1},
        {"section_id": "sec4", "item_id": "", "goal_id": "g2", "weight": 1},
    ]


def test_complete_mapping_no_violations():
    """完整映射：无未映射、目标全支撑、权重和=1、期望偏差 ok（§5.4 示例）。"""
    check = validate_coverage(SECTIONS, ITEMS, _full_mappings(), GOALS)
    assert check.unmapped_sections == []
    assert check.unmapped_items == []
    assert check.goals_without_support == []
    assert check.weight_sum_violations == []
    # 目标1 支撑 20+10+20=50 分，大纲期望 100×30/60=50 → ok
    goal1 = next(x for x in check.goal_score_vs_syllabus if x["goal_code"] == "目标1")
    assert goal1["mapped_score"] == Decimal(50)
    assert goal1["expected_score"] == Decimal(50)
    assert goal1["level"] == "ok"


def test_unmapped_section_detected():
    """题目未归属：sec2 无映射。"""
    mappings = [m for m in _full_mappings() if m["section_id"] != "sec2"]
    check = validate_coverage(SECTIONS, ITEMS, mappings, GOALS)
    assert "sec2" in check.unmapped_sections


def test_unmapped_item_detected():
    """小题未归属：sec1 无大题级映射，i2 也无小题映射 → i2 未映射。"""
    # sec1 只做小题级映射 i1，不做大题级；sec2-4 保持大题级
    mappings = [m for m in _full_mappings() if m["section_id"] != "sec1"]
    mappings.append(
        {"section_id": "sec1", "item_id": "i1", "goal_id": "g1", "weight": 1}
    )
    check = validate_coverage(SECTIONS, ITEMS, mappings, GOALS)
    assert "i2" in check.unmapped_items
    assert "i1" not in check.unmapped_items  # i1 有直接映射


def test_goal_without_support_detected():
    """目标全支撑：g2 无支撑题。"""
    mappings = [m for m in _full_mappings() if m["goal_id"] != "g2"]
    check = validate_coverage(SECTIONS, ITEMS, mappings, GOALS)
    assert "g2" in check.goals_without_support


def test_weight_sum_violation_detected():
    """权重和 ≠ 1 被拦截（§6.5 / T3-02 DoD）。"""
    mappings = _full_mappings()
    mappings[0] = {"section_id": "sec1", "item_id": "", "goal_id": "g1", "weight": 0.8}
    check = validate_coverage(SECTIONS, ITEMS, mappings, GOALS)
    assert len(check.weight_sum_violations) == 1
    assert check.weight_sum_violations[0]["section_id"] == "sec1"
    assert abs(check.weight_sum_violations[0]["sum"] - Decimal("0.8")) < Decimal(
        "0.001"
    )


def test_goal_score_vs_syllabus_deviation():
    """映射分值偏离大纲期望 >20% → warn；>40% → error（§6.5 _deviation 阈值）。"""
    # 把 50 分目标1 只映射到 20 分 → 偏差 60% → error
    mappings = [
        {"section_id": "sec1", "item_id": "", "goal_id": "g1", "weight": 1},
        {"section_id": "sec4", "item_id": "", "goal_id": "g2", "weight": 1},
    ]
    check = validate_coverage(SECTIONS, ITEMS, mappings, GOALS)
    goal1 = next(x for x in check.goal_score_vs_syllabus if x["goal_code"] == "目标1")
    assert goal1["level"] == "error"
    assert goal1["deviation_pct"] == Decimal(60)


# ── 映射服务 apply/list 业务流（SQLite 内存库）──


@pytest.fixture
async def mapping_env():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        for t in (
            CeCourse.__table__,
            CeSemesterTask.__table__,
            CeCourseGoal.__table__,
            CeExamPaper.__table__,
            CeExamSection.__table__,
            CeGoalMapping.__table__,
            CeStatsSnapshot.__table__,
            CeReport.__table__,
            CeAuditLog.__table__,
        ):
            await conn.run_sync(t.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        course = CeCourse(code="C1", name="材料力学", owner_user_id="u1")
        session.add(course)
        await session.flush()
        task = CeSemesterTask(
            course_id=course.course_id,
            semester="2024-2025-1",
            class_no="c1",
            status="computed",
            owner_user_id="u1",
        )
        session.add(task)
        await session.flush()
        goal = CeCourseGoal(
            course_id=course.course_id, goal_code="目标1", weight_exam=30
        )
        session.add(goal)
        await session.flush()
        paper = CeExamPaper(semester_task_id=task.semester_task_id)
        session.add(paper)
        await session.flush()
        sec = CeExamSection(
            paper_id=paper.paper_id,
            order_no=1,
            report_order_no=1,
            question_type="填空题",
            total_score=20,
        )
        session.add(sec)
        await session.flush()
        await session.commit()
        ids = {
            "task_id": task.semester_task_id,
            "goal_id": goal.goal_id,
            "sec_id": sec.section_id,
        }
    yield factory, ids
    await engine.dispose()


async def test_apply_mappings_marks_confirmed_and_invalidates(mapping_env):
    """批量落库映射：confirmed_by 标记 + 快照作废 + 报告 stale（回退触发）。"""
    factory, ids = mapping_env
    async with factory() as session:
        task = await task_service.get_task(session, ids["task_id"])
        task.status = "computed"
        await session.flush()
        session.add(CeStatsSnapshot(semester_task_id=ids["task_id"], result_json="{}"))
        session.add(
            CeReport(
                semester_task_id=ids["task_id"],
                template_id="t1",
                report_type="paper_analysis",
            )
        )
        await session.flush()

        count = await mapping_service.apply_mappings(
            session,
            task_id=ids["task_id"],
            operator_id="u1",
            items=[
                {
                    "goal_id": ids["goal_id"],
                    "section_id": ids["sec_id"],
                    "item_id": "",
                    "weight": 1,
                }
            ],
        )
        assert count == 1
        await session.flush()

        # confirmed_by 标记
        mapping = (
            await session.execute(
                select(CeGoalMapping).where(
                    CeGoalMapping.semester_task_id == ids["task_id"]
                )
            )
        ).scalar_one()
        assert mapping.confirmed_by == "u1"
        # 快照作废
        snap_count = len(
            (
                await session.execute(
                    select(CeStatsSnapshot).where(
                        CeStatsSnapshot.semester_task_id == ids["task_id"]
                    )
                )
            )
            .scalars()
            .all()
        )
        assert snap_count == 0
        # 报告 stale
        report = (
            await session.execute(
                select(CeReport).where(CeReport.semester_task_id == ids["task_id"])
            )
        ).scalar_one()
        assert report.stale is True


async def test_apply_mappings_replaces_old(mapping_env):
    """重复应用映射：旧映射整任务级删除后重写，无重复行。"""
    factory, ids = mapping_env
    async with factory() as session:
        await mapping_service.apply_mappings(
            session,
            task_id=ids["task_id"],
            operator_id="u1",
            items=[
                {
                    "goal_id": ids["goal_id"],
                    "section_id": ids["sec_id"],
                    "item_id": "",
                    "weight": 1,
                }
            ],
        )
        await mapping_service.apply_mappings(
            session,
            task_id=ids["task_id"],
            operator_id="u1",
            items=[
                {
                    "goal_id": ids["goal_id"],
                    "section_id": ids["sec_id"],
                    "item_id": "",
                    "weight": 0.5,
                }
            ],
        )
        await session.flush()
        rows = (
            (
                await session.execute(
                    select(CeGoalMapping).where(
                        CeGoalMapping.semester_task_id == ids["task_id"]
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].weight == 0.5


async def test_list_mappings_returns_checks(mapping_env):
    factory, ids = mapping_env
    async with factory() as session:
        await mapping_service.apply_mappings(
            session,
            task_id=ids["task_id"],
            operator_id="u1",
            items=[
                {
                    "goal_id": ids["goal_id"],
                    "section_id": ids["sec_id"],
                    "item_id": "",
                    "weight": 1,
                }
            ],
        )
        mappings, checks = await mapping_service.list_mappings(session, ids["task_id"])
        assert len(mappings) == 1
        assert checks.unmapped_sections == []  # 唯一大题已映射
