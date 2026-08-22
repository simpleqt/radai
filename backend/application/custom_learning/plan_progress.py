"""学习计划与节点进度：序列化、复习解锁、聚焦、重算与内容修复。"""

from __future__ import annotations

from backend.db.models import (
    LearningNodeAttempt,
    LearningPlan,
    LearningPlanAdjustment,
    LearningPlanNode,
    User,
)
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from typing import Any
from uuid import uuid4
from backend.application.custom_learning.common import (
    AGENT_TEAM,
    GENERIC_NODE_TITLES,
    MASTERY_THRESHOLD,
    _build_low_score_adjustment,
    _build_node_explanation,
    _keyword_nodes,
    _plan_kb_ids,
    _prewarm_node_content,
)


def _duration_days(raw: str, explicit: int | None) -> int:
    if explicit:
        return explicit
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if digits:
        return max(1, min(365, int(digits)))
    return 7


def _normalize_node_status(raw: str | None, index: int) -> str:
    value = str(raw or "").strip().lower()
    if value == "done":
        return "done"
    if value == "locked":
        return "locked"
    return "open" if index == 0 else "locked"


def _serialize_plan(
    plan: LearningPlan,
    include_nodes: bool = False,
    *,
    detail_node_id: str | None = None,
) -> dict[str, Any]:
    """序列化计划。

    传入 ``detail_node_id`` 时只有该节点携带完整正文（explanation /
    explore_cache / resources），其余节点瘦身为 content_status 标志——
    dashboard/next-action 这类高频响应不再把每个节点的详解 Markdown
    全量重传；前端切换节点时按 content_status 经 GET /nodes/{id} 补水。
    """
    nodes = list(getattr(plan, "nodes", []) or [])
    payload = {
        "plan_id": plan.plan_id,
        "template_id": plan.template_id,
        "title": plan.title,
        "summary": plan.summary,
        "domain": plan.domain,
        "goal": plan.goal,
        "baseline": plan.baseline,
        "depth": plan.depth,
        "duration_days": plan.duration_days,
        "daily_minutes": plan.daily_minutes,
        "kb_ids": plan.kb_ids or [],
        "status": plan.status,
        "progress": int(plan.progress or 0),
        "mastery": int(plan.mastery or 0),
        "accent": plan.accent,
        "surface": plan.surface,
        "course_type": plan.course_type,
        "current_node_id": plan.current_node_id,
        "metadata": plan.metadata_ or {},
        "style_applied": bool((plan.style_snapshot or {}).get("enabled")),
        "started_at": plan.started_at.isoformat() if plan.started_at else None,
        "completed_at": plan.completed_at.isoformat() if plan.completed_at else None,
        "archived_at": plan.archived_at.isoformat() if plan.archived_at else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        "stats": {
            "total_nodes": len(nodes),
            "done_nodes": len([node for node in nodes if node.status == "done"]),
            "open_nodes": len([node for node in nodes if node.status in {"open", "learning", "review"}]),
            "review_nodes": len([node for node in nodes if _is_pending_review_node(node)]),
            "locked_nodes": len([node for node in nodes if node.status == "locked"]),
        },
    }
    if include_nodes:
        payload["nodes"] = [
            _serialize_node(
                node,
                slim=bool(detail_node_id) and node.node_id != detail_node_id,
            )
            for node in nodes
        ]
    return payload


