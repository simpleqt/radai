"""课程试卷管线:OCR 结构解析、封面对齐、成绩写入/确认、达成度与报告渲染。(拆自 test_course_eval_agent_tools.py)"""


from __future__ import annotations

import json
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.domain.tools.runtime_context import ToolRuntimeContext
from backend.db.models import User
from backend.db.models.course_eval import (
    CeAchievementConfig,
    CeAuditLog,
    CeCourse,
    CeReportTemplate,
    CeReportVersion,
    CeExamItem,
    CeExamPaper,
    CeExamSection,
    CeMaterial,
    CeScoreRecord,
    CeSemesterTask,
    CeStatsSnapshot,
    CeStudent,
    CeCourseGoal,
    CeGoalMapping,
    CeReport,
)


# ── 对话式工具（智能体自动建任务/绑材料/确认）──


# ── 回归：E2E 实测暴露的 4 个 bug（2026-08-12）──


# ── 一致性修复回归（2026-08-12 审计）──


# ── 试卷结构与逐题得分提取（MinerU 单通道 + 总分交叉校验）──


@pytest.fixture
async def db_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        for table in (
            User.__table__,
            CeCourse.__table__,
            CeSemesterTask.__table__,
            CeAuditLog.__table__,
            CeMaterial.__table__,
            CeExamPaper.__table__,
            CeExamSection.__table__,
            CeExamItem.__table__,
            CeStudent.__table__,
            CeScoreRecord.__table__,
            CeStatsSnapshot.__table__,
            CeCourseGoal.__table__,
            CeGoalMapping.__table__,
            CeAchievementConfig.__table__,
            CeReport.__table__,
            CeReportTemplate.__table__,
            CeReportVersion.__table__,
        ):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def ctx():
    return ToolRuntimeContext(
        request_id="r1",
        user_id="u1",
        agent_code="course_eval",
        domain="course-outcome-evaluation",
        thread_id="t1",
    )


async def _seed_task(factory, *, status="draft"):
    async with factory() as session:
        course = CeCourse(code="C1", name="材料力学", owner_user_id="u1")
        session.add(course)
        await session.flush()
        task = CeSemesterTask(
            course_id=course.course_id,
            semester="2024-2025-1",
            class_no="c1",
            status=status,
            owner_user_id="u1",
        )
        session.add(task)
        await session.flush()
        task_id = task.semester_task_id
        session.add(
            CeMaterial(
                semester_task_id=task_id,
                material_type="grade_sheet",
                file_id="uf_1",
                original_filename="成绩单.pdf",
            )
        )
        await session.commit()
        return {"course_id": course.course_id, "task_id": task_id}


def _patch_factory(monkeypatch, db_factory):
    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: db_factory,
    )


def _fake_student_pages():
    """两个封面页（segment_students 被 patch，这里仅用于版面渲染兜底）。"""
    import numpy as np
    from backend.application.course_eval.extraction.pdf_pager import PageImage

    return [
        PageImage(page_no=0, gray=np.zeros((80, 120), dtype="uint8"), is_spread=False),
        PageImage(page_no=1, gray=np.zeros((80, 120), dtype="uint8"), is_spread=False),
    ]


def _cover_content_list():
    """两页封面 + 一页正文的 MinerU content_list（含 colspan 噪声）。"""
    return [
        {
            "type": "table",
            "page_idx": 0,
            "table_body": (
                "<table><tr><td>大题</td><td>一</td><td colspan=2>二</td>"
                "<td>总分</td></tr>"
                "<tr><td>得分</td><td>8</td><td>20</td><td></td><td>28</td></tr>"
                "</table>"
            ),
        },
        {"type": "table", "page_idx": 0, "table_body": "<table><tr><td>得分</td><td></td></tr></table>"},
        {
            "type": "table",
            "page_idx": 1,
            "table_body": (
                "<table><tr><td>大题</td><td>一</td><td>二</td><td>总分</td></tr>"
                "<tr><td>得分</td><td>6</td><td>9</td><td>20</td></tr></table>"
            ),
        },
        {"type": "text", "page_idx": 0, "text": "一、汉译英。（1分×10，共10分）（工程力学专业）"},
        {"type": "text", "page_idx": 2, "text": "二、填空题。（2分×10，共20分)"},
    ]


