"""自定义学习共享基础：KB 归一化、资源去重、Agent 团队封装、阈值常量与探索调用超时。"""

from __future__ import annotations

import asyncio
import os
import re

from backend.config.loggers import get_logger
from backend.db.crud.agent_crud import get_runtime_agent_kb_ids
from backend.db.models import (
    KnowledgeBase,
    LearningPlan,
    LearningPlanAdjustment,
    LearningPlanNode,
)
from backend.db.models.knowledge_base import KnowledgeBaseStatus
from backend.services.custom_learning_agents import CustomLearningAgentTeam
from backend.services.custom_learning_planning import (
    LearningNodeBlueprint,
    LearningPlanBlueprint,
)
from dataclasses import replace as dataclass_replace
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any


logger = get_logger(__name__)
AGENT_TEAM = CustomLearningAgentTeam()
_EXPLORE_CALL_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("CUSTOM_LEARNING_EXPLORE_TIMEOUT_SECONDS", "60") or "60"),
)
GENERIC_NODE_TITLES = {
    "学习目标拆解",
    "核心概念梳理",
    "关键方法学习",
    "案例与练习",
    "综合输出",
}
MASTERY_THRESHOLD = 70


async def _await_explore_call(awaitable: Any, *, operation: str) -> Any:
    try:
        return await asyncio.wait_for(
            awaitable,
            timeout=_EXPLORE_CALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "自定义学习探索调用超时: operation=%s timeout=%.1fs",
            operation,
            _EXPLORE_CALL_TIMEOUT_SECONDS,
        )
        raise


_BASELINE_LABELS = {
    "unknown": "不确定",
    "beginner": "入门",
    "intermediate": "有基础",
    "advanced": "进阶",
    "expert": "专家",
}
_DEPTH_LABELS = {
    "foundation": "打基础",
    "analysis": "会分析",
    "project": "做项目",
}


def _llm_planning_enabled() -> bool:
    return os.getenv("CUSTOM_LEARNING_LLM_PLANNING", "on").strip().lower() != "off"


def _llm_planning_timeout_seconds() -> float:
    try:
        value = float(os.getenv("CUSTOM_LEARNING_LLM_PLANNING_TIMEOUT_SECONDS", "30") or "30")
    except ValueError:
        value = 30.0
    return min(120.0, max(5.0, value))


async def _planning_llm_call(prompt: str, *, timeout: float) -> Any | None:
    """单独封装以便测试打桩；整体超时封顶，超时由调用方回退规则蓝图。"""
    from backend.application.explore.explore_llm import _llm_json

    return await asyncio.wait_for(
        _llm_json(prompt, model_name=None, timeout=timeout, max_tokens=2400, temperature=0.4),
        timeout=timeout + 20.0,
    )


def _build_planning_prompt(
    *,
    goal: str,
    baseline: str,
    depth: str,
    duration_days: int,
    daily_minutes: int,
    max_nodes: int,
) -> str:
    baseline_label = _BASELINE_LABELS.get(baseline, "不确定")
    depth_label = _DEPTH_LABELS.get(depth, "会分析")
    return (
        "你是一名课程规划师，请把学习目标拆解成循序渐进的学习节点。\n"
        f"学习者背景：基础水平「{baseline_label}」，期望深度「{depth_label}」，"
        f"总时长 {duration_days} 天，每天约 {daily_minutes} 分钟。\n"
        f"请规划 {min(4, max_nodes)} 到 {max_nodes} 个节点，从前置概念到综合应用递进，"
        "每个节点都要可在一天左右完成。\n"
        "只输出一个 JSON 对象，不要输出其他文字，结构如下：\n"
        '{"title": "计划标题（不超过 30 字）", "summary": "一句话概述这个计划",'
        ' "nodes": [{"title": "节点名（不超过 20 字）", "subtitle": "一句副标题",'
        ' "objective": "完成本节点后能做什么", "resource": "建议查阅的资料类型或主题",'
        ' "task": "1-3 个具体任务，用；分隔", "check": "可验证的通过标准"}]}\n'
        f"学习目标：{goal.strip()}"
    )