def _serialize_node(node: LearningPlanNode, *, slim: bool = False) -> dict[str, Any]:
    """序列化节点；``slim=True`` 时剥离大字段正文，只保留 content_status 标志。"""
    explore_cache = node.explore_cache or {}
    return {
        "node_id": node.node_id,
        "plan_id": node.plan_id,
        "parent_node_id": node.parent_node_id,
        "order_index": node.order_index,
        "title": node.title,
        "subtitle": node.subtitle,
        "objective": node.objective,
        "explanation": None if slim else node.explanation,
        "resource": node.resource,
        "task": node.task,
        "check": node.check,
        "day": node.day,
        "node_type": node.node_type,
        "difficulty": node.difficulty,
        "estimated_minutes": node.estimated_minutes,
        "status": node.status,
        "state": node.status,
        "mastery": int(node.mastery or 0),
        "prerequisites": node.prerequisites or [],
        "resources": [] if slim else (node.resources or []),
        "tasks": node.tasks or [],
        "checks": node.checks or [],
        "study_signals": node.study_signals or {},
        "explore_cache": {} if slim else explore_cache,
        "content_status": {
            "explanation_ready": bool(node.explanation),
            "quiz_ready": bool(node.quiz_questions),
            "explore_ready": bool(explore_cache),
            "detail_ready": bool(explore_cache.get("detail_content")),
            "map_ready": bool(explore_cache.get("map")),
        },
        "completed_at": node.completed_at.isoformat() if node.completed_at else None,
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }


def _serialize_adjustment(adjustment: LearningPlanAdjustment | None) -> dict[str, Any] | None:
    if not adjustment:
        return None
    return {
        "adjustment_id": adjustment.adjustment_id,
        "plan_id": adjustment.plan_id,
        "trigger_type": adjustment.trigger_type,
        "reason": adjustment.reason,
        "accepted": adjustment.accepted,
        "before_snapshot": adjustment.before_snapshot or {},
        "after_snapshot": adjustment.after_snapshot or {},
        "created_at": adjustment.created_at.isoformat() if adjustment.created_at else None,
    }


def _ordered_nodes(plan: LearningPlan) -> list[LearningPlanNode]:
    return sorted(list(plan.nodes or []), key=lambda item: item.order_index)


def _is_pending_review_node(node: LearningPlanNode) -> bool:
    if node.status == "review":
        return True
    if node.node_type == "review" and node.status in {"open", "learning"}:
        return True
    return False


def _open_review_child(plan: LearningPlan, parent_node_id: str) -> LearningPlanNode | None:
    return next(
        (
            node
            for node in _ordered_nodes(plan)
            if node.parent_node_id == parent_node_id
            and node.node_type == "review"
            and node.status in {"open", "learning"}
        ),
        None,
    )


def _has_blocking_review_parent(plan: LearningPlan, node: LearningPlanNode) -> bool:
    for item in _ordered_nodes(plan):
        if item.order_index >= node.order_index:
            break
        if item.status == "review" and item.node_type != "review":
            return True
    return False


def _pick_focus_node(plan: LearningPlan) -> LearningPlanNode | None:
    nodes = _ordered_nodes(plan)
    review_parent_ids = {
        node.node_id for node in nodes if node.status == "review" and node.node_type != "review"
    }

    for node in nodes:
        if (
            node.node_type == "review"
            and node.status in {"open", "learning"}
            and node.parent_node_id in review_parent_ids
        ):
            return node

    for node in nodes:
        if node.status == "review" and node.node_type != "review":
            if not _open_review_child(plan, node.node_id):
                return node

    return next((node for node in nodes if node.status in {"open", "learning", "review"}), None)


def _reopen_parent_for_retry(plan: LearningPlan, review_child: LearningPlanNode) -> None:
    if not review_child.parent_node_id:
        return
    parent = next((node for node in plan.nodes or [] if node.node_id == review_child.parent_node_id), None)
    if parent and parent.status == "review":
        now = datetime.now()
        parent.status = "open"
        parent.study_signals = {
            **(parent.study_signals or {}),
            "ready_for_retry": True,
            "review_child_completed": review_child.node_id,
        }
        parent.updated_at = now


def _unlock_next_nodes(plan: LearningPlan, node: LearningPlanNode) -> list[str]:
    if node.node_type == "review" and node.parent_node_id:
        _reopen_parent_for_retry(plan, node)
        return []

    unlocked: list[str] = []
    for next_node in _ordered_nodes(plan):
        if next_node.order_index <= node.order_index or next_node.status != "locked":
            continue
        if _has_blocking_review_parent(plan, next_node):
            continue
        next_node.status = "open"
        _prewarm_node_content(plan, next_node)
        unlocked.append(next_node.node_id)
        break
    return unlocked