def test_parse_paper_ocr_reads_structure_and_cover_rows():
    """一次 OCR 同时给出大题结构与每份封面的得分行。"""
    from decimal import Decimal

    from backend.application.course_eval import tools as ce_tools

    result = ce_tools.parse_paper_ocr(_cover_content_list(), cover_count=2)
    assert result.section_count == 2
    assert [d.question_type for d in result.section_defs] == ["汉译英", "填空题"]
    assert result.cover_rows[0].section_scores == {
        1: Decimal("8"),
        2: Decimal("20"),
    }
    assert result.cover_rows[0].total_consistent is True
    # 第二页各题之和 15 ≠ 总分 20，整页要转人工
    assert result.cover_rows[1].total_consistent is False
    assert any("不一致" in w for w in result.warnings)


def test_resolve_applicable_orders_skips_major_only_section():
    """专业专属大题在全部样本上都没得分 → 不计入本教学班卷面满分。"""
    from decimal import Decimal

    from backend.application.course_eval import tools as ce_tools
    from backend.application.course_eval.extraction.cover_score_table import (
        CoverScoreRow,
        SectionDef,
    )

    result = ce_tools.PaperOcrResult(
        cover_rows={
            0: CoverScoreRow(
                section_scores={2: Decimal("18")},
                total=Decimal("18"),
                section_columns=2,
                total_consistent=True,
            )
        },
        section_defs=[
            SectionDef(1, "汉译英", Decimal("10"), None, "工程力学专业"),
            SectionDef(2, "填空题", Decimal("20"), None, None),
        ],
        section_count=2,
        warnings=[],
    )
    orders, warnings = ce_tools.resolve_applicable_orders(result)
    assert orders == [2]
    assert any("不考该题" in w for w in warnings)


def test_align_covers_to_students_matches_final_score():
    """封面总分与成绩单期末成绩同源：唯一命中直接对齐，同分要提示确认。"""
    from decimal import Decimal

    from backend.application.course_eval import tools as ce_tools
    from backend.application.course_eval.extraction.cover_score_table import (
        CoverScoreRow,
    )

    class Student:
        def __init__(self, sid, name, final):
            self.student_id = sid
            self.student_no = sid
            self.name = name
            self.final_score = final
            self.flag = ""

    students = [
        Student("s1", "甲", Decimal("28")),
        Student("s2", "乙", Decimal("61")),
        Student("s3", "丙", Decimal("61")),
    ]
    cover_rows = {
        0: CoverScoreRow(total=Decimal("61"), section_columns=2),
        1: CoverScoreRow(total=Decimal("28"), section_columns=2),
    }
    assigned, warnings = ce_tools.align_covers_to_students(cover_rows, 2, students)
    assert assigned[1] == "s1"
    assert assigned[0] in ("s2", "s3")
    assert any("同分学生" in w for w in warnings)


def test_build_cover_cells_only_confirms_when_total_matches():
    """单通道下只有总分自洽才 confirmed；补 0 与不自洽都进核对清单。"""
    from decimal import Decimal

    from backend.application.course_eval import tools as ce_tools
    from backend.application.course_eval.extraction.cover_score_table import (
        CoverScoreRow,
    )

    cover_rows = {
        0: CoverScoreRow(
            section_scores={2: Decimal("20")},
            total=Decimal("20"),
            section_columns=2,
            total_consistent=True,
            inferred_zero_orders=(1,),
        ),
        1: CoverScoreRow(
            section_scores={1: Decimal("6"), 2: Decimal("9")},
            total=Decimal("20"),
            section_columns=2,
            total_consistent=False,
        ),
    }
    cells, warnings = ce_tools.build_cover_cells(
        cover_rows=cover_rows,
        cover_to_student={0: "s1", 1: "s2"},
        cover_pages=[0, 5],
        sections_by_order={1: "sec-1", 2: "sec-2"},
        full_scores={1: Decimal("10"), 2: Decimal("20")},
        applicable_orders=[1, 2],
    )
    by_key = {(c["student_id"], c["section_id"]): c for c in cells}
    assert by_key[("s1", "sec-2")]["status"] == "confirmed"
    assert by_key[("s1", "sec-2")]["confidence"] == Decimal("0.90")
    assert by_key[("s1", "sec-2")]["full_score"] == Decimal("20")
    # 总分自洽时空白列补 0，但仍要教师确认
    assert by_key[("s1", "sec-1")]["score"] == Decimal("0")
    assert by_key[("s1", "sec-1")]["status"] == "pending_review"
    assert all(
        c["status"] == "pending_review" for c in cells if c["student_id"] == "s2"
    )
    assert all(c["source"] == "ocr_table" for c in cells)
    assert by_key[("s2", "sec-1")]["page_ref"] == "p05"
    assert warnings == []


