"""统计计算与快照服务（M-06 编排 / T3-03）。

- compute：组装 StatsInputs → build_snapshot（纯函数）→ 落 ce_stats_snapshots；
- statistics：读取最新快照；
- achievement：从快照计算达成度（方法二 course_average）。
"""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.course_eval.errors import TaskNotFound
from backend.application.course_eval.task_service import get_task
from backend.application.course_eval.stats_engine import (
    AchievementConfig,
    StatsInputs,
    build_snapshot,
    goal_achievement_course_average,
    regular_achievement_from_grade_sheet,
)
from backend.config.loggers import get_logger
from backend.db.models.course_eval import (
    CeAchievementConfig,
    CeCourseGoal,
    CeExamPaper,
    CeExamSection,
    CeGoalMapping,
    CeScoreRecord,
    CeSemesterTask,
    CeStatsSnapshot,
    CeStudent,
)

logger = get_logger(__name__)


async def _load_student_scores(
    session: AsyncSession, task_id: str
) -> tuple[list[Decimal], list[Decimal], list[Decimal], dict[str, Decimal]]:
    """成绩单学生 → 平时/期末/总评（缺考按 0 计入总评口径）+ 学生期末成绩索引。"""
    students = (
        (
            await session.execute(
                select(CeStudent).where(CeStudent.semester_task_id == task_id)
            )
        )
        .scalars()
        .all()
    )
    regulars: list[Decimal] = []
    finals: list[Decimal] = []
    totals: list[Decimal] = []
    final_by_student: dict[str, Decimal] = {}
    for s in students:
        if s.regular_score is not None:
            regulars.append(Decimal(s.regular_score))
        if s.final_score is not None:
            finals.append(Decimal(s.final_score))
            final_by_student[s.student_id] = Decimal(s.final_score)
        if s.total_score is not None:
            totals.append(Decimal(s.total_score))
    return regulars, finals, totals, final_by_student


async def _load_sections_and_scores(
    session: AsyncSession, task_id: str
) -> tuple[list[dict], list[dict], list[dict]]:
    papers = (
        (
            await session.execute(
                select(CeExamPaper).where(CeExamPaper.semester_task_id == task_id)
            )
        )
        .scalars()
        .all()
    )
    paper_ids = [p.paper_id for p in papers]
    sections = []
    if paper_ids:
        sections = list(
            (
                await session.execute(
                    select(CeExamSection).where(CeExamSection.paper_id.in_(paper_ids))
                )
            )
            .scalars()
            .all()
        )
    records = list(
        (
            await session.execute(
                select(CeScoreRecord).where(CeScoreRecord.semester_task_id == task_id)
            )
        )
        .scalars()
        .all()
    )
    section_payload = [
        {
            "section_id": s.section_id,
            "order_no": s.order_no,
            "report_order_no": s.report_order_no or s.order_no,
            "question_type": s.question_type,
            "full_score": s.total_score,
            "applicable_majors": s.applicable_majors,
        }
        for s in sections
    ]
    record_payload = [
        {
            "student_id": r.student_id,
            "section_id": r.section_id,
            "item_id": r.item_id,
            "score": r.score,
            "status": r.status,
        }
        for r in records
    ]
    return section_payload, record_payload, papers


def _missing_cell_issues(sections: list[dict], records: list[dict]) -> list[str]:
    """样本学生在本班考的大题上缺得分时显式报告。

    达成度按「Σ得分 /（样本人数 × Σ满分）」计算，缺失单元格等价于按 0 分参与，
    会把目标得分率压低，所以不能静默。
    """
    sample = {r["student_id"] for r in records if r.get("student_id")}
    if not sample:
        return []
    scored_by_section: dict[str, set[str]] = {}
    for r in records:
        scored_by_section.setdefault(r["section_id"], set()).add(r["student_id"])
    issues: list[str] = []
    for sec in sections:
        got = scored_by_section.get(sec["section_id"])
        if not got:  # 全班无得分 = 本班不考或整题未识别，另有提示
            continue
        missing = len(sample - got)
        if missing:
            issues.append(
                f"第{sec.get('order_no') or sec.get('report_order_no')}题"
                f"（{sec.get('question_type')}）有 {missing} 名样本学生缺得分，"
                "达成度会被低估；建议用 update_scores 补录后重算"
            )
    return issues


