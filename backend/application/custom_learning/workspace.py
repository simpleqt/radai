"""学习工作台聚合：模块效果、复盘摘要、仪表盘序列化与下一步建议。"""

from __future__ import annotations

from backend.db.models import (
    LearningNodeAttempt,
    LearningPlan,
    LearningPlanAdjustment,
    LearningPlanNode,
    User,
)
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any
from backend.application.custom_learning.common import _style_summary
from backend.application.custom_learning.plan_progress import (
    _collect_weak_points,
    _current_plan_node,
    _is_pending_review_node,
    _open_review_child,
    _ordered_nodes,
    _serialize_adjustment,
    _serialize_node,
    _serialize_plan,
)


def _module_effects(
    plan: LearningPlan,
    *,
    review: dict[str, Any] | None = None,
    style: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Summarize how each learning module is helping the current plan."""
    nodes = _ordered_nodes(plan)
    unlocked_nodes = [node for node in nodes if node.status != "locked"]
    explanation_ready = [node for node in nodes if node.explanation]
    prewarmed_nodes = [
        node
        for node in nodes
        if isinstance(node.study_signals, dict) and node.study_signals.get("prewarmed")
    ]
    explore_ready = [node for node in nodes if node.explore_cache]
    quiz_ready = [node for node in nodes if node.quiz_questions]
    review = review or {}
    style = style or _style_summary(plan.style_snapshot)
    weak_points = review.get("weak_points") or []
    attempts = review.get("latest_attempts") or []
    review_nodes = review.get("review_nodes") or []

    if plan.status == "draft":
        path_status = "waiting"
        path_description = "计划处于草稿状态，确认后会按顺序解锁第一个节点。"
    elif plan.status == "completed":
        path_status = "ready"
        path_description = "计划已完成，可通过复习节点继续巩固薄弱点。"
    else:
        path_status = "active"
        path_description = f"已解锁 {len(unlocked_nodes)}/{len(nodes)} 个节点，当前进度由节点完成情况实时计算。"

    return [
        {
            "id": "custom_path",
            "title": "自定义路径",
            "status": path_status,
            "metric": f"{int(plan.progress or 0)}% 进度",
            "description": path_description,
            "impact": "让目标、节点、验收标准和下一步动作保持一致。",
        },
        {
            "id": "explore",
            "title": "探索式学习",
            "status": "active" if plan.kb_ids and explore_ready else ("waiting" if plan.kb_ids else "idle"),
            "metric": f"{len(explore_ready)}/{max(len(unlocked_nodes), 1)} 节点有资料",
            "description": "绑定知识库后，节点会按需召回资料、知识点和补充阅读。",
            "impact": "把探索页发现的知识点转成可执行学习材料。",
        },
        {
            "id": "learning_style",
            "title": "学习风格",
            "status": "active" if style.get("enabled") else "idle",
            "metric": "、".join((style.get("labels") or [])[:2]) or "默认策略",
            "description": style.get("explanation_hint") or "按默认讲解、任务和练习节奏推进。",
            "impact": "影响讲解形式、任务提示和练习安排。",
        },
        {
            "id": "practice",
            "title": "练习诊断",
            "status": "risk" if weak_points or review_nodes else ("active" if attempts or quiz_ready else "waiting"),
            "metric": f"{len(attempts)} 次记录",
            "description": (
                f"已识别 {len(weak_points)} 个薄弱点，{len(review_nodes)} 个复习节点。"
                if weak_points or review_nodes
                else "生成练习后会更新掌握度，并在低分时插入复习节点。"
            ),
            "impact": "用分数、薄弱点和复习节点驱动路径调整。",
        },
        {
            "id": "response_speed",
            "title": "响应速度",
            "status": "active" if prewarmed_nodes else ("ready" if explanation_ready else "waiting"),
            "metric": f"{len(prewarmed_nodes)} 个节点已预热",
            "description": f"{len(explanation_ready)} 个节点详解已就绪，解锁后优先预生成下一节点内容。",
            "impact": "减少开始学习和切换节点时的等待。",
        },
    ]


def _workspace_module_effects(
    plans: list[LearningPlan],
    *,
    style: dict[str, Any],
    knowledge_bases: list[dict[str, str]],
) -> list[dict[str, Any]]:
    active_plans = [plan for plan in plans if plan.status == "active"]
    draft_plans = [plan for plan in plans if plan.status == "draft"]
    completed_plans = [plan for plan in plans if plan.status == "completed"]
    attempts_ready = sum(
        1
        for plan in plans
        for node in list(getattr(plan, "nodes", []) or [])
        if node.quiz_questions or node.status in {"done", "review"}
    )
    prewarmed = sum(
        1
        for plan in plans
        for node in list(getattr(plan, "nodes", []) or [])
        if isinstance(node.study_signals, dict) and node.study_signals.get("prewarmed")
    )
    return [
        {
            "id": "custom_path",
            "title": "自定义路径",
            "status": "active" if active_plans else ("waiting" if draft_plans else "idle"),
            "metric": f"{len(active_plans)} 个进行中",
            "description": f"{len(draft_plans)} 个草稿、{len(completed_plans)} 个已完成计划。",
            "impact": "首页优先推荐正在推进或刚生成的学习计划。",
        },
        {
            "id": "explore",
            "title": "探索式学习",
            "status": "active" if knowledge_bases else "idle",
            "metric": f"{len(knowledge_bases)} 个知识库",
            "description": "可从探索页把目标、前置或复习意图带入自定义学习。",
            "impact": "减少从发现知识点到生成计划的断点。",
        },
        {
            "id": "learning_style",
            "title": "学习风格",
            "status": "active" if style.get("enabled") else "idle",
            "metric": "、".join((style.get("labels") or [])[:2]) or "默认策略",
            "description": style.get("explanation_hint") or "尚未启用学习风格，将使用默认学习策略。",
            "impact": "用于生成计划、讲解提示和任务安排。",
        },
        {
            "id": "practice",
            "title": "练习诊断",
            "status": "active" if attempts_ready else "waiting",
            "metric": f"{attempts_ready} 个节点有练习状态",
            "description": "练习记录会回流到复习节点和路径调整。",
            "impact": "让学习闭环从阅读推进到可验证掌握。",
        },
        {
            "id": "response_speed",
            "title": "响应速度",
            "status": "active" if prewarmed else "waiting",
            "metric": f"{prewarmed} 个节点已预热",
            "description": "激活计划和解锁节点时提前准备详解内容。",
            "impact": "降低点击下一步后的生成等待。",
        },
    ]


async def _plan_review_summary(
    plan: LearningPlan,
    user: User,
    session: AsyncSession,
) -> dict[str, Any]:
    attempts_result = await session.execute(
        select(LearningNodeAttempt)
        .where(LearningNodeAttempt.plan_id == plan.plan_id, LearningNodeAttempt.user_id == user.user_id)
        .order_by(desc(LearningNodeAttempt.created_at))
        .limit(20)
    )
    attempts = attempts_result.scalars().all()
    adjustments_result = await session.execute(
        select(LearningPlanAdjustment)
        .where(LearningPlanAdjustment.plan_id == plan.plan_id)
        .order_by(desc(LearningPlanAdjustment.created_at))
        .limit(10)
    )
    adjustments = adjustments_result.scalars().all()
    weak_points = _collect_weak_points(plan, list(attempts))
    review_nodes = [
        _serialize_node(node)
        for node in _ordered_nodes(plan)
        if _is_pending_review_node(node)
    ]
    return {
        "review_nodes": review_nodes,
        "weak_points": weak_points[:8],
        "latest_attempts": [
            {
                "attempt_id": item.attempt_id,
                "node_id": item.node_id,
                "action_type": item.action_type,
                "score": item.score,
                "diagnosis": item.diagnosis or {},
                "duration_seconds": item.duration_seconds,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in attempts[:8]
        ],
        "adjustments": [_serialize_adjustment(item) for item in adjustments],
    }


async def _serialize_plan_dashboard(
    plan: LearningPlan,
    user: User,
    session: AsyncSession,
    *,
    selected_node_id: str | None = None,
) -> dict[str, Any]:
    current = _current_plan_node(plan, selected_node_id)
    review = await _plan_review_summary(plan, user, session)
    style = _style_summary(plan.style_snapshot)
    return {
        # 只有当前节点携带完整正文，其余节点瘦身（前端按 content_status 补水）。
        "plan": _serialize_plan(plan, include_nodes=True, detail_node_id=current.node_id if current else None),
        "current_node": _serialize_node(current) if current else None,
        "review": review,
        "style": style,
        "agent_trace": (plan.metadata_ or {}).get("agent_trace", []),
        "next_recommendation": _next_action_hint(plan, current),
        "module_effects": _module_effects(plan, review=review, style=style),
    }


def _next_action_hint(plan: LearningPlan, node: LearningPlanNode | None) -> dict[str, Any]:
    if plan.status == "draft":
        return {"action": "activate_plan", "label": "确认并开始计划", "reason": "计划仍是草稿，开始后会解锁第一个节点"}
    if not node:
        return {"action": "none", "label": "暂无可执行节点", "reason": "当前计划没有节点"}
    if node.status == "locked":
        return {"action": "locked", "label": "前置节点未完成", "reason": "需要先完成前置节点"}
    if node.node_type == "review" and node.status in {"open", "learning", "review"}:
        if not node.explanation:
            return {"action": "start_node", "label": "开始复习", "reason": "生成复习详解，针对薄弱点补强后再练习"}
        if not node.quiz_questions:
            return {"action": "generate_quiz", "label": "生成复习练习", "reason": "通过复习练习验证补强效果"}
        if node.status != "done":
            return {"action": "await_quiz", "label": "提交复习练习", "reason": "完成复习练习后，回到原节点重新检测"}
    if not node.explanation:
        return {"action": "start_node", "label": "开始学习", "reason": "生成节点详解并进入学习中状态"}
    cache = node.explore_cache or {}
    if plan.kb_ids and not cache.get("detail_content"):
        return {
            "action": "enrich_content",
            "label": "加载知识库详解",
            "reason": "计划已绑定知识库，可结合资料生成更完整的节点讲解",
        }
    if plan.kb_ids and not cache.get("map"):
        return {"action": "explore_node", "label": "检索资料", "reason": "当前计划绑定了知识库，可补充阅读材料与知识图谱"}
    if not node.quiz_questions and node.status != "done":
        return {"action": "generate_quiz", "label": "生成练习", "reason": "通过练习更新掌握度"}
    if node.status == "done":
        return {"action": "advance_node", "label": "查看下一节点", "reason": "当前节点已完成"}
    review_child = _open_review_child(plan, node.node_id) if node.status == "review" else None
    if review_child:
        return {
            "action": "goto_review_child",
            "label": "先完成复习节点",
            "reason": f"请先完成“{review_child.title}”，再回到本节点重新检测",
            "node_id": review_child.node_id,
        }
    if node.status == "review":
        if not node.quiz_questions:
            return {"action": "generate_quiz", "label": "生成练习", "reason": "上次未达标，请重新生成练习并检测掌握度"}
        return {"action": "retry_quiz", "label": "重新提交练习", "reason": "上次未达标，请完成复习后重新检测"}
    return {"action": "await_quiz", "label": "提交练习", "reason": "已有练习题，提交后会诊断薄弱点"}