async def test_extract_paper_writes_ocr_records(monkeypatch, db_factory, ctx):
    """extract_paper 落大题结构（题型/满分）与封面得分，并标记样本学生。"""
    from decimal import Decimal
    from pathlib import Path

    from backend.application.course_eval import tools as ce_tools
    from backend.application.course_eval.agent_tools import (
        extraction_tools as ce_extraction_tools,
    )
    from backend.application.course_eval.extraction.cover_score_table import (
        CoverScoreRow,
        SectionDef,
    )
    from backend.application.course_eval.extraction.paper_layout import StudentPageRange

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)
    async with db_factory() as session:
        tmp_pdf = Path("/tmp/radai-test-paper.pdf")
        tmp_pdf.write_bytes(b"%PDF-1.4\n%%EOF")
        material = CeMaterial(
            semester_task_id=ids["task_id"], material_type="paper",
            file_id="uf_paper_1", original_filename="学生答题试卷.pdf",
        )
        session.add(material)
        await session.flush()
        material_id = material.material_id
        # 上一轮遗留的 3 个空占位大题
        paper = CeExamPaper(semester_task_id=ids["task_id"])
        session.add(paper)
        await session.flush()
        for order in (1, 2, 3):
            session.add(CeExamSection(
                paper_id=paper.paper_id, order_no=order,
                report_order_no=order, question_type="待核对", total_score=0,
            ))
        session.add(CeStudent(
            semester_task_id=ids["task_id"], student_no="S1", name="甲",
            final_score=Decimal("28"),
        ))
        session.add(CeStudent(
            semester_task_id=ids["task_id"], student_no="S2", name="乙",
            final_score=Decimal("15"),
        ))
        await session.commit()

    class _FakeFileRow:
        display_name = "学生答题试卷.pdf"
        local_abs_path = "/tmp/radai-test-paper.pdf"
        storage_object = None
        storage_bucket = None
        mime_type = "application/pdf"

    async def _fake_resolve(session, file_id, current_user_id):
        return _FakeFileRow(), True, "ok"

    async def _fake_ocr(**kwargs):
        return ce_tools.PaperOcrResult(
            cover_rows={
                0: CoverScoreRow(
                    section_scores={1: Decimal("8"), 2: Decimal("20")},
                    total=Decimal("28"),
                    section_columns=2,
                    total_consistent=True,
                )
            },
            section_defs=[
                SectionDef(1, "汉译英", Decimal("10"), "1分×10", None),
                SectionDef(2, "填空题", Decimal("20"), "2分×10", None),
            ],
            section_count=2,
            warnings=[],
        )

    monkeypatch.setattr(
        "backend.services.user_file_service.resolve_user_file_for_download",
        _fake_resolve,
    )
    monkeypatch.setattr(
        "backend.application.course_eval.extraction.pdf_pager.render_pages",
        lambda data: _fake_student_pages(),
    )
    monkeypatch.setattr(
        "backend.application.course_eval.extraction.paper_layout.segment_students",
        lambda pages, params=None: ([StudentPageRange(cover_page=0, end_page=0)], []),
    )
    monkeypatch.setattr(ce_extraction_tools, "ocr_paper_structure", _fake_ocr)

    tool = ce_tools.get_extract_paper_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({
        "semester_task_id": ids["task_id"],
        "material_id": material_id,
    }))
    assert "error" not in out, out
    assert out["paper_sample_n"] == 1
    assert out["score_cells"] == 2
    assert out["pending_review_cells"] == 0
    assert [s["total_score"] for s in out["sections"]] == ["10", "20"]

    from sqlalchemy import select
    async with db_factory() as session:
        records = (
            (await session.execute(
                select(CeScoreRecord).where(
                    CeScoreRecord.semester_task_id == ids["task_id"]
                )
            ))
            .scalars()
            .all()
        )
        sections = (
            (await session.execute(
                select(CeExamSection).order_by(CeExamSection.order_no)
            ))
            .scalars()
            .all()
        )
        sample = (
            (await session.execute(
                select(CeStudent).where(CeStudent.in_paper_sample.is_(True))
            ))
            .scalars()
            .all()
        )
    # 空占位大题被清掉，题型与满分来自试卷正文
    assert [(s.order_no, s.question_type, s.total_score) for s in sections] == [
        (1, "汉译英", Decimal("10.00")),
        (2, "填空题", Decimal("20.00")),
    ]
    assert len(records) == 2
    assert {r.status for r in records} == {"confirmed"}
    assert {r.source for r in records} == {"ocr_table"}
    assert {r.full_score for r in records} == {Decimal("10.00"), Decimal("20.00")}
    # 封面总分 28 唯一命中「甲」的期末成绩
    assert [s.name for s in sample] == ["甲"]