def _parse_llm_blueprint(
    payload: Any,
    *,
    depth: str,
    daily_minutes: int,
    max_nodes: int,
) -> tuple[list[LearningNodeBlueprint], str, str] | None:
    """校验 LLM 蓝图输出；结构非法或有效节点不足 3 个时返回 None（回退规则）。"""
    if isinstance(payload, dict):
        raw_nodes = payload.get("nodes")
        title = str(payload.get("title") or "").strip()[:80]
        summary = str(payload.get("summary") or "").strip()[:300]
    elif isinstance(payload, list):
        raw_nodes, title, summary = payload, "", ""
    else:
        return None
    if not isinstance(raw_nodes, list):
        return None

    nodes: list[LearningNodeBlueprint] = []
    seen_titles: set[str] = set()
    for item in raw_nodes:
        if not isinstance(item, dict):
            continue
        node_title = str(item.get("title") or "").strip()[:60]
        if not node_title or node_title in seen_titles:
            continue
        seen_titles.add(node_title)
        nodes.append(
            LearningNodeBlueprint(
                title=node_title,
                subtitle=str(item.get("subtitle") or "").strip()[:200],
                objective=str(item.get("objective") or "").strip()[:300],
                resource=str(item.get("resource") or "").strip()[:200],
                task=str(item.get("task") or "").strip()[:300],
                check=str(item.get("check") or "").strip()[:300],
                difficulty=depth or "analysis",
                estimated_minutes=max(30, int(daily_minutes or 90)),
                agent_notes={"agent": "llm_planner", "planned_by": "llm_blueprint"},
            )
        )
        if len(nodes) >= max_nodes:
            break
    if len(nodes) < 3:
        return None
    return nodes, title, summary


async def _generate_plan_blueprint(
    *,
    goal: str,
    baseline: str,
    depth: str,
    duration_days: int,
    daily_minutes: int,
    kb_ids: list[str],
    style_snapshot: dict[str, Any] | None,
) -> LearningPlanBlueprint:
    """生成计划蓝图：命中人工领域模板走规则；通用目标优先 LLM，失败回退规则骨架。"""
    if _llm_planning_enabled() and AGENT_TEAM.planner.uses_generic_fallback(goal):
        max_nodes = max(3, min(10, int(duration_days or 7)))
        prompt = _build_planning_prompt(
            goal=goal,
            baseline=baseline,
            depth=depth,
            duration_days=duration_days,
            daily_minutes=daily_minutes,
            max_nodes=max_nodes,
        )
        try:
            payload = await _planning_llm_call(prompt, timeout=_llm_planning_timeout_seconds())
        except Exception:
            logger.warning("自定义学习 LLM 蓝图生成失败，回退规则蓝图", exc_info=True)
            payload = None
        parsed = _parse_llm_blueprint(payload, depth=depth, daily_minutes=daily_minutes, max_nodes=max_nodes)
        if parsed:
            nodes, title, summary = parsed
            blueprint = AGENT_TEAM.generate_plan_blueprint(
                goal=goal,
                baseline=baseline,
                depth=depth,
                duration_days=duration_days,
                daily_minutes=daily_minutes,
                kb_ids=kb_ids,
                style_snapshot=style_snapshot,
                node_overrides=nodes,
            )
            if title or summary:
                blueprint = dataclass_replace(
                    blueprint,
                    title=title or blueprint.title,
                    summary=summary or blueprint.summary,
                )
            return blueprint
    return AGENT_TEAM.generate_plan_blueprint(
        goal=goal,
        baseline=baseline,
        depth=depth,
        duration_days=duration_days,
        daily_minutes=daily_minutes,
        kb_ids=kb_ids,
        style_snapshot=style_snapshot,
    )