async def compute(
    session: AsyncSession, *, task_id: str, operator_id: str
) -> CeStatsSnapshot:
    # 任务行锁与改分/回退(update_scores → invalidate_downstream)互斥:无锁时
    # compute 基于改分前的 RR 快照生成的新快照会躲过并发回退的删除,且
    # computed_at 最新,报告随后引用旧分数——教师修正被静默丢弃(写偏斜)。
    task = await get_task(session, task_id, for_update=True)

    regulars, finals, totals, final_by_student = await _load_student_scores(
        session, task_id
    )
    sections, records, _papers = await _load_sections_and_scores(session, task_id)

    # 样本量按「有逐题得分的学生数」，不是记录条数（一名学生有多道大题）
    sample_student_ids = {r["student_id"] for r in records if r.get("student_id")}
    inputs = StatsInputs(
        grade_sheet_scores=totals or finals,
        final_scores=finals,
        paper_sample_scores=[
            final_by_student[sid]
            for sid in sorted(sample_student_ids)
            if sid in final_by_student
        ],
        section_records=records,
        sections=sections,
        expected_students=task.expected_students or 0,
        absent_students=task.absent_students or 0,
        violation_students=task.violation_students or 0,
    )
    result = build_snapshot(inputs)
    result.blocking_issues.extend(_missing_cell_issues(sections, records))
    pending = sum(1 for r in records if r.get("status") == "pending_review")
    if pending:
        result.blocking_issues.append(
            f"仍有 {pending} 条得分处于待核对状态，"
            "统计与达成度基于未确认数据；请先让教师确认或修正（confirm_scores / update_scores）"
        )

    snapshot = CeStatsSnapshot(
        semester_task_id=task_id,
        config_version=1,
        data_version=1,
        result_json=json.dumps(_serialize(result), ensure_ascii=False, default=str),
    )
    session.add(snapshot)
    await session.flush()
    logger.info(
        "计算统计快照: task_id=%s snapshot_id=%s issues=%d",
        task_id,
        snapshot.snapshot_id,
        len(result.blocking_issues),
    )
    return snapshot


def _serialize(result) -> dict:
    """把 StatsResult 转可 JSON 序列化的 dict（Decimal 转 str）。"""

    def band(b):
        return {"label": b.label, "count": b.count, "pct": str(b.pct)}

    def stats(s):
        return {
            "bands": [band(b) for b in s.bands],
            "average": str(s.average),
            "pass_rate": str(s.pass_rate) if s.pass_rate is not None else None,
            "max": str(s.max_score) if s.max_score is not None else None,
            "min": str(s.min_score) if s.min_score is not None else None,
        }

    return {
        "scope": result.scope,
        "final_score_stats": (
            stats(result.final_score_stats) if result.final_score_stats else None
        ),
        "section_rates": [
            {
                "report_order_no": r.report_order_no,
                "question_type": r.question_type,
                "full_score": str(r.full_score),
                "avg_score": str(r.avg_score),
                "rate": str(r.rate),
            }
            for r in result.section_rates
        ],
        "blocking_issues": result.blocking_issues,
    }