async def test_update_scores_upsert_adds_missing_records(monkeypatch, db_factory, ctx):
    """update_scores 对不存在的得分记录执行新增（补录），可支撑达成度计算。"""
    from decimal import Decimal

    from backend.application.course_eval import tools as ce_tools
    from backend.db.models.course_eval import (
        CeExamPaper, CeExamSection, CeStudent,
    )

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory, status="mapping_pending")
    async with db_factory() as session:
        paper = CeExamPaper(semester_task_id=ids["task_id"])
        session.add(paper)
        await session.flush()
        sec = CeExamSection(paper_id=paper.paper_id, order_no=1, question_type="计算题", total_score=20)
        session.add(sec)
        await session.flush()
        stu = CeStudent(semester_task_id=ids["task_id"], student_no="S9", name="补录生")
        session.add(stu)
        await session.flush()
        await session.commit()
        sec_id2, stu_id2 = sec.section_id, stu.student_id

    tool = ce_tools.get_update_scores_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({
        "semester_task_id": ids["task_id"],
        "cells": [
            {"student_id": stu_id2, "section_id": sec_id2, "score": "16"},
        ],
    }))
    assert "error" not in out, out
    assert out["updated"] == 1

    from sqlalchemy import select
    async with db_factory() as session:
        rec = (
            await session.execute(
                select(CeScoreRecord).where(
                    CeScoreRecord.semester_task_id == ids["task_id"],
                    CeScoreRecord.student_id == stu_id2,
                    CeScoreRecord.section_id == sec_id2,
                )
            )
        ).scalar_one_or_none()
    assert rec is not None
    assert Decimal(rec.score) == Decimal("16")
    assert rec.source == "manual"
    assert rec.status == "confirmed"
    assert rec.confidence == 1.0


