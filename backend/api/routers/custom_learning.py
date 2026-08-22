"""自定义学习 API（路由层）。

业务逻辑已下沉到 ``backend.application.custom_learning``（common /
plan_progress / content_quiz / workspace）；本模块只保留路由接线，
并为既有测试 re-export 下划线辅助符号。
"""

from __future__ import annotations

from backend.api.routers.auth import get_current_user
from backend.api.schemas.custom_learning import (
    AdjustPlanRequest,
    CompleteNodeRequest,
    CreatePlanRequest,
    EnrichNodeRequest,
    ExploreNodeRequest,
    GeneratePlanRequest,
    GradeQuizRequest,
    NextActionRequest,
    QuizRequest,
    UpdatePlanRequest,
)
from backend.config.loggers import get_logger
from backend.db.connection import get_async_session
from backend.db.models import (
    LearningNodeAttempt,
    LearningPlan,
    LearningPlanAdjustment,
    LearningPlanNode,
    LearningStyleProfile,
    User,
)
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from typing import Any
from backend.application.custom_learning.common import (
    AGENT_TEAM,
    MASTERY_THRESHOLD,
    _build_low_score_adjustment,
    _build_node_explanation,
    _generate_plan_blueprint,
    _list_selectable_knowledge_bases,
    _resolve_agent_code,
    _style_strategy,
    _style_summary,
    _validate_selectable_kb_ids,
)
from backend.application.custom_learning.common import _grade_quiz  # noqa: F401  (测试兼容 re-export；路由判分已走 _grade_quiz_with_llm)
from backend.application.custom_learning.content_quiz import (
    _apply_explore_snapshot_to_node,
    _ensure_node_learning_content,
    _explore_learning_node,
    _generate_node_quiz,
    _grade_quiz_with_llm,
    _public_quiz_questions,
    _refresh_node_quiz,
    _score_learning_artifact,
)
from backend.application.custom_learning.plan_progress import (
    _activate_draft_plan,
    _current_plan_node,
    _duration_days,
    _finalize_node_assessment,
    _normalize_node_status,
    _owned_node,
    _owned_plan,
    _pick_focus_node,
    _recalculate_plan,
    _regenerate_unstarted_plan_nodes,
    _repair_plan_content,
    _serialize_adjustment,
    _serialize_node,
    _serialize_plan,
    _start_learning_node,
)
from backend.application.custom_learning.workspace import (
    _plan_review_summary,
    _serialize_plan_dashboard,
    _workspace_module_effects,
)
from backend.application.custom_learning.common import _build_multi_agent_quiz  # noqa: F401  (测试兼容 re-export)
from backend.application.custom_learning.plan_progress import _collect_weak_points  # noqa: F401  (测试兼容 re-export)
from backend.application.custom_learning.plan_progress import _is_pending_review_node  # noqa: F401  (测试兼容 re-export)
from backend.application.custom_learning.common import _keyword_nodes  # noqa: F401  (测试兼容 re-export)
from backend.application.custom_learning.content_quiz import _merge_node_explanation  # noqa: F401  (测试兼容 re-export)
from backend.application.custom_learning.workspace import _module_effects  # noqa: F401  (测试兼容 re-export)
from backend.application.custom_learning.workspace import _next_action_hint  # noqa: F401  (测试兼容 re-export)
from backend.application.custom_learning.content_quiz import _node_learning_path  # noqa: F401  (测试兼容 re-export)
from backend.application.custom_learning.common import _normalize_kb_ids  # noqa: F401  (测试兼容 re-export)
from backend.application.custom_learning.common import _prewarm_node_content  # noqa: F401  (测试兼容 re-export)
from backend.application.custom_learning.common import _style_task_hints  # noqa: F401  (测试兼容 re-export)
from backend.application.custom_learning.plan_progress import _unlock_next_nodes  # noqa: F401  (测试兼容 re-export)


logger = get_logger(__name__)
router = APIRouter(prefix="/custom-learning", tags=["自定义学习"])


