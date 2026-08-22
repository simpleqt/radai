"""试题-目标映射服务（M-05 / T3-02）。

- validate_coverage：纯函数，映射完整性的全部规则（题目全归属、目标全支撑、权重和为 1、
  映射分值 vs 大纲期望偏差）；
- bulk_assign：按题型批量指派（§1.7 的主力交互）；
- apply_mappings：批量落库，全部标 confirmed_by。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.course_eval import task_service
from backend.application.course_eval.errors import TaskNotFound
from backend.application.course_eval.task_service import assert_can_edit_data
from backend.config.loggers import get_logger
from backend.db.models.course_eval import (
    CeAuditLog,
    CeCourseGoal,
    CeExamPaper,
    CeExamSection,
    CeGoalMapping,
    CeSemesterTask,
)

logger = get_logger(__name__)

# 大纲期望偏差阈值（§6.5 _deviation）：>20% warn，>40% error
DEV_WARN_PCT = 20
DEV_ERROR_PCT = 40


@dataclass(frozen=True)
class CoverageCheck:
    """映射完整性检查结果（§5.4 GET /mappings 的 checks 部分）。"""

    unmapped_sections: list[str] = field(default_factory=list)
    unmapped_items: list[str] = field(default_factory=list)
    goals_without_support: list[str] = field(default_factory=list)
    weight_sum_violations: list[dict] = field(default_factory=list)
    goal_score_vs_syllabus: list[dict] = field(default_factory=list)


def validate_coverage(
    sections: Sequence[dict],
    items: Sequence[dict],
    mappings: Sequence[dict],
    goals: Sequence[dict],
) -> CoverageCheck:
    """映射完整性规则（纯函数，测试直接构造数据穷举）。

    sections: [{section_id, order_no, report_order_no, question_type, total_score, applicable_majors}]
    items:    [{item_id, section_id, order_no, full_score}]
    mappings: [{section_id, item_id, goal_id, weight}]
    goals:    [{goal_id, goal_code, weight_exam}]
    """
    section_ids = {s["section_id"] for s in sections}
    # 大题级映射 = 无 item_id 的映射（小题级映射不覆盖整道大题）
    mapped_sections = {m["section_id"] for m in mappings if not m.get("item_id")}
    mapped_items = {m["item_id"] for m in mappings if m.get("item_id")}

    unmapped_sections = sorted(section_ids - mapped_sections)
    # 小题被覆盖当且仅当：有直接小题映射，或所在大题有大题级映射
    unmapped_items = sorted(
        i["item_id"]
        for i in items
        if i["item_id"] not in mapped_items and i["section_id"] not in mapped_sections
    )

    # 目标全支撑
    supported_goals = {m["goal_id"] for m in mappings}
    goals_without_support = sorted(
        g["goal_id"] for g in goals if g["goal_id"] not in supported_goals
    )

    # 权重和为 1（同一 题目×目标 组合）
    weight_sum_violations: list[dict] = []
    per_question: dict[tuple[str, str], Decimal] = {}
    for m in mappings:
        key = (m["section_id"], m.get("item_id", ""))
        per_question[key] = per_question.get(key, Decimal(0)) + Decimal(
            m.get("weight", 1)
        )
    for (section_id, item_id), total in per_question.items():
        if abs(total - Decimal(1)) > Decimal("0.001"):
            weight_sum_violations.append(
                {"section_id": section_id, "item_id": item_id, "sum": total}
            )

    # 映射分值 vs 大纲期望（§5.4 expected_score 算法）
    # full_score 解析：小题级取 item.full_score，大题级取 section.total_score
    section_full = {s["section_id"]: Decimal(s.get("total_score", 0)) for s in sections}
    item_full = {i["item_id"]: Decimal(i.get("full_score", 0)) for i in items}
    goal_score_vs_syllabus: list[dict] = []
    total_exam_weight = sum(Decimal(g.get("weight_exam", 0)) for g in goals)
    for goal in goals:
        goal_mappings = [m for m in mappings if m.get("goal_id") == goal["goal_id"]]
        mapped_score = sum(
            (item_full.get(m.get("item_id"), section_full.get(m["section_id"], 0)))
            * Decimal(m.get("weight", 1))
            for m in goal_mappings
        )
        paper_total = sum(Decimal(s.get("total_score", 0)) for s in sections)
        expected = (
            paper_total * (Decimal(goal.get("weight_exam", 0)) / total_exam_weight)
            if total_exam_weight
            else Decimal(0)
        )
        deviation_pct = (
            abs(mapped_score - expected) / expected * 100 if expected else Decimal(0)
        )
        level = (
            "error"
            if deviation_pct > DEV_ERROR_PCT
            else ("warn" if deviation_pct > DEV_WARN_PCT else "ok")
        )
        goal_score_vs_syllabus.append(
            {
                "goal_code": goal.get("goal_code"),
                "mapped_score": mapped_score,
                "syllabus_exam_weight": goal.get("weight_exam"),
                "expected_score": expected,
                "deviation_pct": deviation_pct,
                "level": level,
            }
        )

    return CoverageCheck(
        unmapped_sections=unmapped_sections,
        unmapped_items=unmapped_items,
        goals_without_support=goals_without_support,
        weight_sum_violations=weight_sum_violations,
        goal_score_vs_syllabus=goal_score_vs_syllabus,
    )


async def _get_task(session: AsyncSession, task_id: str) -> CeSemesterTask:
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
    return task


async def _assert_mapping_items_belong_to_task(
    session: AsyncSession,
    *,
    task: CeSemesterTask,
    items: list[dict],
) -> None:
    """Reject mapping rows whose section/goal belongs to another task/course."""

    section_ids = {
        str(item.get("section_id") or "") for item in items if item.get("section_id")
    }
    goal_ids = {str(item.get("goal_id") or "") for item in items if item.get("goal_id")}
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
        unknown = sorted(section_ids - owned_sections)
        if unknown:
            raise ValueError(
                "映射中的题目不属于本任务的试卷: " + ", ".join(unknown)
            )
    if goal_ids:
        owned_goals = set(
            (
                await session.execute(
                    select(CeCourseGoal.goal_id).where(
                        CeCourseGoal.goal_id.in_(goal_ids),
                        CeCourseGoal.course_id == task.course_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        unknown_goals = sorted(goal_ids - owned_goals)
        if unknown_goals:
            raise ValueError(
                "映射中的课程目标不属于本任务的课程: " + ", ".join(unknown_goals)
            )


async def apply_mappings(
    session: AsyncSession,
    *,
    task_id: str,
    items: list[dict],
    operator_id: str,
) -> int:
    """批量落库映射，全部标 confirmed_by/confirmed_at（T3-02 DoD）。"""
    # 任务行锁串行化整任务级重写:无锁时两个并发重写各自基于旧快照删旧插新,
    # 后者 DELETE 匹配 0 行(SQLAlchemy 仅告警),终态是两份映射的并集——每条
    # 题目×目标映射翻倍、权重和变 2.0,达成度静默算错。
    task = await task_service.get_task(session, task_id, for_update=True)
    assert_can_edit_data(task)

    # 外键只保证 id 存在,不保证它属于本任务:Agent 传入幻觉 UUID 或其它任务的
    # id 时,映射会静默写成跨任务组合,得分率/达成度随之算错。section 必须属于
    # 本任务的试卷,goal 必须属于本任务的课程。
    await _assert_mapping_items_belong_to_task(session, task=task, items=items)

    # 删除旧映射（整任务级重写）。用批量 DELETE(当前读):即使会话更早的
    # 普通读已建立 REPEATABLE READ 视图,当前读也按最新已提交数据删除,
    # 不会漏删并发写入的行。
    await session.execute(
        delete(CeGoalMapping)
        .where(CeGoalMapping.semester_task_id == task_id)
        .execution_options(synchronize_session=False)
    )

    now = datetime.now()
    count = 0
    for item in items:
        session.add(
            CeGoalMapping(
                semester_task_id=task_id,
                goal_id=item["goal_id"],
                section_id=item["section_id"],
                item_id=item.get("item_id", ""),
                weight=Decimal(item.get("weight", 1)),
                auto_suggested=bool(item.get("auto_suggested", False)),
                suggest_reason=item.get("suggest_reason"),
                confirmed_by=operator_id,
                confirmed_at=now,
            )
        )
        count += 1
    await session.flush()
    session.add(
        CeAuditLog(
            semester_task_id=task_id,
            operator_id=operator_id,
            action="mapping.batch_apply",
            target_type="semester_task",
            target_id=task_id,
            detail_json={"mappings": count},
        )
    )
    # 修改映射触发回退
    await task_service.invalidate_downstream(
        session, task_id, from_stage="mapping.confirm", operator_id=operator_id
    )
    logger.info(
        "批量应用映射: task_id=%s mappings=%d operator=%s", task_id, count, operator_id
    )
    return count


async def list_mappings(
    session: AsyncSession, task_id: str
) -> tuple[list[dict], CoverageCheck]:
    """当前映射 + 完整性检查结果（§5.4 GET /mappings）。"""
    await _get_task(session, task_id)
    mappings = (
        (
            await session.execute(
                select(CeGoalMapping).where(CeGoalMapping.semester_task_id == task_id)
            )
        )
        .scalars()
        .all()
    )
    goals = (
        (
            await session.execute(
                select(CeCourseGoal)
                .join(
                    CeSemesterTask,
                    CeSemesterTask.course_id == CeCourseGoal.course_id,
                )
                .where(CeSemesterTask.semester_task_id == task_id)
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

    mapping_payload = [
        {
            "mapping_id": m.mapping_id,
            "goal_id": m.goal_id,
            "section_id": m.section_id,
            "item_id": m.item_id,
            "weight": m.weight,
            "confirmed_by": m.confirmed_by,
        }
        for m in mappings
    ]
    check = validate_coverage(
        sections=[
            {"section_id": s.section_id, "total_score": s.total_score} for s in sections
        ],
        items=[],
        mappings=mapping_payload,
        goals=[
            {
                "goal_id": g.goal_id,
                "goal_code": g.goal_code,
                "weight_exam": g.weight_exam,
            }
            for g in goals
        ],
    )
    return mapping_payload, check