async def test_confirm_scores_confirms_pending_without_changing_values(
    monkeypatch, db_factory, ctx
):
    """教师说「分数没问题」时 confirm_scores 只改状态，识别值与来源保持不变。"""
    from decimal import Decimal

    from backend.application.course_eval import tools as ce_tools
    from backend.db.models.course_eval import CeExamPaper, CeExamSection, CeStudent

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory, status="needs_review")
    async with db_factory() as session:
        paper = CeExamPaper(semester_task_id=ids["task_id"])
        session.add(paper)
        await session.flush()
        sec = CeExamSection(
            paper_id=paper.paper_id, order_no=1, question_type="计算题", total_score=50
        )
        session.add(sec)
        stu = CeStudent(semester_task_id=ids["task_id"], student_no="S1", name="甲")
        session.add(stu)
        await session.flush()
        session.add(
            CeScoreRecord(
                semester_task_id=ids["task_id"],
                student_id=stu.student_id,
                section_id=sec.section_id,
                item_id="",
                full_score=Decimal("50"),
                score=Decimal("31"),
                raw_score=Decimal("31"),
                source="ocr_table",
                confidence=Decimal("0.400"),
                status="pending_review",
            )
        )
        await session.commit()

    tool = ce_tools.get_confirm_scores_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({"semester_task_id": ids["task_id"]}))
    assert "error" not in out, out
    assert out["confirmed"] == 1

    from sqlalchemy import select

    async with db_factory() as session:
        rec = (
            await session.execute(
                select(CeScoreRecord).where(
                    CeScoreRecord.semester_task_id == ids["task_id"]
                )
            )
        ).scalar_one()
        audit = (
            (
                await session.execute(
                    select(CeAuditLog).where(CeAuditLog.action == "score.confirm")
                )
            )
            .scalars()
            .all()
        )
    assert rec.status == "confirmed"
    assert rec.source == "ocr_table"
    assert Decimal(rec.score) == Decimal("31")
    assert len(audit) == 1


async def test_achievement_excludes_class_specific_unscored_section(
    monkeypatch, db_factory, ctx
):
    """专业专属大题本班不考（全班无得分）时，不能计进目标支撑分拉低得分率。"""
    from decimal import Decimal

    from backend.application.course_eval import report_service
    from backend.db.models.course_eval import CeExamPaper, CeExamSection, CeStudent

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory, status="computed")
    async with db_factory() as session:
        goal = CeCourseGoal(
            course_id=ids["course_id"], goal_code="目标1", description="d"
        )
        session.add(goal)
        paper = CeExamPaper(semester_task_id=ids["task_id"])
        session.add(paper)
        await session.flush()
        # 第 1 题限工程力学专业（本班不考，无得分）；第 2 题全班都考
        sec1 = CeExamSection(
            paper_id=paper.paper_id,
            order_no=1,
            question_type="汉译英",
            total_score=Decimal("10"),
            applicable_majors=["工程力学专业"],
        )
        sec2 = CeExamSection(
            paper_id=paper.paper_id,
            order_no=2,
            question_type="填空题",
            total_score=Decimal("20"),
        )
        session.add_all([sec1, sec2])
        stu = CeStudent(
            semester_task_id=ids["task_id"],
            student_no="S1",
            name="甲",
            regular_score=Decimal("80"),
            final_score=Decimal("10"),
            total_score=Decimal("38"),
        )
        session.add(stu)
        await session.flush()
        session.add_all(
            [
                CeScoreRecord(
                    semester_task_id=ids["task_id"],
                    student_id=stu.student_id,
                    section_id=sec2.section_id,
                    item_id="",
                    full_score=Decimal("20"),
                    score=Decimal("10"),
                    source="ocr_table",
                    status="confirmed",
                ),
                CeGoalMapping(
                    semester_task_id=ids["task_id"],
                    goal_id=goal.goal_id,
                    section_id=sec1.section_id,
                    item_id="",
                    weight=Decimal("1"),
                ),
                CeGoalMapping(
                    semester_task_id=ids["task_id"],
                    goal_id=goal.goal_id,
                    section_id=sec2.section_id,
                    item_id="",
                    weight=Decimal("1"),
                ),
            ]
        )
        await session.commit()

    async with db_factory() as session:
        items = await report_service.compute_achievement(session, ids["task_id"])
    assert len(items) == 1
    item = items[0]
    # 支撑分只算本班考的第 2 题（20 分），得分率 10/20=50.0%
    assert Decimal(item["support_score"]) == Decimal("20")
    assert item["rate"] == "50.0"
    assert item["notes"] and "本班不考" in item["notes"][0]