@router.get("/knowledge-bases")
async def list_knowledge_bases(
    agent_code: str | None = Query(default=None, description="对话智能体编码"),
    domain: str | None = Query(default=None, description="学习领域，用于映射智能体"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """列出指定对话智能体绑定的知识库。"""
    resolved_agent_code = _resolve_agent_code(domain=domain, agent_code=agent_code)
    items = await _list_selectable_knowledge_bases(session, resolved_agent_code)
    return {"items": items, "agent_code": resolved_agent_code}


@router.get("/workspace")
async def learning_workspace(
    domain: str | None = Query(default="ne", description="学习领域，用于映射智能体"),
    agent_code: str | None = Query(default=None, description="对话智能体编码"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """自定义学习首页聚合数据，减少前端首屏串行请求。"""
    resolved_agent_code = _resolve_agent_code(domain=domain, agent_code=agent_code)
    plans_result = await session.execute(
        select(LearningPlan)
        .where(LearningPlan.user_id == user.user_id, LearningPlan.status != "archived")
        .options(selectinload(LearningPlan.nodes))
        .order_by(LearningPlan.updated_at.desc())
        .limit(30)
    )
    plans = plans_result.scalars().all()
    profile_result = await session.execute(
        select(LearningStyleProfile).where(LearningStyleProfile.user_id == user.user_id)
    )
    profile = profile_result.scalar_one_or_none()
    style_snapshot = {
        "enabled": bool(profile and profile.apply_to_ai),
        "scores": profile.scores if profile else {},
        "result": profile.result if profile else {},
        "latest_attempt_id": profile.latest_attempt_id if profile else None,
    }
    draft_plans = [plan for plan in plans if plan.status == "draft"]
    active_plans = [plan for plan in plans if plan.status == "active"]
    completed_plans = [plan for plan in plans if plan.status == "completed"]
    average_progress = round(sum(int(plan.progress or 0) for plan in plans) / len(plans)) if plans else 0
    style = {
        **_style_summary(style_snapshot),
        "profile_updated_at": profile.updated_at.isoformat() if profile and profile.updated_at else None,
    }
    knowledge_bases = await _list_selectable_knowledge_bases(session, resolved_agent_code)
    return {
        "plans": [_serialize_plan(plan) for plan in plans],
        "recommended_plan": _serialize_plan(active_plans[0]) if active_plans else None,
        "draft_plans": [_serialize_plan(plan) for plan in draft_plans[:5]],
        "active_plans": [_serialize_plan(plan) for plan in active_plans[:8]],
        "completed_plans": [_serialize_plan(plan) for plan in completed_plans[:5]],
        "stats": {
            "total": len(plans),
            "draft": len(draft_plans),
            "active": len(active_plans),
            "completed": len(completed_plans),
            "average_progress": average_progress,
        },
        "style": style,
        "knowledge_bases": knowledge_bases,
        "agent_code": resolved_agent_code,
        "module_effects": _workspace_module_effects(plans, style=style, knowledge_bases=knowledge_bases),
    }


@router.get("/plans")
async def list_plans(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(LearningPlan)
        .where(LearningPlan.user_id == user.user_id, LearningPlan.status != "archived")
        .options(selectinload(LearningPlan.nodes))
        .order_by(LearningPlan.updated_at.desc())
    )
    plans = result.scalars().all()
    return {"plans": [_serialize_plan(plan) for plan in plans]}


@router.post("/plans")
async def create_plan_from_template(
    payload: CreatePlanRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    if not payload.nodes:
        raise HTTPException(status_code=422, detail="学习计划至少需要一个节点")

    kb_ids = await _validate_selectable_kb_ids(
        session,
        _resolve_agent_code(domain=payload.domain),
        payload.kb_ids,
    )

    plan = LearningPlan(
        user_id=user.user_id,
        template_id=payload.template_id,
        title=payload.title.strip(),
        summary=payload.summary or payload.subtitle,
        domain=payload.domain or "ne",
        goal=payload.goal or payload.summary or payload.subtitle,
        baseline=payload.baseline,
        depth=payload.depth,
        duration_days=_duration_days(payload.duration, payload.duration_days),
        daily_minutes=payload.daily_minutes,
        kb_ids=kb_ids,
        status="draft",
        accent=payload.accent,
        surface=payload.surface or payload.soft,
        course_type=payload.course_type,
        metadata_={
            "objectives": [item.model_dump() for item in payload.objectives],
            "resources": [item.model_dump() for item in payload.resources],
            "projects": [item.model_dump() for item in payload.projects],
            "weekly_plan": payload.weekly_plan,
            "source": "template",
        },
    )
    session.add(plan)
    await session.flush()

    default_resource = payload.resources[0].title if payload.resources else ""
    default_task = payload.projects[0].title if payload.projects else ""
    default_check = (
        payload.objectives[0].checkpoints[0]
        if payload.objectives and payload.objectives[0].checkpoints
        else ""
    )

    for index, item in enumerate(payload.nodes):
        status = _normalize_node_status(item.state or item.status, index)
        node = LearningPlanNode(
            plan_id=plan.plan_id,
            order_index=index,
            title=item.title,
            subtitle=item.subtitle,
            day=item.day or f"Day {index + 1}",
            status=status,
            mastery=100 if status == "done" else 0,
            resource=item.resource or default_resource,
            task=item.task or default_task,
            check=item.check or default_check,
            objective=item.subtitle or item.title,
            resources=[res.model_dump() for res in payload.resources[:3]],
            tasks=[item.task or default_task] if (item.task or default_task) else [],
            checks=[item.check or default_check] if (item.check or default_check) else [],
            estimated_minutes=max(30, payload.daily_minutes),
        )
        session.add(node)

    await session.flush()
    result = await session.execute(
        select(LearningPlan)
        .where(LearningPlan.plan_id == plan.plan_id)
        .options(selectinload(LearningPlan.nodes))
    )
    plan = result.scalar_one()
    _repair_plan_content(plan)
    _recalculate_plan(plan)
    await session.flush()
    return {"plan": _serialize_plan(plan, include_nodes=True)}


@router.post("/plans/generate")
async def generate_plan(
    payload: GeneratePlanRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """生成并保存一个结构化计划草稿。

    命中人工领域模板的目标走确定性规则；通用目标优先 LLM 蓝图
    （失败/超时/输出非法回退规则骨架），响应 schema 不变。
    """
    style_snapshot: dict[str, Any] = {"enabled": False}
    if payload.use_learning_style:
        style_result = await session.execute(
            select(LearningStyleProfile).where(LearningStyleProfile.user_id == user.user_id)
        )
        profile = style_result.scalar_one_or_none()
        if profile and profile.apply_to_ai:
            style_snapshot = {
                "enabled": True,
                "scores": profile.scores or {},
                "result": profile.result or {},
                "latest_attempt_id": profile.latest_attempt_id,
            }
        else:
            style_snapshot = {"enabled": False, "reason": "profile_missing_or_disabled"}

    style_enabled = bool(style_snapshot.get("enabled"))
    if style_enabled:
        style_snapshot["strategy"] = _style_strategy(style_snapshot)
    kb_ids = await _validate_selectable_kb_ids(
        session,
        _resolve_agent_code(domain=payload.domain),
        payload.kb_ids,
    )
    # LLM 蓝图（最长 30s+20s 封顶）期间不持有 MySQL 连接：提交当前只读
    # 事务归还连接（expire_on_commit=False，已加载对象仍可用）。
    await session.commit()
    blueprint = await _generate_plan_blueprint(
        goal=payload.goal,
        baseline=payload.baseline,
        depth=payload.depth,
        duration_days=payload.duration_days,
        daily_minutes=payload.daily_minutes,
        kb_ids=kb_ids,
        style_snapshot=style_snapshot,
    )
    explore_snapshot = payload.explore_snapshot if isinstance(payload.explore_snapshot, dict) else None
    explore_node_title = str((explore_snapshot or {}).get("node_title") or "").strip()
    plan = LearningPlan(
        user_id=user.user_id,
        title=blueprint.title,
        summary=blueprint.summary,
        domain=payload.domain,
        goal=payload.goal,
        baseline=payload.baseline,
        depth=payload.depth,
        duration_days=payload.duration_days,
        daily_minutes=payload.daily_minutes,
        kb_ids=kb_ids,
        style_snapshot=style_snapshot,
        status="draft",
        accent="#2563eb",
        surface="#eff6ff",
        course_type="自定义学习",
        metadata_={
            "source": "generated",
            "generator": "agent-team-v2",
            "planner_source": (blueprint.agent_trace[0].get("source", "keyword_rules") if blueprint.agent_trace else "keyword_rules"),
            "style_applied": style_enabled,
            "style_task_hints": blueprint.style_task_hints,
            "agent_trace": blueprint.agent_trace,
            "objectives": blueprint.objectives,
            "projects": blueprint.projects,
            "weekly_plan": blueprint.weekly_plan,
            "explore_snapshot_applied": bool(explore_snapshot),
            "explore_snapshot_topic": explore_node_title,
        },
    )
    session.add(plan)
    await session.flush()

    for index, item in enumerate(blueprint.nodes):
        title = explore_node_title if index == 0 and explore_node_title else item.title
        subtitle = item.subtitle
        objective = item.objective or subtitle
        if style_enabled and index == 0:
            subtitle = f"{subtitle}；本计划会结合你的学习偏好安排图表、练习和复盘。"
        if explore_node_title and index == 0:
            subtitle = f"继承探索式学习“{explore_node_title}”的已有资料与练习。"
            objective = f"围绕“{explore_node_title}”完成首个学习闭环，并把探索结果转化为可检测掌握。"
        tasks = [part for part in str(item.task or "").split("；") if part]
        item_check = item.check or f"能解释“{title}”的关键概念、适用条件和常见误区"
        node = LearningPlanNode(
            plan_id=plan.plan_id,
            order_index=index,
            title=title,
            subtitle=subtitle,
            objective=objective,
            day=f"Day {min(index + 1, payload.duration_days)}",
            status="locked",
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
            },
        )
        if index == 0 and explore_snapshot:
            _apply_explore_snapshot_to_node(plan, node, explore_snapshot)
        session.add(node)

    await session.flush()
    result = await session.execute(
        select(LearningPlan)
        .where(LearningPlan.plan_id == plan.plan_id)
        .options(selectinload(LearningPlan.nodes))
    )
    plan = result.scalar_one()
    _recalculate_plan(plan)
    await session.flush()
    return {"plan": _serialize_plan(plan, include_nodes=True)}


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan = await _owned_plan(plan_id, user, session)
    if _repair_plan_content(plan):
        _recalculate_plan(plan)
        # get_async_session 对 GET 一律 rollback,flush 的修复从不落库、每次
        # 读取都重复计算(R4-RT-2)。读修复是幂等的确定性重算,显式提交。
        await session.commit()
    return {"plan": _serialize_plan(plan, include_nodes=True)}


@router.get("/plans/{plan_id}/dashboard")
async def get_plan_dashboard(
    plan_id: str,
    node_id: str | None = Query(default=None, description="前端当前选中的节点"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan = await _owned_plan(plan_id, user, session)
    if _repair_plan_content(plan):
        _recalculate_plan(plan)
        # 同 get_plan:GET 会话默认回滚,读修复需显式提交(R4-RT-2)。
        await session.commit()
    return await _serialize_plan_dashboard(plan, user, session, selected_node_id=node_id)


@router.patch("/plans/{plan_id}")
async def update_plan(
    plan_id: str,
    payload: UpdatePlanRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan = await _owned_plan(plan_id, user, session, for_update=True)
    if plan.status == "archived":
        raise HTTPException(status_code=409, detail="已归档计划不能修改")

    update_data = payload.model_dump(exclude_unset=True)
    path_fields = {"goal", "baseline", "depth", "duration_days", "daily_minutes", "kb_ids"}
    before_values = {field: getattr(plan, field, None) for field in path_fields}
    if "kb_ids" in update_data:
        update_data["kb_ids"] = await _validate_selectable_kb_ids(
            session,
            _resolve_agent_code(domain=plan.domain),
            update_data.get("kb_ids"),
        )
    for field, value in update_data.items():
        if field == "title" and value is not None:
            plan.title = str(value).strip()
        elif hasattr(plan, field):
            setattr(plan, field, value)
    path_changed = any(
        field in update_data and before_values.get(field) != getattr(plan, field, None)
        for field in path_fields
    )
    if path_changed:
        metadata = dict(plan.metadata_ or {})
        if _regenerate_unstarted_plan_nodes(plan):
            metadata = dict(plan.metadata_ or {})
            metadata["path_regeneration_reason"] = "plan_fields_changed"
            plan.metadata_ = metadata
        else:
            metadata["path_regeneration_skipped"] = True
            metadata["path_regeneration_reason"] = "plan_already_started"
            metadata["path_regeneration_requested_at"] = datetime.now().isoformat()
            plan.metadata_ = metadata
    plan.updated_at = datetime.now()
    _recalculate_plan(plan)
    await session.flush()
    return {"plan": _serialize_plan(plan, include_nodes=True)}


@router.post("/plans/{plan_id}/activate")
async def activate_plan(
    plan_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan = await _owned_plan(plan_id, user, session, for_update=True)
    if plan.status == "archived":
        raise HTTPException(status_code=409, detail="已归档计划不能启用")
    _activate_draft_plan(plan)
    await session.flush()
    return {"plan": _serialize_plan(plan, include_nodes=True)}


@router.post("/plans/{plan_id}/next-action")
async def run_plan_next_action(
    plan_id: str,
    payload: NextActionRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    payload = payload or NextActionRequest()
    plan = await _owned_plan(plan_id, user, session, for_update=True)
    if plan.status == "archived":
        raise HTTPException(status_code=409, detail="已归档计划不能继续学习")

    action = "none"
    result: dict[str, Any] = {}
    adjustment: LearningPlanAdjustment | None = None

    if plan.status == "draft":
        _activate_draft_plan(plan)
        action = "activate_plan"
    else:
        node = _current_plan_node(plan, payload.node_id)
        if not node:
            action = "none"
        elif node.status == "locked":
            action = "locked"
        elif payload.answers is not None:
            if not node.quiz_questions:
                await session.commit()
                node.quiz_questions = await _generate_node_quiz(plan, node)
                await session.flush()
                await session.commit()
            await session.commit()
            score, diagnosis = await _grade_quiz_with_llm(node, list(node.quiz_questions or []), payload.answers)
            # 同 grade_quiz 路由：判分后写回前重取行锁，防并发双 attempt。
            plan = await _owned_plan(plan_id, user, session, for_update=True)
            node = _current_plan_node(plan, payload.node_id)
            if node is None or node.status == "locked":
                raise HTTPException(status_code=409, detail="学习节点状态已变化，请重试")
            before = int(node.mastery or 0)
            adjustment, unlocked_node_ids = _finalize_node_assessment(plan, node, score, diagnosis)
            if adjustment:
                session.add(adjustment)
            session.add(
                LearningNodeAttempt(
                    plan_id=plan.plan_id,
                    node_id=node.node_id,
                    user_id=user.user_id,
                    action_type="quiz",
                    payload={"answers": payload.answers, "source": "next_action"},
                    score=score,
                    diagnosis=diagnosis,
                    duration_seconds=payload.duration_seconds,
                    mastery_before=before,
                    mastery_after=node.mastery,
                    source="next_action",
                )
            )
            _recalculate_plan(plan)
            action = "grade_quiz"
            result = {
                "attempt": {
                    "score": score,
                    "diagnosis": diagnosis,
                    "mastery_before": before,
                    "mastery_after": node.mastery,
                },
                "unlocked_node_ids": unlocked_node_ids,
                "adjustment": _serialize_adjustment(adjustment),
            }
        elif not node.explanation:
            _start_learning_node(plan, node, user, session)
            action = "start_node"
        elif plan.kb_ids and not (node.explore_cache or {}).get("detail_content"):
            await session.commit()
            await _ensure_node_learning_content(plan, node)
            action = "enrich_content"
            result = {"node": _serialize_node(node)}
        elif payload.prefer_materials and plan.kb_ids and not (node.explore_cache or {}).get("map"):
            await session.commit()
            result = await _explore_learning_node(plan, node)
            action = "explore_node"
        elif not node.quiz_questions and node.status != "done":
            await session.commit()
            node.quiz_questions = await _generate_node_quiz(plan, node)
            node.study_signals = {**(node.study_signals or {}), "quiz_ready": True, "last_agent": "quiz_author"}
            node.updated_at = datetime.now()
            action = "generate_quiz"
            result = {"questions": _public_quiz_questions(node.quiz_questions)}
        elif node.status == "done":
            next_node = _pick_focus_node(plan)
            if next_node:
                plan.current_node_id = next_node.node_id
            action = "advance_node"
        else:
            action = "await_quiz"
            result = {"questions": _public_quiz_questions(node.quiz_questions)}

    await session.flush()
    dashboard = await _serialize_plan_dashboard(plan, user, session, selected_node_id=payload.node_id)
    dashboard["action"] = action
    dashboard["result"] = result
    return dashboard


@router.post("/plans/{plan_id}/archive")
async def archive_plan(
    plan_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan = await _owned_plan(plan_id, user, session, for_update=True)
    plan.status = "archived"
    plan.archived_at = datetime.now()
    plan.updated_at = datetime.now()
    await session.flush()
    return {"plan": _serialize_plan(plan)}


@router.post("/plans/{plan_id}/adjust")
async def adjust_plan(
    plan_id: str,
    payload: AdjustPlanRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    payload = payload or AdjustPlanRequest()
    plan = await _owned_plan(plan_id, user, session, for_update=True)
    nodes = sorted(plan.nodes or [], key=lambda item: item.order_index)
    current = next((node for node in nodes if node.node_id == plan.current_node_id), None)
    adjustment = None
    if payload.accept and current and current.status != "done":
        adjustment = _build_low_score_adjustment(
            plan,
            current,
            score=min(int(current.mastery or 0), 60),
            diagnosis={"weak_points": [payload.reason], "suggestions": ["按补救节点完成复习"]},
        )
    if adjustment is None:
        adjustment = LearningPlanAdjustment(
            plan_id=plan.plan_id,
            trigger_type=payload.trigger_type,
            before_snapshot={
                "progress": plan.progress,
                "mastery": plan.mastery,
                "current_node_id": plan.current_node_id,
                "node_count": len(nodes),
            },
            after_snapshot={
                "strategy": "manual_review_suggestion",
                "current_node_id": plan.current_node_id,
            },
            reason=payload.reason,
            accepted=payload.accept,
        )
    session.add(adjustment)
    _recalculate_plan(plan)
    await session.flush()
    return {"plan": _serialize_plan(plan, include_nodes=True), "adjustment": _serialize_adjustment(adjustment)}


@router.get("/plans/{plan_id}/review")
async def review_plan(
    plan_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    # 只读摘要：MVCC 快照足够，FOR UPDATE 只会让轮询 review 阻塞
    # 同用户的 complete/grade 写路径。
    plan = await _owned_plan(plan_id, user, session)
    review = await _plan_review_summary(plan, user, session)
    return {
        "plan": _serialize_plan(plan, include_nodes=True, detail_node_id=plan.current_node_id),
        **review,
    }


@router.get("/plans/{plan_id}/nodes")
async def list_nodes(
    plan_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan = await _owned_plan(plan_id, user, session)
    return {"nodes": [_serialize_node(node) for node in sorted(plan.nodes, key=lambda item: item.order_index)]}


@router.get("/nodes/{node_id}")
async def get_node(
    node_id: str,
    include_quiz: bool = Query(default=False),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan, node = await _owned_node(node_id, user, session)
    repaired = _repair_plan_content(plan)
    if node.status != "locked" and not node.explanation:
        node.explanation = _build_node_explanation(plan, node)
        node.updated_at = datetime.now()
        repaired = True
    if repaired:
        # GET 会话默认回滚,读修复(含 explanation 补写)需显式提交,否则
        # 每次读取都重复生成(R4-RT-2)。
        await session.commit()
    explore_cache = node.explore_cache or {}
    payload: dict[str, Any] = {
        "node": _serialize_node(node),
        "content_status": {
            "explanation_ready": bool(node.explanation),
            "quiz_ready": bool(node.quiz_questions),
            "explore_ready": bool(explore_cache),
            "detail_ready": bool(explore_cache.get("detail_content")),
            "map_ready": bool(explore_cache.get("map")),
        },
    }
    if include_quiz and node.quiz_questions:
        payload["questions"] = _public_quiz_questions(node.quiz_questions)
    return payload


@router.post("/nodes/{node_id}/start")
async def start_node(
    node_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan, node = await _owned_node(node_id, user, session, for_update=True)
    _start_learning_node(plan, node, user, session)
    await session.flush()
    return {
        "plan": _serialize_plan(plan, include_nodes=True, detail_node_id=node.node_id),
        "node": _serialize_node(node),
    }


@router.post("/nodes/{node_id}/complete")
async def complete_node(
    node_id: str,
    payload: CompleteNodeRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    payload = payload or CompleteNodeRequest()
    plan, node = await _owned_node(node_id, user, session, for_update=True)
    if node.status == "locked":
        raise HTTPException(status_code=409, detail="前置节点尚未完成")

    before = int(node.mastery or 0)
    artifact_score, artifact_diagnosis = _score_learning_artifact(payload.payload, node)
    score = payload.score if payload.score is not None else (artifact_score if artifact_score is not None else 100)
    diagnosis: dict[str, Any] = {}
    if artifact_diagnosis:
        diagnosis = artifact_diagnosis
    if score < MASTERY_THRESHOLD:
        diagnosis = {
            "weak_points": diagnosis.get("weak_points") or (payload.payload.get("weak_points", []) if payload.payload else []),
            "suggestions": diagnosis.get("suggestions") or ["先完成自动复习节点，再重新提交当前节点"],
        }
    adjustment, unlocked_node_ids = _finalize_node_assessment(
        plan,
        node,
        score,
        diagnosis or None,
    )
    if adjustment:
        session.add(adjustment)

    session.add(
        LearningNodeAttempt(
            plan_id=plan.plan_id,
            node_id=node.node_id,
            user_id=user.user_id,
            action_type="complete",
            payload=payload.payload,
            score=score,
            diagnosis=diagnosis or None,
            mastery_before=before,
            mastery_after=node.mastery,
        )
    )
    _recalculate_plan(plan)
    await session.flush()
    return {
        "plan": _serialize_plan(plan, include_nodes=True, detail_node_id=node.node_id),
        "node": _serialize_node(node),
        "unlocked_node_ids": unlocked_node_ids,
        "adjustment": _serialize_adjustment(adjustment),
        # 产物/手动完成的评分反馈：让前端能展示得分与诊断，而不是静默完成。
        "attempt": {
            "score": score,
            "diagnosis": diagnosis or {},
            "mastery_before": before,
            "mastery_after": node.mastery,
        },
    }


@router.post("/nodes/{node_id}/explore")
async def explore_node(
    node_id: str,
    payload: ExploreNodeRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    payload = payload or ExploreNodeRequest()
    plan, node = await _owned_node(node_id, user, session)
    result = await _explore_learning_node(plan, node, payload)
    await session.flush()
    return result


@router.post("/nodes/{node_id}/enrich")
async def enrich_node_content(
    node_id: str,
    payload: EnrichNodeRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    payload = payload or EnrichNodeRequest()
    plan, node = await _owned_node(node_id, user, session, for_update=True)
    if node.status == "locked":
        raise HTTPException(status_code=409, detail="前置节点尚未完成")
    await session.commit()
    await _ensure_node_learning_content(plan, node, force_detail=payload.force)
    await session.flush()
    return {
        "plan": _serialize_plan(plan, include_nodes=True, detail_node_id=node.node_id),
        "node": _serialize_node(node),
    }


@router.post("/nodes/{node_id}/quiz")
async def get_or_create_quiz(
    node_id: str,
    payload: QuizRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    payload = payload or QuizRequest()
    _plan, node = await _owned_node(node_id, user, session, for_update=True)
    if node.status == "locked":
        raise HTTPException(status_code=409, detail="前置节点尚未完成")
    if payload.force or not node.quiz_questions:
        await session.commit()
        questions = (
            await _refresh_node_quiz(_plan, node)
            if payload.force
            else await _generate_node_quiz(_plan, node)
        )
        # 出题耗时数秒且入口锁已被上面的 commit 释放：写回前重取行锁，
        # 避免与并发 complete/grade 交错。
        _plan, node = await _owned_node(node_id, user, session, for_update=True)
        if node.status == "locked":
            raise HTTPException(status_code=409, detail="前置节点尚未完成")
        node.quiz_questions = questions
        node.updated_at = datetime.now()
        await session.flush()
    return {"questions": _public_quiz_questions(node.quiz_questions)}


@router.post("/nodes/{node_id}/quiz/grade")
async def grade_quiz(
    node_id: str,
    payload: GradeQuizRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan, node = await _owned_node(node_id, user, session, for_update=True)
    if node.status == "locked":
        raise HTTPException(status_code=409, detail="前置节点尚未完成")
    if not node.quiz_questions:
        await session.commit()
        node.quiz_questions = await _generate_node_quiz(plan, node)
        await session.flush()
        await session.commit()

    await session.commit()
    score, diagnosis = await _grade_quiz_with_llm(node, list(node.quiz_questions or []), payload.answers)
    # 判分耗时数秒且入口锁已被释放：写回 attempt/掌握度/解锁前重取行锁
    # 并复验前置状态，否则并发 grade/complete 会双 attempt、解锁互相覆盖。
    plan, node = await _owned_node(node_id, user, session, for_update=True)
    if node.status == "locked":
        raise HTTPException(status_code=409, detail="前置节点尚未完成")
    before = int(node.mastery or 0)
    adjustment, unlocked_node_ids = _finalize_node_assessment(plan, node, score, diagnosis)
    if adjustment:
        session.add(adjustment)

    session.add(
        LearningNodeAttempt(
            plan_id=plan.plan_id,
            node_id=node.node_id,
            user_id=user.user_id,
            action_type="quiz",
            payload={"answers": payload.answers},
            score=score,
            diagnosis=diagnosis,
            duration_seconds=payload.duration_seconds,
            mastery_before=before,
            mastery_after=node.mastery,
            source="rules_quiz",
        )
    )
    _recalculate_plan(plan)
    await session.flush()
    return {
        "attempt": {
            "score": score,
            "diagnosis": diagnosis,
            "mastery_before": before,
            "mastery_after": node.mastery,
        },
        "plan": _serialize_plan(plan, include_nodes=True, detail_node_id=node.node_id),
        "node": _serialize_node(node),
        "unlocked_node_ids": unlocked_node_ids,
        "adjustment": _serialize_adjustment(adjustment),
    }