def _collect_weak_points(plan: LearningPlan, attempts: list[LearningNodeAttempt]) -> list[str]:
    latest_by_node: dict[str, LearningNodeAttempt] = {}
    ordered_attempts = sorted(
        attempts,
        key=lambda item: item.created_at or datetime.min,
        reverse=True,
    )
    for attempt in ordered_attempts:
        if attempt.node_id not in latest_by_node:
            latest_by_node[attempt.node_id] = attempt

    pending_node_ids = {node.node_id for node in _ordered_nodes(plan) if _is_pending_review_node(node)}
    weak_points: list[str] = []
    for node_id, attempt in latest_by_node.items():
        score = int(attempt.score or 0)
        if score >= MASTERY_THRESHOLD and node_id not in pending_node_ids:
            continue
        diagnosis = attempt.diagnosis or {}
        for item in diagnosis.get("weak_points", []) if isinstance(diagnosis, dict) else []:
            text = str(item).strip()
            if text and text not in weak_points:
                weak_points.append(text)
    return weak_points


def _finalize_node_assessment(
    plan: LearningPlan,
    node: LearningPlanNode,
    score: int,
    diagnosis: dict[str, Any] | None = None,
) -> tuple[LearningPlanAdjustment | None, list[str]]:
    before = int(node.mastery or 0)
    node.mastery = max(before, score)
    node.status = "done" if node.mastery >= MASTERY_THRESHOLD else "review"
    node.completed_at = datetime.now() if node.status == "done" else None
    node.updated_at = datetime.now()

    adjustment = None
    unlocked: list[str] = []
    if node.status != "done":
        adjustment = _build_low_score_adjustment(plan, node, score=score, diagnosis=diagnosis)
    else:
        unlocked = _unlock_next_nodes(plan, node)
    return adjustment, unlocked


def _current_plan_node(plan: LearningPlan, node_id: str | None = None) -> LearningPlanNode | None:
    nodes = _ordered_nodes(plan)
    if node_id:
        return next((node for node in nodes if node.node_id == node_id), None)
    focus = _pick_focus_node(plan)
    if focus:
        return focus
    current_id = str(plan.current_node_id or "")
    if current_id:
        current = next((node for node in nodes if node.node_id == current_id), None)
        if current:
            return current
    return next((node for node in nodes if node.status in {"open", "learning", "review"}), None) or (nodes[0] if nodes else None)


def _activate_draft_plan(plan: LearningPlan) -> None:
    now = datetime.now()
    plan.status = "active"
    plan.started_at = plan.started_at or now
    nodes = _ordered_nodes(plan)
    if nodes and all(node.status == "locked" for node in nodes):
        nodes[0].status = "open"
        _prewarm_node_content(plan, nodes[0])
    _recalculate_plan(plan)


def _start_learning_node(plan: LearningPlan, node: LearningPlanNode, user: User, session: AsyncSession) -> None:
    if node.status == "locked":
        raise HTTPException(status_code=409, detail="前置节点尚未完成")
    started_now = node.status == "open"
    if started_now:
        node.status = "learning"
    if not node.explanation:
        node.explanation = _build_node_explanation(plan, node)
    node.study_signals = {
        **(node.study_signals or {}),
        "started": True,
        "content_status": "ready",
        "last_agent": "content_explainer",
    }
    node.updated_at = datetime.now()
    # 只在 open→learning 首次转换时记一条 study 尝试；
    # 重复点「进入详解」不再刷屏「最近练习」列表。
    if started_now:
        session.add(
            LearningNodeAttempt(
                plan_id=plan.plan_id,
                node_id=node.node_id,
                user_id=user.user_id,
                action_type="study",
                payload={"event": "node.started", "agent": "content_explainer"},
                mastery_before=node.mastery,
                mastery_after=node.mastery,
            )
        )
    _recalculate_plan(plan)


def _plan_has_learning_progress(plan: LearningPlan) -> bool:
    for node in _ordered_nodes(plan):
        if node.status in {"learning", "review", "done"}:
            return True
        if int(node.mastery or 0) > 0 or node.completed_at or node.quiz_questions:
            return True
    return False


