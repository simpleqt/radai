"""自定义学习 API。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
from backend.db.connection import get_async_session
from backend.db.crud.agent_crud import get_runtime_agent_kb_ids
from backend.db.models import (
    KnowledgeBase,
    LearningNodeAttempt,
    LearningPlan,
    LearningPlanAdjustment,
    LearningPlanNode,
    LearningStyleProfile,
    User,
)
from backend.db.models.knowledge_base import KnowledgeBaseStatus
from backend.services.custom_learning_agents import CustomLearningAgentTeam

router = APIRouter(prefix="/custom-learning", tags=["自定义学习"])
AGENT_TEAM = CustomLearningAgentTeam()


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


GENERIC_NODE_TITLES = {
    "学习目标拆解",
    "核心概念梳理",
    "关键方法学习",
    "案例与练习",
    "综合输出",
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


def _node_learning_path(plan: LearningPlan, node: LearningPlanNode) -> list[str]:
    path: list[str] = []
    for value in (plan.title, plan.goal, node.subtitle, node.title):
        text = str(value or "").strip()
        if text and (not path or path[-1] != text):
            path.append(text)
    return path


async def _gather_learning_resources(
    plan: LearningPlan,
    node: LearningPlanNode,
    *,
    top_k: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    from backend.application.explore import service as explore_svc

    resources: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    kb_ids = _plan_kb_ids(plan)
    if not kb_ids:
        payload = await explore_svc.gather_resources(
            node_title=node.title,
            kb_id=None,
            top_k=top_k,
        )
        return list(payload.get("resources") or []), []

    per_kb_top_k = max(3, min(8, top_k))
    for kb_id in kb_ids:
        try:
            payload = await explore_svc.gather_resources(
                node_title=node.title,
                kb_id=kb_id,
                top_k=per_kb_top_k,
            )
            resources.extend(list(payload.get("resources") or []))
        except Exception as exc:
            errors.append({"kb_id": kb_id, "error": type(exc).__name__})
    return _dedupe_resources(resources, top_k), errors


async def _generate_learning_details(
    plan: LearningPlan,
    node: LearningPlanNode,
) -> tuple[str, list[dict[str, Any]], list[dict[str, str]]]:
    from backend.application.explore import service as explore_svc

    contents: list[str] = []
    sources: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for index, kb_id in enumerate(_plan_kb_ids(plan), start=1):
        try:
            detail = await explore_svc.generate_detail(
                node_title=node.title,
                path=_node_learning_path(plan, node),
                kb_id=kb_id,
                subject=explore_svc.resolve_subject(plan.domain),
            )
            content = str(detail.get("content") or "").strip()
            if content:
                label = f"知识库 {index}"
                contents.append(f"## {label}补充\n\n{content}" if len(_plan_kb_ids(plan)) > 1 else content)
            sources.extend(list(detail.get("sources") or []))
        except Exception as exc:
            errors.append({"kb_id": kb_id, "error": type(exc).__name__})
    return "\n\n".join(contents), _dedupe_resources(sources, 12), errors


def _merge_node_explanation(plan: LearningPlan, node: LearningPlanNode, detail_content: str) -> str:
    detail = str(detail_content or "").strip()
    if detail:
        return detail
    return node.explanation or _build_node_explanation(plan, node)


async def _ensure_node_learning_content(
    plan: LearningPlan,
    node: LearningPlanNode,
    *,
    force_detail: bool = False,
) -> bool:
    changed = False
    if not node.explanation:
        node.explanation = _build_node_explanation(plan, node)
        changed = True

    kb_ids = _plan_kb_ids(plan)
    if not kb_ids:
        return changed

    cache = dict(node.explore_cache or {})
    if not cache.get("map"):
        try:
            await _explore_learning_node(plan, node)
            cache = dict(node.explore_cache or {})
            changed = True
        except HTTPException:
            raise
        except Exception:
            pass

    need_detail = force_detail or not cache.get("detail_content")
    if need_detail:
        content, sources, errors = await _generate_learning_details(plan, node)
        cache = dict(node.explore_cache or {})
        if content:
            cache["detail_content"] = content
            cache["detail_sources"] = sources
            cache["detail_grounded"] = bool(sources)
            cache["detail_loaded_at"] = datetime.now().isoformat()
            cache.pop("detail_error", None)
            node.explore_cache = cache
            node.explanation = _merge_node_explanation(plan, node, content)
            node.study_signals = {
                **(node.study_signals or {}),
                "detail_enriched": True,
                "last_agent": "explore_detail",
            }
            changed = True
        elif errors:
            cache["detail_error"] = "知识库详解生成失败，已保留通用学习导引"
            cache["detail_errors"] = errors
            node.explore_cache = cache
            changed = True

    if changed:
        node.updated_at = datetime.now()
    return changed


def _adapt_explore_question(question: dict[str, Any]) -> dict[str, Any]:
    qtype = str(question.get("type") or "short")
    stem = str(question.get("stem") or question.get("question") or "").strip()
    adapted = {
        **question,
        "question": stem,
    }
    if qtype == "judge":
        adapted["options"] = [
            {"key": "正确", "text": "正确"},
            {"key": "错误", "text": "错误"},
        ]
    return adapted


def _normalize_snapshot_questions(raw_questions: Any) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for index, item in enumerate(raw_questions or [], start=1):
        if not isinstance(item, dict):
            continue
        question = _adapt_explore_question(item)
        stem = str(question.get("question") or "").strip()
        if not stem:
            continue
        question["id"] = str(question.get("id") or f"q{index}")
        question.setdefault("source_agent", "explore_snapshot")
        question.setdefault("skill", "explore_reuse")
        if question.get("type") in {"choice", "judge"} and not question.get("answer"):
            continue
        questions.append(question)
        if len(questions) >= 6:
            break
    return questions


def _apply_style_to_questions(
    questions: list[dict[str, Any]],
    style_snapshot: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not questions or not style_snapshot or not style_snapshot.get("enabled"):
        return questions
    strategy = _style_strategy(style_snapshot)
    styled: list[dict[str, Any]] = []
    for item in questions:
        question = dict(item)
        question["style_strategy"] = strategy
        if strategy.get("resource_priority") == "diagram_and_table" and question.get("type") == "short":
            question["question"] = f"{question['question']} 可用结构图、流程图或对比表辅助说明。"
            question.setdefault("reference", "答案应包含关键概念、关系结构和判断依据。")
        elif strategy.get("quiz_focus") == "reflection_first" and question.get("type") == "short":
            question["question"] = f"先写出推理过程，再回答：{question['question']}"
        styled.append(question)
    if strategy.get("quiz_focus") == "case_first":
        styled.sort(key=lambda item: 0 if item.get("skill") in {"application", "error_diagnosis"} else 1)
    return styled


def _apply_explore_snapshot_to_node(
    plan: LearningPlan,
    node: LearningPlanNode,
    snapshot: dict[str, Any] | None,
) -> bool:
    if not isinstance(snapshot, dict):
        return False
    node_title = str(snapshot.get("node_title") or "").strip()
    path = [str(item).strip() for item in (snapshot.get("path") or []) if str(item).strip()]
    detail = snapshot.get("detail") if isinstance(snapshot.get("detail"), dict) else {}
    detail_content = str((detail or {}).get("content") or "").strip()
    detail_sources = (detail or {}).get("sources") if isinstance((detail or {}).get("sources"), list) else []
    resources = snapshot.get("resources") if isinstance(snapshot.get("resources"), list) else []
    questions = _normalize_snapshot_questions(snapshot.get("questions"))

    cache = dict(node.explore_cache or {})
    cache["from_explore_snapshot"] = True
    cache["snapshot_captured_at"] = snapshot.get("captured_at")
    cache["intent"] = snapshot.get("intent")
    cache["topic"] = node_title or node.title
    cache["map"] = snapshot.get("map") or {
        "root": {"title": path[0] if path else (plan.title or node.title), "summary": plan.goal or ""},
        "children": [{"title": item, "summary": "来自探索式学习路径"} for item in path[1:]],
        "source": "explore_snapshot",
    }
    if detail_content:
        cache["detail_content"] = detail_content
        cache["detail_sources"] = detail_sources
        cache["detail_grounded"] = bool(detail_sources)
        node.explanation = detail_content
    if resources:
        node.resources = resources[:8]
        cache["resource_refs"] = [item.get("doc_id") for item in resources if isinstance(item, dict) and item.get("doc_id")]
    if questions:
        node.quiz_questions = _apply_style_to_questions(questions, plan.style_snapshot)
    node.study_signals = {
        **(node.study_signals or {}),
        "explore_snapshot_applied": True,
        "explore_snapshot_topic": node_title,
        "last_agent": "explore_bridge",
    }
    node.explore_cache = cache
    node.updated_at = datetime.now()
    return True


async def _generate_node_quiz(plan: LearningPlan, node: LearningPlanNode) -> list[dict[str, Any]]:
    kb_ids = _plan_kb_ids(plan)
    if kb_ids:
        from backend.application.explore import service as explore_svc

        external_questions: list[dict[str, Any]] = []
        quiz_errors: list[dict[str, str]] = []
        for kb_id in kb_ids:
            try:
                payload = await explore_svc.generate_quiz(
                    node_title=node.title,
                    kb_id=kb_id,
                    types=["choice", "judge", "short"],
                    difficulty="all",
                    count=4,
                    subject=explore_svc.resolve_subject(plan.domain),
                )
                external_questions.extend(
                    _adapt_explore_question(item)
                    for item in (payload.get("questions") or [])
                    if isinstance(item, dict)
                )
            except Exception as exc:
                quiz_errors.append({"kb_id": kb_id, "error": type(exc).__name__})
        if external_questions:
            node.study_signals = {
                **(node.study_signals or {}),
                "quiz_grounded": True,
                "quiz_kb_count": len(kb_ids),
                "last_agent": "quiz_author",
            }
            return _apply_style_to_questions(
                _build_multi_agent_quiz(node, external_questions=external_questions),
                plan.style_snapshot,
            )
        if quiz_errors:
            node.study_signals = {
                **(node.study_signals or {}),
                "quiz_grounded": False,
                "quiz_errors": quiz_errors,
                "last_agent": "quiz_author",
            }
        try:
            payload = await explore_svc.generate_quiz(
                node_title=node.title,
                kb_id=None,
                types=["choice", "judge", "short"],
                difficulty="all",
                count=5,
                subject=explore_svc.resolve_subject(plan.domain),
            )
            questions = [
                _adapt_explore_question(item)
                for item in (payload.get("questions") or [])
                if isinstance(item, dict)
            ]
            if questions:
                return _apply_style_to_questions(
                    _build_multi_agent_quiz(node, external_questions=questions),
                    plan.style_snapshot,
                )
        except Exception:
            # RAG/LLM 不可用时回退到规则练习，保证闭环不中断。
            pass
    return _apply_style_to_questions(_build_multi_agent_quiz(node), plan.style_snapshot)


async def _refresh_node_quiz(plan: LearningPlan, node: LearningPlanNode) -> list[dict[str, Any]]:
    signals = dict(node.study_signals or {})
    revision = int(signals.get("quiz_revision") or 0) + 1
    questions = list(await _generate_node_quiz(plan, node))
    questions.append(
        {
            "id": f"q_review_{revision}",
            "type": "short",
            "question": f"第 {revision} 次练习：请用一个具体案例说明“{node.title}”的关键判断依据。",
            "reference": node.check or f"能结合案例解释“{node.title}”的关键概念、适用条件和常见误区。",
        }
    )
    node.study_signals = {
        **signals,
        "quiz_revision": revision,
        "quiz_refreshed_at": datetime.now().isoformat(),
        "last_agent": "quiz_author",
    }
    return questions


def _grade_quiz(questions: list[dict[str, Any]], answers: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return AGENT_TEAM.grade_quiz(questions, answers)


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


def _serialize_plan(plan: LearningPlan, include_nodes: bool = False) -> dict[str, Any]:
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
        payload["nodes"] = [_serialize_node(node) for node in nodes]
    return payload


def _serialize_node(node: LearningPlanNode) -> dict[str, Any]:
    explore_cache = node.explore_cache or {}
    return {
        "node_id": node.node_id,
        "plan_id": node.plan_id,
        "parent_node_id": node.parent_node_id,
        "order_index": node.order_index,
        "title": node.title,
        "subtitle": node.subtitle,
        "objective": node.objective,
        "explanation": node.explanation,
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
        "resources": node.resources or [],
        "tasks": node.tasks or [],
        "checks": node.checks or [],
        "study_signals": node.study_signals or {},
        "explore_cache": explore_cache,
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


def _build_low_score_adjustment(
    plan: LearningPlan,
    node: LearningPlanNode,
    *,
    score: int,
    diagnosis: dict[str, Any] | None = None,
) -> LearningPlanAdjustment | None:
    return AGENT_TEAM.build_low_score_adjustment(plan, node, score=score, diagnosis=diagnosis)


def _ordered_nodes(plan: LearningPlan) -> list[LearningPlanNode]:
    return sorted(list(plan.nodes or []), key=lambda item: item.order_index)


MASTERY_THRESHOLD = 70


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


def _score_learning_artifact(
    payload: dict[str, Any] | None,
    node: LearningPlanNode,
) -> tuple[int | None, dict[str, Any]]:
    if not payload:
        return None, {}
    artifact_text = str(payload.get("artifact_text") or payload.get("artifact") or "").strip()
    if not artifact_text:
        return None, {}
    char_count = len(artifact_text)
    weak_points: list[str] = []
    suggestions: list[str] = []

    expected_terms = _text_keywords(
        node.title,
        node.subtitle,
        node.objective,
        node.task,
        node.check,
        node.explanation,
        limit=14,
    )
    answer_text = artifact_text.lower()
    matched_terms = [
        term
        for term in expected_terms
        if term.lower() in answer_text
    ]
    coverage = len(matched_terms) / max(len(expected_terms), 1) if expected_terms else 0.5
    structure_bonus = 8 if any(marker in artifact_text for marker in ("1.", "1、", "\n", "：", ":", "；", ";")) else 0
    length_bonus = min(18, max(0, (char_count - 40) // 12))
    score = int(round(45 + coverage * 35 + length_bonus + structure_bonus))

    if char_count < 60:
        weak_points.append("学习产物说明过短，尚未覆盖关键概念、判断依据和自检结论")
        suggestions.append("补充至少一个具体例子、一个关键判断依据和一个容易混淆点")
        score = min(score, 60)
    if expected_terms and coverage < 0.35:
        missing = [term for term in expected_terms if term not in matched_terms][:5]
        weak_points.append(f"产物与节点目标关联不足，缺少：{'、'.join(missing)}")
        suggestions.append("围绕节点任务和验收标准重写结论，明确概念、依据和应用场景")
        score = min(score, 68)
    elif char_count < 160:
        suggestions.append("产物已覆盖基本说明，建议继续补充图表、计算过程或误区复盘")
    else:
        suggestions.append("产物较完整，可以进入下一节点并在复盘时回看")

    return max(0, min(96, score)), {
        "weak_points": weak_points,
        "suggestions": suggestions,
        "artifact_length": char_count,
        "keyword_coverage": round(coverage, 2),
        "matched_terms": matched_terms[:8],
    }


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
        "plan": _serialize_plan(plan, include_nodes=True),
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
    if node.status == "open":
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


async def _explore_learning_node(
    plan: LearningPlan,
    node: LearningPlanNode,
    payload: ExploreNodeRequest | None = None,
) -> dict[str, Any]:
    payload = payload or ExploreNodeRequest()
    if node.status == "locked":
        raise HTTPException(status_code=409, detail="前置节点尚未完成")

    kb_ids = _plan_kb_ids(plan)
    kb_id = kb_ids[0] if kb_ids else None
    subject = None
    map_payload: dict[str, Any] = {}
    resources_payload: dict[str, Any] = {"resources": [], "grounded": False}
    resource_errors: list[dict[str, str]] = []

    try:
        from backend.application.explore import service as explore_svc

        subject = explore_svc.resolve_subject(plan.domain)
        try:
            map_payload = await explore_svc.generate_map(
                topic=AGENT_TEAM.explore_bridge.topic_for(goal=str(plan.goal or ""), node_title=node.title),
                kb_id=kb_id,
                max_children=payload.max_children,
                subject=subject,
                model_name=payload.model,
            )
        except Exception as exc:
            map_payload = {
                "root": {"title": node.title, "summary": node.subtitle or ""},
                "children": [],
                "source": "fallback",
                "error": type(exc).__name__,
            }
        resources, resource_errors = await _gather_learning_resources(plan, node, top_k=payload.top_k)
        resources_payload = {
            "resources": resources,
            "grounded": bool(resources),
            "errors": resource_errors,
        }
    except Exception as exc:
        map_payload = {
            "root": {"title": node.title, "summary": node.subtitle or ""},
            "children": [],
            "source": "fallback",
            "error": type(exc).__name__,
        }

    resources = resources_payload.get("resources") or []
    node.resources = resources
    existing_cache = dict(node.explore_cache or {})
    node.explore_cache = {
        **existing_cache,
        "topic": node.title,
        "subject": subject,
        "map": map_payload,
        "resource_refs": [item.get("doc_id") for item in resources if isinstance(item, dict) and item.get("doc_id")],
        "grounded": bool(resources_payload.get("grounded")),
        "kb_ids_used": kb_ids,
        "resource_errors": resource_errors,
        "last_explored_at": datetime.now().isoformat(),
        "agent": "explore_bridge",
    }
    node.study_signals = {**(node.study_signals or {}), "explored": True, "last_agent": "explore_bridge"}
    node.updated_at = datetime.now()
    return {"map": map_payload, "resources": resources, "node": _serialize_node(node)}


async def _owned_plan(plan_id: str, user: User, session: AsyncSession) -> LearningPlan:
    result = await session.execute(
        select(LearningPlan)
        .where(LearningPlan.plan_id == plan_id, LearningPlan.user_id == user.user_id)
        .options(selectinload(LearningPlan.nodes))
    )
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="学习计划不存在")
    return plan


async def _owned_node(node_id: str, user: User, session: AsyncSession) -> tuple[LearningPlan, LearningPlanNode]:
    result = await session.execute(
        select(LearningPlanNode, LearningPlan)
        .join(LearningPlan, LearningPlan.plan_id == LearningPlanNode.plan_id)
        .where(LearningPlanNode.node_id == node_id, LearningPlan.user_id == user.user_id)
        .options(selectinload(LearningPlan.nodes))
    )
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

    MVP 使用确定性规则生成，后续可在保持响应 schema 不变的前提下替换为 LLM/SSE。
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
    blueprint = AGENT_TEAM.generate_plan_blueprint(
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
        await session.flush()
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
        await session.flush()
    return await _serialize_plan_dashboard(plan, user, session, selected_node_id=node_id)


@router.patch("/plans/{plan_id}")
async def update_plan(
    plan_id: str,
    payload: UpdatePlanRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan = await _owned_plan(plan_id, user, session)
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
    plan = await _owned_plan(plan_id, user, session)
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
    plan = await _owned_plan(plan_id, user, session)
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
                node.quiz_questions = await _generate_node_quiz(plan, node)
            before = int(node.mastery or 0)
            score, diagnosis = _grade_quiz(list(node.quiz_questions or []), payload.answers)
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
            await _ensure_node_learning_content(plan, node)
            action = "enrich_content"
            result = {"node": _serialize_node(node)}
        elif payload.prefer_materials and plan.kb_ids and not (node.explore_cache or {}).get("map"):
            result = await _explore_learning_node(plan, node)
            action = "explore_node"
        elif not node.quiz_questions and node.status != "done":
            node.quiz_questions = await _generate_node_quiz(plan, node)
            node.study_signals = {**(node.study_signals or {}), "quiz_ready": True, "last_agent": "quiz_author"}
            node.updated_at = datetime.now()
            action = "generate_quiz"
            result = {"questions": node.quiz_questions}
        elif node.status == "done":
            next_node = _pick_focus_node(plan)
            if next_node:
                plan.current_node_id = next_node.node_id
            action = "advance_node"
        else:
            action = "await_quiz"
            result = {"questions": node.quiz_questions or []}

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
    plan = await _owned_plan(plan_id, user, session)
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
    plan = await _owned_plan(plan_id, user, session)
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
    plan = await _owned_plan(plan_id, user, session)
    review = await _plan_review_summary(plan, user, session)
    return {
        "plan": _serialize_plan(plan, include_nodes=True),
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
        await session.flush()
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
        payload["questions"] = node.quiz_questions
    return payload


@router.post("/nodes/{node_id}/start")
async def start_node(
    node_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan, node = await _owned_node(node_id, user, session)
    _start_learning_node(plan, node, user, session)
    await session.flush()
    return {"plan": _serialize_plan(plan, include_nodes=True), "node": _serialize_node(node)}


@router.post("/nodes/{node_id}/complete")
async def complete_node(
    node_id: str,
    payload: CompleteNodeRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    payload = payload or CompleteNodeRequest()
    plan, node = await _owned_node(node_id, user, session)
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
        "plan": _serialize_plan(plan, include_nodes=True),
        "node": _serialize_node(node),
        "unlocked_node_ids": unlocked_node_ids,
        "adjustment": _serialize_adjustment(adjustment),
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
    plan, node = await _owned_node(node_id, user, session)
    if node.status == "locked":
        raise HTTPException(status_code=409, detail="前置节点尚未完成")
    await _ensure_node_learning_content(plan, node, force_detail=payload.force)
    await session.flush()
    return {"plan": _serialize_plan(plan, include_nodes=True), "node": _serialize_node(node)}


@router.post("/nodes/{node_id}/quiz")
async def get_or_create_quiz(
    node_id: str,
    payload: QuizRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    payload = payload or QuizRequest()
    _plan, node = await _owned_node(node_id, user, session)
    if node.status == "locked":
        raise HTTPException(status_code=409, detail="前置节点尚未完成")
    if payload.force or not node.quiz_questions:
        node.quiz_questions = (
            await _refresh_node_quiz(_plan, node)
            if payload.force
            else await _generate_node_quiz(_plan, node)
        )
        node.updated_at = datetime.now()
        await session.flush()
    return {"questions": node.quiz_questions}


@router.post("/nodes/{node_id}/quiz/grade")
async def grade_quiz(
    node_id: str,
    payload: GradeQuizRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    plan, node = await _owned_node(node_id, user, session)
    if node.status == "locked":
        raise HTTPException(status_code=409, detail="前置节点尚未完成")
    if not node.quiz_questions:
        node.quiz_questions = await _generate_node_quiz(plan, node)

    before = int(node.mastery or 0)
    score, diagnosis = _grade_quiz(list(node.quiz_questions or []), payload.answers)
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
        "plan": _serialize_plan(plan, include_nodes=True),
        "node": _serialize_node(node),
        "unlocked_node_ids": unlocked_node_ids,
        "adjustment": _serialize_adjustment(adjustment),
    }