async def latest_snapshot(session: AsyncSession, task_id: str) -> dict | None:
    snap = (
        (
            await session.execute(
                select(CeStatsSnapshot)
                .where(CeStatsSnapshot.semester_task_id == task_id)
                .order_by(CeStatsSnapshot.computed_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if snap is None:
        return None
    data = json.loads(snap.result_json or "{}")
    data["snapshot_id"] = snap.snapshot_id
    data["computed_at"] = snap.computed_at.isoformat() if snap.computed_at else None
    return data


async def compute_achievement(session: AsyncSession, task_id: str) -> list[dict]:
    """从快照与映射计算课程目标达成度（方法二 course_average，§6.6）。"""
    task = (
        await session.execute(
            select(CeSemesterTask).where(CeSemesterTask.semester_task_id == task_id)
        )
    ).scalar_one_or_none()
    if task is None:
        raise TaskNotFound()

    regulars, _finals, _totals, _final_by_student = await _load_student_scores(
        session, task_id
    )
    if not regulars:
        # 无平时成绩时按目标逐个报告阻塞，而不是返回空列表（§6.6）
        goals = list(
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
        return [
            {
                "goal_code": g.goal_code,
                "method": "course_average",
                "achievement": None,
                "support_score": None,
                "rate": None,
                "blocking": "无平时成绩数据",
            }
            for g in goals
        ]

    goals = list(
        (
            await session.execute(
                select(CeCourseGoal).where(CeCourseGoal.course_id == task.course_id)
            )
        )
        .scalars()
        .all()
    )
    mappings = list(
        (
            await session.execute(
                select(CeGoalMapping).where(CeGoalMapping.semester_task_id == task_id)
            )
        )
        .scalars()
        .all()
    )
    sections, records, _papers = await _load_sections_and_scores(session, task_id)
    section_full = {s["section_id"]: s["full_score"] for s in sections}

    config_row = (
        (
            await session.execute(
                select(CeAchievementConfig).where(
                    CeAchievementConfig.course_id == task.course_id,
                    CeAchievementConfig.active.is_(True),
                )
            )
        )
        .scalars()
        .first()
    )
    config = AchievementConfig(
        method=(config_row.method if config_row else "course_average"),
        regular_weight=(
            Decimal(config_row.regular_weight) if config_row else Decimal("0.40")
        ),
        exam_weight=Decimal(config_row.exam_weight) if config_row else Decimal("0.60"),
    )

    # 专业专属大题（如「汉译英（工程力学专业）」与「证明题（非工程力学专业）」二选一）
    # 本班没考时全班都没有得分，若仍把它的满分算进支撑分，目标得分率会被系统性拉低。
    scored_sections = {r["section_id"] for r in records}
    skipped_notes: dict[str, str] = {}
    for sec in sections:
        sid = sec["section_id"]
        if sid in scored_sections or not sec.get("applicable_majors"):
            continue
        majors = "/".join(str(m) for m in (sec.get("applicable_majors") or []))
        skipped_notes[sid] = (
            f"第{sec['order_no']}题（{sec['question_type']}，限{majors}）"
            "本班不考，未计入支撑分"
        )

    mapping_payload = [
        {
            "goal_id": m.goal_id,
            "section_id": m.section_id,
            "item_id": m.item_id,
            "weight": m.weight,
            "full_score": section_full.get(m.section_id, 0),
        }
        for m in mappings
        if m.section_id not in skipped_notes
    ]
    notes_by_goal: dict[str, list[str]] = {}
    for m in mappings:
        note = skipped_notes.get(m.section_id)
        if note:
            notes_by_goal.setdefault(m.goal_id, []).append(note)
    goal_code_by_id = {g.goal_id: g.goal_code for g in goals}
    notes_by_code = {
        goal_code_by_id.get(gid, gid): notes
        for gid, notes in notes_by_goal.items()
    }
    goal_payload = [
        {"goal_id": g.goal_id, "goal_code": g.goal_code, "weight_exam": g.weight_exam}
        for g in goals
    ]
    record_payload = [
        {
            "student_id": r["student_id"],
            "section_id": r["section_id"],
            "item_id": r["item_id"],
            "score": r["score"],
        }
        for r in records
    ]

    try:
        regular_achievement = regular_achievement_from_grade_sheet(regulars)
    except ValueError:
        regular_achievement = None

    results = goal_achievement_course_average(
        record_payload,
        mapping_payload,
        goal_payload,
        config,
        regular_achievement=regular_achievement,
    )
    return [
        {
            "goal_code": r.goal_code,
            "method": r.method,
            "achievement": str(r.achievement),
            "support_score": (
                str(r.support_score) if r.support_score is not None else None
            ),
            "rate": str(r.rate) if r.rate is not None else None,
            "blocking": r.blocking,
            "notes": notes_by_code.get(r.goal_code, []),
        }
        for r in results
    ]
