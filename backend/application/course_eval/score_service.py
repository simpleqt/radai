"""分数读取与核对服务（M-02/M-03）。

- list_score_grid：核对页主接口数据（学生 × 大题/小题得分网格 + 行校验）。
- update_scores：批量修正；修改后经 invalidate_downstream 触发回退（作废快照、报告 stale）。
- manual 修正：source=manual、confidence=1.0、写审计（T2-05 DoD 第 4 条）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.course_eval import task_service
from backend.application.course_eval.errors import TaskNotFound
from backend.application.course_eval.task_service import assert_can_edit_data
from backend.config.loggers import get_logger
from backend.db.models.course_eval import (
    CeAuditLog,
    CeExamItem,
    CeExamPaper,
    CeExamSection,
    CeScoreRecord,
    CeSemesterTask,
    CeStudent,
)

logger = get_logger(__name__)


async def list_score_grid(session: AsyncSession, task_id: str) -> dict:
    """核对页主接口：sections / students / cells / row_checks（§5.3）。"""
    task = (
        await session.execute(
            select(CeSemesterTask).where(
                CeSemesterTask.semester_task_id == task_id,
                CeSemesterTask.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise TaskNotFound()

    students = (
        (
            await session.execute(
                select(CeStudent).where(CeStudent.semester_task_id == task_id)
            )
        )
        .scalars()
        .all()
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
    items = (
        (
            await session.execute(
                select(CeExamItem)
                .where(CeExamItem.section_id.in_([s.section_id for s in sections]))
                .order_by(CeExamItem.order_no)
            )
        )
        .scalars()
        .all()
    )
    records = (
        (
            await session.execute(
                select(CeScoreRecord).where(CeScoreRecord.semester_task_id == task_id)
            )
        )
        .scalars()
        .all()
    )

    items_by_section: dict[str, list[dict]] = {}
    for item in items:
        items_by_section.setdefault(item.section_id, []).append(
            {
                "item_id": item.item_id,
                "order_no": item.order_no,
                "full_score": item.full_score,
            }
        )
    sections_payload = [
        {
            "section_id": s.section_id,
            "order_no": s.order_no,
            "report_order_no": s.report_order_no,
            "question_type": s.question_type,
            "total_score": s.total_score,
            "items": items_by_section.get(s.section_id, []),
        }
        for s in sections
    ]
    students_payload = [
        {
            "student_id": st.student_id,
            "student_no": st.student_no,
            "name": st.name,
            "flag": st.flag,
        }
        for st in students
    ]
    cells = [
        {
            "student_id": r.student_id,
            "section_id": r.section_id,
            "item_id": r.item_id,
            "score": r.score,
            "raw_score": r.raw_score,
            "full_score": r.full_score,
            "source": r.source,
            "confidence": r.confidence,
            "status": r.status,
            "channel_agree": r.channel_agree,
        }
        for r in records
    ]

    # 行校验：Σ大题得分 vs 总分框（若成绩单有总评可交叉）
    per_student: dict[str, Decimal] = {}
    for r in records:
        if not r.item_id:
            per_student[r.student_id] = per_student.get(
                r.student_id, Decimal(0)
            ) + Decimal(r.score)
    grade_finals = {st.student_id: st.final_score for st in students}
    # 只校验有逐题得分的学生：没进试卷样本的学生本来就没有 Σ大题，
    # 全班列进来会把 60+ 名非样本学生误报成「总分不符」
    row_checks = [
        {
            "student_id": st.student_id,
            "sum_of_sections": per_student.get(st.student_id),
            "reported_total": grade_finals.get(st.student_id),
            "passed": (
                grade_finals.get(st.student_id) is not None
                and abs(
                    per_student.get(st.student_id, Decimal(0))
                    - grade_finals.get(st.student_id, Decimal(0))
                )
                <= Decimal("0.5")
            ),
        }
        for st in students
        if st.student_id in per_student
    ]
    return {
        "sections": sections_payload,
        "students": students_payload,
        "cells": cells,
        "row_checks": row_checks,
    }


async def _assert_score_cells_belong_to_task(
    session: AsyncSession,
    *,
    task: CeSemesterTask,
    cells: list[dict],
) -> None:
    """Reject cells whose student/section belongs to another task."""

    student_ids = {
        str(cell.get("student_id") or "") for cell in cells if cell.get("student_id")
    }
    section_ids = {
        str(cell.get("section_id") or "") for cell in cells if cell.get("section_id")
    }
    if student_ids:
        owned_students = set(
            (
                await session.execute(
                    select(CeStudent.student_id).where(
                        CeStudent.student_id.in_(student_ids),
                        CeStudent.semester_task_id == task.semester_task_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        unknown = sorted(student_ids - owned_students)
        if unknown:
            raise ValueError("得分单元格中的学生不属于本任务: " + ", ".join(unknown))
    if section_ids:
        owned_sections = set(
            (
                await session.execute(
                    select(CeExamSection.section_id)
                    .join(
                        CeExamPaper,
                        CeExamSection.paper_id == CeExamPaper.paper_id,
                    )
                    .where(
                        CeExamSection.section_id.in_(section_ids),
                        CeExamPaper.semester_task_id == task.semester_task_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        unknown_sections = sorted(section_ids - owned_sections)
        if unknown_sections:
            raise ValueError(
                "得分单元格中的题目不属于本任务的试卷: " + ", ".join(unknown_sections)
            )


async def update_scores(
    session: AsyncSession,
    *,
    task_id: str,
    cells: list[dict],
    operator_id: str,
) -> int:
    """批量修正/补录得分（upsert）。manual 修正 source=manual、confidence=1.0、写审计；
    记录不存在时新增（补录自动识别缺失的得分）；修改/新增后经
    invalidate_downstream 触发回退（同一事务）。"""
    task = await task_service.get_task(session, task_id, for_update=True)
    assert_can_edit_data(task)
    # 外键只保证 id 存在:补录分支会用传入的 student_id/section_id 新建记录,
    # 幻觉或跨任务 id 会静默写出属于别的任务的得分行,统计随之算错。
    await _assert_score_cells_belong_to_task(session, task=task, cells=cells)

    updated = 0
    for cell in cells:
        record = (
            await session.execute(
                select(CeScoreRecord).where(
                    CeScoreRecord.semester_task_id == task_id,
                    CeScoreRecord.student_id == cell["student_id"],
                    CeScoreRecord.section_id == cell["section_id"],
                    CeScoreRecord.item_id == cell.get("item_id", ""),
                )
            )
        ).scalar_one_or_none()
        score = Decimal(cell["score"])
        if record is None:
            # 补录：新增一条 confirmed 记录（full_score 占位 0 由自动识别回填，
            # 人工补录按教师提供的题分写 score）。
            record = CeScoreRecord(
                semester_task_id=task_id,
                student_id=cell["student_id"],
                section_id=cell["section_id"],
                item_id=cell.get("item_id", ""),
                full_score=Decimal(
                    cell.get("full_score", "0") or "0"
                ),
                score=score,
                raw_score=score,
                source="manual",
                confidence=Decimal("1.0"),
                status="confirmed",
                extract_version=1,
            )
            session.add(record)
        else:
            record.score = score
            record.source = "manual"
            record.confidence = Decimal("1.0")
            record.status = "confirmed"
            record.updated_at = datetime.now()
        updated += 1

    if updated:
        session.add(
            CeAuditLog(
                semester_task_id=task_id,
                operator_id=operator_id,
                action="score.batch_update",
                target_type="semester_task",
                target_id=task_id,
                detail_json={"cells": len(cells), "updated": updated},
            )
        )
        await task_service.invalidate_downstream(
            session, task_id, from_stage="score.update", operator_id=operator_id
        )
        logger.info(
            "批量修正/补录得分: task_id=%s cells=%d updated=%d operator=%s",
            task_id,
            len(cells),
            updated,
            operator_id,
        )
    return updated


async def confirm_scores(
    session: AsyncSession,
    *,
    task_id: str,
    operator_id: str,
    cells: list[dict] | None = None,
) -> int:
    """教师认可自动识别值时把待核对得分置为 confirmed（不改分值、不改来源）。

    cells 为空表示确认该任务全部 pending_review；否则只确认列出的
    (student_id, section_id, item_id)。识别值保持原样，因此 raw_score 与
    confidence 都不动，只留审计。
    """
    task = await task_service.get_task(session, task_id, for_update=True)
    assert_can_edit_data(task)

    stmt = select(CeScoreRecord).where(
        CeScoreRecord.semester_task_id == task_id,
        CeScoreRecord.status == "pending_review",
    )
    pending = (await session.execute(stmt)).scalars().all()
    if cells:
        wanted = {
            (c["student_id"], c["section_id"], c.get("item_id", "") or "")
            for c in cells
        }
        pending = [
            r
            for r in pending
            if (r.student_id, r.section_id, r.item_id or "") in wanted
        ]

    for record in pending:
        record.status = "confirmed"
        record.updated_at = datetime.now()
    confirmed = len(pending)

    if confirmed:
        session.add(
            CeAuditLog(
                semester_task_id=task_id,
                operator_id=operator_id,
                action="score.confirm",
                target_type="semester_task",
                target_id=task_id,
                detail_json={"confirmed": confirmed, "scope": "cells" if cells else "all"},
            )
        )
        logger.info(
            "确认识别得分: task_id=%s confirmed=%d operator=%s",
            task_id,
            confirmed,
            operator_id,
        )
    return confirmed