async def test_snapshot_flags_pending_review_scores(monkeypatch, db_factory, ctx):
    """存在待核对得分时快照要显式报告，不能让统计看起来已定稿。"""
    from decimal import Decimal

    from backend.application.course_eval import report_service
    from backend.db.models.course_eval import CeExamPaper, CeExamSection, CeStudent

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory, status="computed")
    async with db_factory() as session:
        paper = CeExamPaper(semester_task_id=ids["task_id"])
        session.add(paper)
        await session.flush()
        sec = CeExamSection(
            paper_id=paper.paper_id,
            order_no=1,
            question_type="计算题",
            total_score=Decimal("50"),
        )
        session.add(sec)
        students = [
            CeStudent(
                semester_task_id=ids["task_id"],
                student_no=f"S{i}",
                name=f"学生{i}",
                regular_score=Decimal("80"),
                final_score=Decimal("30"),
                total_score=Decimal("50"),
            )
            for i in range(2)
        ]
        session.add_all(students)
        await session.flush()
        session.add_all(
            [
                CeScoreRecord(
                    semester_task_id=ids["task_id"],
                    student_id=st.student_id,
                    section_id=sec.section_id,
                    item_id="",
                    full_score=Decimal("50"),
                    score=Decimal("30"),
                    source="ocr_table",
                    status="pending_review" if i == 0 else "confirmed",
                )
                for i, st in enumerate(students)
            ]
        )
        await session.commit()

    async with db_factory() as session:
        await report_service.compute(
            session, task_id=ids["task_id"], operator_id="u1"
        )
        await session.commit()
        snap = await report_service.latest_snapshot(session, ids["task_id"])
    assert snap is not None
    # 样本量按学生数（2），不是记录条数
    assert snap["scope"]["paper_sample_n"] == 2
    assert any("待核对" in issue for issue in snap["blocking_issues"])


def test_align_covers_falls_back_to_section_sum_when_total_unreadable():
    """总分列是手写图片读不出时，各题分读全的封面按各题之和对齐学生。"""
    from decimal import Decimal
    from types import SimpleNamespace

    from backend.application.course_eval.extraction.cover_score_table import (
        CoverScoreRow,
    )
    from backend.application.course_eval.tools import align_covers_to_students

    students = [
        SimpleNamespace(student_id="a", name="甲", final_score=Decimal("78"), flag=""),
        SimpleNamespace(student_id="b", name="乙", final_score=Decimal("46"), flag=""),
    ]
    rows = {
        0: CoverScoreRow(
            section_scores={1: Decimal("12"), 2: Decimal("19"), 3: Decimal("47")},
            total=None,
            section_columns=3,
        ),
        1: CoverScoreRow(
            section_scores={1: Decimal("6"), 2: Decimal("20"), 3: Decimal("20")},
            total=Decimal("46"),
            section_columns=3,
        ),
    }
    assigned, warnings = align_covers_to_students(rows, 2, students)
    assert assigned == {0: "a", 1: "b"}
    assert any("按各题之和 78 对齐" in w for w in warnings)
    # 各题分没读全时不推算总分，仍走名单顺序兜底
    partial = {
        0: CoverScoreRow(
            section_scores={1: Decimal("12")}, total=None, section_columns=3
        )
    }
    assigned2, warnings2 = align_covers_to_students(partial, 1, students)
    assert assigned2 == {0: "a"}  # 名单顺序兜底
    assert any("对不上" in w for w in warnings2)