def _normalize_kb_ids(kb_ids: list[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in kb_ids or []:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _plan_kb_ids(plan: LearningPlan) -> list[str]:
    return _normalize_kb_ids(list(plan.kb_ids or []))


def _dedupe_resources(resources: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in resources:
        if not isinstance(item, dict):
            continue
        key = str(item.get("doc_id") or item.get("download_url") or item.get("title") or item.get("doc_name") or "")
        if not key:
            key = str(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _text_keywords(*values: Any, limit: int = 16) -> list[str]:
    stopwords = {
        "一个",
        "一种",
        "需要",
        "说明",
        "解释",
        "判断",
        "完成",
        "关键",
        "概念",
        "条件",
        "步骤",
        "案例",
        "依据",
        "节点",
        "学习",
        "资料",
        "标准",
        "能够",
        "可以",
        "以及",
        "进行",
        "相关",
    }
    text = " ".join(str(value or "") for value in values)
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_+\-/.]{1,}|[\u4e00-\u9fff]{2,}", text)
    terms: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = item.strip()
        if not value or value in stopwords:
            continue
        chunks = [value]
        if len(value) > 14 and re.fullmatch(r"[\u4e00-\u9fff]+", value):
            chunks = [value[i : i + 4] for i in range(0, len(value), 4)]
        for chunk in chunks:
            if len(chunk) < 2 or chunk in stopwords or chunk in seen:
                continue
            seen.add(chunk)
            terms.append(chunk)
            if len(terms) >= limit:
                return terms
    return terms


def _resolve_agent_code(
    *,
    domain: str | None = None,
    agent_code: str | None = None,
) -> str:
    if agent_code and str(agent_code).strip():
        return str(agent_code).strip()
    return str(domain or "ne").strip() or "ne"


async def _list_selectable_knowledge_bases(
    session: AsyncSession,
    agent_code: str,
) -> list[dict[str, str]]:
    """返回指定对话智能体绑定的知识库，与 agent-runtime info 一致。"""
    kb_ids = await get_runtime_agent_kb_ids(session, agent_code)
    if not kb_ids:
        return []

    rows = await session.execute(
        select(KnowledgeBase.kb_id, KnowledgeBase.kb_name).where(
            KnowledgeBase.kb_id.in_(kb_ids),
            KnowledgeBase.status == KnowledgeBaseStatus.ACTIVE,
        )
    )
    labels = {str(row.kb_id): str(row.kb_name) for row in rows}
    return [
        {"kb_id": kb_id, "kb_name": labels.get(kb_id, kb_id)}
        for kb_id in kb_ids
        if kb_id in labels
    ]


async def _validate_selectable_kb_ids(
    session: AsyncSession,
    agent_code: str,
    kb_ids: list[str] | None,
) -> list[str]:
    normalized = _normalize_kb_ids(kb_ids)
    if not normalized:
        return []

    selectable = {
        item["kb_id"]
        for item in await _list_selectable_knowledge_bases(session, agent_code)
    }
    denied = [kb_id for kb_id in normalized if kb_id not in selectable]
    if denied:
        raise HTTPException(
            status_code=403,
            detail=f"以下知识库不可用于学习计划：{', '.join(denied)}",
        )
    return normalized


def _style_task_hints(style_snapshot: dict[str, Any] | None) -> list[str]:
    return AGENT_TEAM.style_coach.task_hints(style_snapshot)


def _style_explanation_hint(style_snapshot: dict[str, Any] | None) -> str:
    return AGENT_TEAM.style_coach.explanation_hint(style_snapshot)


def _style_strategy(style_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    dimensions = AGENT_TEAM.style_coach.dimensions(style_snapshot)
    if not dimensions:
        return {
            "quiz_focus": "balanced",
            "resource_priority": "mixed",
            "pacing": "standard",
            "artifact_prompt": "完成一页学习小结或节点练习。",
        }
    input_pref = dimensions.get("input", "")
    processing_pref = dimensions.get("processing", "")
    understanding_pref = dimensions.get("understanding", "")
    return {
        "quiz_focus": "case_first" if processing_pref == "活跃型" else ("reflection_first" if processing_pref == "沉思型" else "balanced"),
        "resource_priority": "diagram_and_table" if input_pref == "视觉型" else ("text_explanation" if input_pref == "言语型" else "mixed"),
        "pacing": "step_by_step" if understanding_pref == "序列型" else ("overview_then_detail" if understanding_pref == "综合型" else "standard"),
        "artifact_prompt": "优先画结构图、流程图或对比表。" if input_pref == "视觉型" else "优先写概念解释、类比和关键判断依据。",
    }


def _keyword_nodes(goal: str, duration_days: int) -> list[dict[str, Any]]:
    nodes = AGENT_TEAM.planner.plan_nodes(
        goal,
        duration_days,
        depth="analysis",
        daily_minutes=90,
    )
    return [
        {
            "title": node.title,
            "subtitle": node.subtitle,
            "objective": node.objective,
            "resource": node.resource,
            "task": node.task,
            "check": node.check,
        }
        for node in nodes
    ]


def _build_multi_agent_quiz(
    node: LearningPlanNode,
    *,
    external_questions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return AGENT_TEAM.build_quiz_questions(node, external_questions=external_questions)


def _build_node_explanation(plan: LearningPlan, node: LearningPlanNode) -> str:
    return AGENT_TEAM.build_node_explanation(plan, node)


def _grade_quiz(questions: list[dict[str, Any]], answers: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return AGENT_TEAM.grade_quiz(questions, answers)


def _build_low_score_adjustment(
    plan: LearningPlan,
    node: LearningPlanNode,
    *,
    score: int,
    diagnosis: dict[str, Any] | None = None,
) -> LearningPlanAdjustment | None:
    return AGENT_TEAM.build_low_score_adjustment(plan, node, score=score, diagnosis=diagnosis)


def _style_summary(style_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    dimensions = AGENT_TEAM.style_coach.dimensions(style_snapshot)
    labels = [label for label in dimensions.values() if label]
    enabled = bool(style_snapshot and style_snapshot.get("enabled"))
    return {
        "enabled": enabled,
        "dimensions": dimensions,
        "labels": labels,
        "task_hints": _style_task_hints(style_snapshot),
        "explanation_hint": _style_explanation_hint(style_snapshot),
        "strategy": _style_strategy(style_snapshot),
    }


def _prewarm_node_content(plan: LearningPlan, node: LearningPlanNode) -> None:
    if node.status == "locked":
        return
    if not node.explanation:
        node.explanation = _build_node_explanation(plan, node)
    node.study_signals = {
        **(node.study_signals or {}),
        "prewarmed": True,
        "content_status": "ready",
        "last_agent": "content_explainer",
    }
    node.updated_at = datetime.now()
