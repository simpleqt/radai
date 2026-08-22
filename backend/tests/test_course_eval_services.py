"""Tests for course-eval services (compute/pdf_pager).

覆盖 report_service.compute / latest_snapshot / compute_achievement、
pdf_pager 裁剪与水印。归档/改进台账已随旧工作台移除。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.application.course_eval import report_service
from backend.application.course_eval.extraction import pdf_pager
from backend.db.models.course_eval import (
    CeAchievementConfig,
    CeAuditLog,
    CeCourse,
    CeCourseGoal,
    CeExamItem,
    CeExamPaper,
    CeExamSection,
    CeGoalMapping,
    CeMaterial,
    CeReport,
    CeScoreRecord,
    CeSemesterTask,
    CeStatsSnapshot,
    CeStudent,
)

_ALL_TABLES = [
    CeCourse.__table__,
    CeSemesterTask.__table__,
    CeCourseGoal.__table__,
    CeExamPaper.__table__,
    CeExamSection.__table__,
    CeExamItem.__table__,
    CeStudent.__table__,
    CeScoreRecord.__table__,
    CeGoalMapping.__table__,
    CeAchievementConfig.__table__,
    CeMaterial.__table__,
    CeReport.__table__,
    CeStatsSnapshot.__table__,
    CeAuditLog.__table__,
]


@pytest.fixture
async def env():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        for table in _ALL_TABLES:
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        course = CeCourse(code="2021L1609", name="材料力学", owner_user_id="u1")
        session.add(course)
        await session.flush()
        course_id = course.course_id
        goal1 = CeCourseGoal(course_id=course_id, goal_code="目标1", weight_exam=30)
        goal2 = CeCourseGoal(course_id=course_id, goal_code="目标2", weight_exam=30)
        session.add_all([goal1, goal2])
        await session.flush()
        session.add(CeAchievementConfig(course_id=course_id, active=True))
        task = CeSemesterTask(
            course_id=course_id,
            semester="2024-2025-1",
            class_no="c1",
            status="computed",
            owner_user_id="u1",
            expected_students=2,
        )
        session.add(task)
        await session.flush()
        task_id = task.semester_task_id
        paper = CeExamPaper(semester_task_id=task_id)
        session.add(paper)
        await session.flush()
        sec1 = CeExamSection(
            paper_id=paper.paper_id,
            order_no=1,
            report_order_no=1,
            question_type="填空题",
            total_score=20,
        )
        sec2 = CeExamSection(
            paper_id=paper.paper_id,
            order_no=2,
            report_order_no=2,
            question_type="计算题",
            total_score=50,
        )
        session.add_all([sec1, sec2])
        await session.flush()
        stu1 = CeStudent(
            semester_task_id=task_id,
            student_no="990203000203",
            name="学生甲",
            regular_score=80,
            final_score=60,
            total_score=68,
        )
        stu2 = CeStudent(
            semester_task_id=task_id,
            student_no="990203000204",
            name="学生乙",
            regular_score=90,
            final_score=80,
            total_score=86,
        )
        session.add_all([stu1, stu2])
        await session.flush()
        session.add_all(
            [
                CeScoreRecord(
                    semester_task_id=task_id,
                    student_id=stu1.student_id,
                    section_id=sec1.section_id,
                    item_id="",
                    full_score=20,
                    score=12,
                    source="ocr_table",
                    confidence="0.95",
                    status="confirmed",
                ),
                CeScoreRecord(
                    semester_task_id=task_id,
                    student_id=stu1.student_id,
                    section_id=sec2.section_id,
                    item_id="",
                    full_score=50,
                    score=30,
                ),
                CeScoreRecord(
                    semester_task_id=task_id,
                    student_id=stu2.student_id,
                    section_id=sec1.section_id,
                    item_id="",
                    full_score=20,
                    score=16,
                ),
                CeScoreRecord(
                    semester_task_id=task_id,
                    student_id=stu2.student_id,
                    section_id=sec2.section_id,
                    item_id="",
                    full_score=50,
                    score=40,
                ),
                CeGoalMapping(
                    semester_task_id=task_id,
                    goal_id=goal1.goal_id,
                    section_id=sec1.section_id,
                    item_id="",
                    weight=1,
                ),
                CeGoalMapping(
                    semester_task_id=task_id,
                    goal_id=goal2.goal_id,
                    section_id=sec2.section_id,
                    item_id="",
                    weight=1,
                ),
            ]
        )
        await session.commit()
        ids = {
            "task_id": task_id,
            "course_id": course_id,
            "stu1": stu1.student_id,
            "stu2": stu2.student_id,
            "sec1": sec1.section_id,
            "sec2": sec2.section_id,
        }
    yield factory, ids
    await engine.dispose()


async def test_compute_and_latest_snapshot(env):
    factory, ids = env
    async with factory() as session:
        snap = await report_service.compute(
            session, task_id=ids["task_id"], operator_id="u1"
        )
        assert snap.snapshot_id
        data = await report_service.latest_snapshot(session, ids["task_id"])
        assert data is not None
        # 样本量是有逐题得分的学生数（2 人 × 2 题 = 4 条记录）
        assert data["scope"]["paper_sample_n"] == 2
        assert data["final_score_stats"] is not None
        # 缺快照时返回 None
        assert await report_service.latest_snapshot(session, "missing") is None


async def test_compute_achievement(env):
    factory, ids = env
    async with factory() as session:
        items = await report_service.compute_achievement(session, ids["task_id"])
        assert len(items) == 2
        assert all(item["goal_code"] in ("目标1", "目标2") for item in items)
        assert all(item["blocking"] is None for item in items)


def test_pdf_pager_watermark():
    assert pdf_pager.is_watermark_image(203, 69) is True
    assert pdf_pager.is_watermark_image(3050, 2200) is False


def test_pdf_pager_render_synthetic_pdf():
    """合成 PDF 验证渲染尺寸与裁剪（无真实资料）。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)  # A4 横向
    page.insert_text((50, 100), "hello scan", fontsize=24)
    data = doc.tobytes()
    doc.close()

    pages = pdf_pager.render_pages(data, dpi=150)
    assert len(pages) == 1
    assert pages[0].is_spread is True  # 宽 > 高
    # PyMuPDF 把 pt→px 取整到 1755x1240（§6.3.0 实测值）
    assert pages[0].gray.shape == (1240, 1755)

    crop = pdf_pager.crop_region(data, 0, pdf_pager.NormBox(0, 0, 0.5, 0.5), dpi=300)
    assert len(crop) > 100  # PNG 字节非空

    png = pdf_pager.render_page_to_png(data, 0)
    assert png.startswith(b"\x89PNG")