async def _seed_report_ready_task(db_factory) -> dict:
    """computed 状态任务 + 统计快照 + 激活模板行（generate_report 前置）。"""
    import json as _json

    from backend.db.models.course_eval import CeReportTemplate, CeStatsSnapshot

    ids = await _seed_task(db_factory, status="computed")
    async with db_factory() as session:
        session.add(
            CeStatsSnapshot(
                semester_task_id=ids["task_id"],
                config_version=1,
                data_version=1,
                result_json=_json.dumps(
                    {
                        "scope": {"grade_sheet_n": 62, "paper_sample_n": 10},
                        "final_score_stats": {
                            "bands": [
                                {"label": "50以下", "count": 29, "pct": "46.8"}
                            ],
                            "average": "52.66",
                            "pass_rate": "43.5",
                            "max": "88",
                            "min": "6",
                        },
                        "section_rates": [],
                        "blocking_issues": [],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.add(
            CeReportTemplate(
                template_type="paper_analysis", version="1", active=True
            )
        )
        await session.commit()
    return ids


async def test_generate_report_renders_docx_and_links_download(
    monkeypatch, db_factory, ctx
):
    """docx 渲染成功时：版本记录 docx_file_key、返回下载链接与文件名。"""
    from backend.application.course_eval import render_service
    from backend.application.course_eval import tools as ce_tools
    from backend.application.course_eval.agent_tools import _shared
    from backend.application.course_eval.report.paper_analysis_renderer import (
        RenderResult,
    )

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_report_ready_task(db_factory)

    async def _fake_render(session, **kwargs):
        return (
            RenderResult(docx_bytes=b"PK-fake-docx", warnings=["示例告警"]),
            "",
            "材料力学试卷分析表_2024-2025-1_v1.docx",
        )

    async def _fake_register(runtime_context, *, filename, payload):
        assert payload == b"PK-fake-docx"
        return "uf_report_1", "/api/v2/files/uf_report_1/download"

    monkeypatch.setattr(render_service, "render_paper_analysis_docx", _fake_render)
    monkeypatch.setattr(_shared, "_register_report_artifact", _fake_register)

    tool = ce_tools.get_generate_report_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({"semester_task_id": ids["task_id"]}))
    assert "error" not in out, out
    assert out["docx"]["rendered"] is True
    assert out["docx"]["download_url"] == "/api/v2/files/uf_report_1/download"
    assert out["docx"]["filename"].endswith(".docx")
    assert out["docx"]["warnings"] == ["示例告警"]

    from sqlalchemy import select

    async with db_factory() as session:
        version = (
            await session.execute(select(CeReportVersion))
        ).scalars().one()
    assert version.docx_file_key == "uf_report_1"


async def test_generate_report_degrades_without_template_file(
    monkeypatch, db_factory, ctx
):
    """模板文件缺失时降级：草稿照常落库，docx.rendered=false 且给出原因。"""
    from backend.application.course_eval import render_service
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_report_ready_task(db_factory)

    async def _fake_render(session, **kwargs):
        return None, "模板文件未配置：请把学院空白模板 docx 放到模板目录", ""

    monkeypatch.setattr(render_service, "render_paper_analysis_docx", _fake_render)

    tool = ce_tools.get_generate_report_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({"semester_task_id": ids["task_id"]}))
    assert "error" not in out, out
    assert out["report_id"]
    assert out["docx"]["rendered"] is False
    assert "模板文件未配置" in out["docx"]["reason"]

    from sqlalchemy import select

    async with db_factory() as session:
        version = (
            await session.execute(select(CeReportVersion))
        ).scalars().one()
    assert version.docx_file_key is None
    assert version.content_json  # 草稿数据仍在


def test_missing_cell_issues_reports_gap_not_silent_zero():
    """样本学生在本班考的大题上缺得分要显式报告（否则达成度被静默低估）。"""
    from backend.application.course_eval.report_service import _missing_cell_issues

    sections = [
        {"section_id": "s1", "order_no": 3, "question_type": "证明题"},
        {"section_id": "s2", "order_no": 5, "question_type": "计算题"},
    ]
    records = [
        {"student_id": "a", "section_id": "s1", "score": 5},
        {"student_id": "a", "section_id": "s2", "score": 30},
        {"student_id": "b", "section_id": "s2", "score": 40},
    ]
    issues = _missing_cell_issues(sections, records)
    assert len(issues) == 1
    assert "第3题" in issues[0] and "1 名样本学生缺得分" in issues[0]


def test_course_eval_prompt_guides_missing_scores():
    """提示词须包含"缺少逐题得分时引导补录、不得静默产出空达成度"约束。"""
    from backend.application.agents.prompt_templates.course_eval import SYSTEM_PROMPT

    assert "update_scores" in SYSTEM_PROMPT
    assert "补录" in SYSTEM_PROMPT
    assert "无法计算" in SYSTEM_PROMPT
    assert "不得声称「已完成评价」" in SYSTEM_PROMPT