def _regenerate_unstarted_plan_nodes(plan: LearningPlan) -> bool:
    if _plan_has_learning_progress(plan):
        return False

    kb_ids = _plan_kb_ids(plan)
    blueprint = AGENT_TEAM.generate_plan_blueprint(
        goal=str(plan.goal or plan.title or ""),
        baseline=str(plan.baseline or "unknown"),
        depth=str(plan.depth or "analysis"),
        duration_days=int(plan.duration_days or 7),
        daily_minutes=int(plan.daily_minutes or 90),
        kb_ids=kb_ids,
        style_snapshot=plan.style_snapshot,
    )
    now = datetime.now()
    nodes: list[LearningPlanNode] = []
    for index, item in enumerate(blueprint.nodes):
        title = item.title
        tasks = [part for part in str(item.task or "").split("；") if part]
        item_check = item.check or f"能解释“{title}”的关键概念、适用条件和常见误区"
        status = "open" if plan.status == "active" and index == 0 else "locked"
        node = LearningPlanNode(
            node_id=str(uuid4()),
            plan_id=plan.plan_id,
            order_index=index,
            title=title,
            subtitle=item.subtitle,
            objective=item.objective or item.subtitle,
            day=f"Day {min(index + 1, int(plan.duration_days or 7))}",
            status=status,
            node_type=item.node_type,
            difficulty=item.difficulty,
            resource=item.resource or f"围绕“{title}”检索关联知识库资料",
            task="；".join(tasks[:3]) if tasks else f"整理“{title}”学习笔记",
            check=item_check,
            resources=[],
            tasks=tasks,
            checks=[item_check],
            estimated_minutes=item.estimated_minutes,
            study_signals={
                "agent_notes": item.agent_notes,
                "planned_by": "custom_learning_agent_team",
                "regenerated_at": now.isoformat(),
            },
        )
        if status == "open":
            _prewarm_node_content(plan, node)
        nodes.append(node)

    plan.nodes = nodes
    plan.summary = blueprint.summary
    plan.current_node_id = nodes[0].node_id if nodes and plan.status == "active" else None
    plan.metadata_ = {
        **(plan.metadata_ or {}),
        "generator": "agent-team-v2",
        "agent_trace": blueprint.agent_trace,
        "objectives": blueprint.objectives,
        "projects": blueprint.projects,
        "weekly_plan": blueprint.weekly_plan,
        "path_regenerated_at": now.isoformat(),
        "path_regeneration_skipped": False,
    }
    plan.updated_at = now
    return True


async def _owned_plan(
    plan_id: str,
    user: User,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> LearningPlan:
    """按属主加载计划；for_update=True 时对计划行加排他锁。

    同一用户多端并发推进（complete/quiz-grade 双写）此前靠读-改-写，
    会产生双 attempt、解锁互相覆盖；行锁把同一计划的变更串行化。
    """
    stmt = (
        select(LearningPlan)
        .where(LearningPlan.plan_id == plan_id, LearningPlan.user_id == user.user_id)
        .options(selectinload(LearningPlan.nodes))
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="学习计划不存在")
    return plan


async def _owned_node(
    node_id: str,
    user: User,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> tuple[LearningPlan, LearningPlanNode]:
    stmt = (
        select(LearningPlanNode, LearningPlan)
        .join(LearningPlan, LearningPlan.plan_id == LearningPlanNode.plan_id)
        .where(
            LearningPlanNode.node_id == node_id, LearningPlan.user_id == user.user_id
        )
        .options(selectinload(LearningPlan.nodes))
    )
    if for_update:
        stmt = stmt.with_for_update(of=LearningPlan)
    result = await session.execute(stmt)
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="学习节点不存在")
    node, plan = row
    return plan, node


def _recalculate_plan(plan: LearningPlan) -> None:
    nodes = sorted(list(plan.nodes or []), key=lambda item: item.order_index)
    if not nodes:
        plan.progress = 0
        plan.mastery = 0
        plan.current_node_id = None
        return

    done_count = len([node for node in nodes if node.status == "done"])
    plan.progress = round(done_count / len(nodes) * 100)
    plan.mastery = round(sum(int(node.mastery or 0) for node in nodes) / len(nodes))

    current = _pick_focus_node(plan)
    if current is None:
        current = next((node for node in nodes if node.status != "done"), None)
    plan.current_node_id = current.node_id if current else nodes[-1].node_id

    now = datetime.now()
    if done_count == len(nodes):
        plan.status = "completed"
        plan.completed_at = plan.completed_at or now
        plan.progress = 100
    elif plan.status == "completed":
        plan.status = "active"
        plan.completed_at = None
    plan.updated_at = now


def _is_placeholder_text(value: str | None) -> bool:
    text = str(value or "")
    return any(item in text for item in GENERIC_NODE_TITLES) or "最需要先确认的是哪一项" in text


def _node_needs_content_repair(node: LearningPlanNode) -> bool:
    title = str(node.title or "")
    clean_title = title.replace("复习：", "", 1).strip()
    return (
        clean_title in GENERIC_NODE_TITLES
        or _is_placeholder_text(node.subtitle)
        or _is_placeholder_text(node.objective)
        or _is_placeholder_text(node.task)
        or _is_placeholder_text(node.check)
        or _is_placeholder_text(node.explanation)
    )


def _quiz_needs_repair(questions: Any) -> bool:
    if not questions:
        return False
    try:
        text = " ".join(str(item.get("question") or item.get("stem") or "") for item in questions if isinstance(item, dict))
    except Exception:
        text = str(questions)
    return "最需要先确认的是哪一项" in text or "跳过基础，直接记忆结论" in text


def _repair_plan_content(plan: LearningPlan) -> bool:
    """把早期 rules-v1 生成的空泛节点修复为可学习内容，保留进度状态。"""
    nodes = sorted(list(plan.nodes or []), key=lambda item: item.order_index)
    if not nodes:
        return False
    goal = str(plan.goal or plan.title or "").strip()
    profile_nodes = _keyword_nodes(goal, max(plan.duration_days or len(nodes), len(nodes)))
    if not profile_nodes:
        return False

    changed = False
    profile_index = 0
    previous_profile: dict[str, Any] | None = None
    now = datetime.now()

    for node in nodes:
        node_changed = False
        is_review = node.node_type == "review" or str(node.title or "").startswith("复习：")
        if is_review:
            profile = previous_profile or profile_nodes[min(profile_index, len(profile_nodes) - 1)]
        else:
            profile = profile_nodes[min(profile_index, len(profile_nodes) - 1)]
            previous_profile = profile
            profile_index += 1

        needs_repair = _node_needs_content_repair(node)
        if needs_repair:
            base_title = str(profile.get("title") or node.title)
            node.title = f"复习：{base_title}" if is_review else base_title
            node.subtitle = "根据练习结果补强薄弱点" if is_review else profile.get("subtitle", node.subtitle)
            node.objective = (
                f"修正“{base_title}”的薄弱点，并重新完成验收标准"
                if is_review
                else profile.get("objective", node.objective or node.subtitle)
            )
            node.resource = profile.get("resource", node.resource)
            base_task = profile.get("task", node.task or f"整理“{base_title}”学习笔记")
            node.task = f"复盘错题并重新完成：{base_task}" if is_review else base_task
            node.check = profile.get("check", node.check or f"能解释“{base_title}”的关键概念")
            node.tasks = [node.task, "完成节点练习"]
            node.checks = [node.check]
            node.explanation = None if node.status == "locked" else _build_node_explanation(plan, node)
            node_changed = True

        if _quiz_needs_repair(node.quiz_questions):
            node.quiz_questions = None
            node_changed = True

        if node.status != "locked" and (not node.explanation or _is_placeholder_text(node.explanation)):
            node.explanation = _build_node_explanation(plan, node)
            node_changed = True

        if node_changed:
            node.updated_at = now
            changed = True

    if changed:
        metadata = dict(plan.metadata_ or {})
        metadata["generator"] = "rules-v2"
        metadata["content_repaired_at"] = now.isoformat()
        plan.metadata_ = metadata
        plan.updated_at = now
    return changed
