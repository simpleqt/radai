"""节点内容与测验：探索脑图/资源装配、讲解生成、出题与评分产物。"""

from __future__ import annotations

import asyncio
import json
import os

from backend.api.schemas.custom_learning import ExploreNodeRequest
from backend.config.loggers import get_logger
from backend.db.models import LearningPlan, LearningPlanNode
from datetime import datetime
from fastapi import HTTPException
from typing import Any
from backend.application.custom_learning.common import (
    AGENT_TEAM,
    _await_explore_call,
    _build_multi_agent_quiz,
    _build_node_explanation,
    _dedupe_resources,
    _grade_quiz,
    _plan_kb_ids,
    _style_strategy,
    _text_keywords,
)
from backend.application.custom_learning.plan_progress import _serialize_node


logger = get_logger(__name__)


_SHORT_ANSWER_TYPES_EXCLUDED = {"choice", "judge", "multi"}

# 判分依据只允许留在服务端；下发给客户端的题目必须剥离这些字段，
# 否则学生在作答前就能从响应里读到正确答案，掌握度门槛形同虚设。
_QUIZ_PRIVATE_FIELDS = ("answer", "analysis", "reference", "reference_answer", "scoring_points")


def _public_quiz_questions(questions: Any) -> list[dict[str, Any]]:
    public: list[dict[str, Any]] = []
    for question in questions or []:
        if not isinstance(question, dict):
            continue
        public.append({key: value for key, value in question.items() if key not in _QUIZ_PRIVATE_FIELDS})
    return public


def _llm_grading_enabled() -> bool:
    return os.getenv("CUSTOM_LEARNING_LLM_GRADING", "on").strip().lower() != "off"


def _llm_grading_timeout_seconds() -> float:
    try:
        value = float(os.getenv("CUSTOM_LEARNING_LLM_GRADING_TIMEOUT_SECONDS", "25") or "25")
    except ValueError:
        value = 25.0
    return min(120.0, max(3.0, value))


async def _grading_llm_call(prompt: str, *, timeout: float) -> Any | None:
    """单独封装以便测试打桩；复用 explore 的 LLM JSON 调用（自带超时与异常兜底）。

    ``_llm_json`` 的 timeout 只约束首 token，后续流式读取无总上限；判分是同步等待
    场景，再包一层整体超时，超时由调用方回退为纯规则判分。
    """
    from backend.application.explore.explore_llm import _llm_json

    return await asyncio.wait_for(
        _llm_json(prompt, model_name=None, timeout=timeout, max_tokens=1600, temperature=0.1),
        timeout=timeout + 20.0,
    )


def _build_grading_prompt(node: LearningPlanNode, shorts: list[dict[str, Any]], answers: dict[str, Any]) -> str:
    items = []
    for question in shorts:
        qid = str(question.get("id", ""))
        items.append(
            {
                "id": qid,
                "question": str(question.get("question") or ""),
                "reference": str(question.get("reference") or question.get("analysis") or ""),
                "student_answer": str(answers.get(qid) or "").strip()[:1200],
            }
        )
    payload = json.dumps(items, ensure_ascii=False)
    return (
        f"你是「{node.title}」学习节点的判卷老师。请逐题评价学生的简答题作答。\n"
        "评分标准：85-100 覆盖参考要点且表述正确；70-84 基本正确但有遗漏；"
        "40-69 部分正确或依据不足；0-39 未答对或答非所问。答案与题目无关时给 0-20 分。\n"
        "只输出一个 JSON 数组，不要输出其他文字。每个元素形如：\n"
        '{"id": "题目id", "score": 0到100的整数, "missing_points": ["缺失要点", "最多5条"], '
        '"comment": "不超过80字的中文点评，指出对在哪里、错或缺在哪里"}\n'
        f"题目与作答：{payload}"
    )


def _merge_llm_grading(
    diagnosis: dict[str, Any],
    llm_items: Any,
    short_ids: set[str],
) -> bool:
    """把 LLM 的短答题评价合并进规则诊断；返回是否发生了有效合并。"""
    if not isinstance(llm_items, list):
        return False
    by_id: dict[str, dict[str, Any]] = {}
    for item in llm_items:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or "")
        if qid not in short_ids:
            continue
        try:
            score = int(round(float(item.get("score"))))
        except (TypeError, ValueError):
            continue
        by_id[qid] = {
            "score": min(100, max(0, score)),
            "missing_points": [str(point)[:80] for point in (item.get("missing_points") or []) if str(point).strip()][:5],
            "comment": str(item.get("comment") or "").strip()[:200],
        }
    if not by_id:
        return False

    results = diagnosis.get("question_results") or []
    merged = False
    for result in results:
        graded = by_id.get(str(result.get("id")))
        if graded is None:
            continue
        result["score"] = graded["score"]
        result["verdict"] = "correct" if graded["score"] >= 70 else ("partial" if graded["score"] >= 40 else "wrong")
        result["missing_points"] = graded["missing_points"]
        if graded["comment"]:
            result["comment"] = graded["comment"]
        result["graded_by"] = "llm"
        merged = True
    if not merged:
        return False

    # 依据合并后的逐题结果重建总分与薄弱点，保持与规则路径相同的口径。
    weak_points = [
        str(result.get("question") or result.get("id"))
        for result in results
        if result.get("verdict") != "correct"
    ]
    diagnosis["weak_points"] = weak_points
    diagnosis["suggestions"] = (
        ["回到节点详解，对照验收标准补充说明"] if weak_points else ["可以进入下一个节点"]
    )
    return True


async def _grade_quiz_with_llm(
    node: LearningPlanNode,
    questions: list[dict[str, Any]],
    answers: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """规则判分打底；短答题在允许时交给 LLM 评价，任何失败都回退规则结果。"""
    score, diagnosis = _grade_quiz(questions, answers)
    shorts = [
        question
        for question in questions
        if str(question.get("type") or "short") not in _SHORT_ANSWER_TYPES_EXCLUDED
        and str(answers.get(str(question.get("id", ""))) or "").strip()
    ]
    if not shorts or not _llm_grading_enabled():
        return score, diagnosis

    try:
        llm_items = await _grading_llm_call(
            _build_grading_prompt(node, shorts, answers),
            timeout=_llm_grading_timeout_seconds(),
        )
    except Exception:
        logger.warning("自定义学习 LLM 判分调用异常，回退规则判分: node_id=%s", node.id, exc_info=True)
        llm_items = None

    short_ids = {str(question.get("id", "")) for question in shorts}
    if _merge_llm_grading(diagnosis, llm_items, short_ids):
        results = diagnosis.get("question_results") or []
        if results:
            score = round(sum(int(result.get("score") or 0) for result in results) / len(results))
        diagnosis["grading"] = "llm_assisted"
    else:
        diagnosis["grading"] = "rules_fallback"
    return score, diagnosis


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
        payload = await _await_explore_call(
            explore_svc.gather_resources(
                node_title=node.title,
                kb_id=None,
                top_k=top_k,
            ),
            operation="gather_resources",
        )
        return list(payload.get("resources") or []), []

    per_kb_top_k = max(3, min(8, top_k))

    async def _fetch(kb_id: str) -> dict[str, Any]:
        return await _await_explore_call(
            explore_svc.gather_resources(
                node_title=node.title,
                kb_id=kb_id,
                top_k=per_kb_top_k,
            ),
            operation=f"gather_resources:{kb_id}",
        )

    # 多知识库并行召回：总等待时间取决于最慢的一个，而不是全部之和。
    outcomes = await asyncio.gather(*(_fetch(kb_id) for kb_id in kb_ids), return_exceptions=True)
    for kb_id, outcome in zip(kb_ids, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            errors.append({"kb_id": kb_id, "error": type(outcome).__name__})
        else:
            resources.extend(list(outcome.get("resources") or []))
    return _dedupe_resources(resources, top_k), errors


async def _generate_learning_details(
    plan: LearningPlan,
    node: LearningPlanNode,
) -> tuple[str, list[dict[str, Any]], list[dict[str, str]]]:
    from backend.application.explore import service as explore_svc

    contents: list[str] = []
    sources: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    kb_ids = _plan_kb_ids(plan)

    async def _fetch(kb_id: str) -> dict[str, Any]:
        return await _await_explore_call(
            explore_svc.generate_detail(
                node_title=node.title,
                path=_node_learning_path(plan, node),
                kb_id=kb_id,
                subject=explore_svc.resolve_subject(plan.domain),
            ),
            operation=f"generate_detail:{kb_id}",
        )

    # 多知识库并行生成详解，结果按 kb_ids 顺序拼接，保持原有编号语义。
    outcomes = await asyncio.gather(*(_fetch(kb_id) for kb_id in kb_ids), return_exceptions=True)
    for index, (kb_id, outcome) in enumerate(zip(kb_ids, outcomes, strict=True), start=1):
        if isinstance(outcome, BaseException):
            errors.append({"kb_id": kb_id, "error": type(outcome).__name__})
            continue
        content = str(outcome.get("content") or "").strip()
        if content:
            label = f"知识库 {index}"
            contents.append(f"## {label}补充\n\n{content}" if len(kb_ids) > 1 else content)
        sources.extend(list(outcome.get("sources") or []))
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
            logger.warning(
                "自定义学习节点预加载探索内容失败: plan_id=%s node_id=%s",
                plan.id,
                node.id,
                exc_info=True,
            )

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

        async def _fetch_quiz(kb_id: str) -> dict[str, Any]:
            return await _await_explore_call(
                explore_svc.generate_quiz(
                    node_title=node.title,
                    kb_id=kb_id,
                    types=["choice", "judge", "short"],
                    difficulty="all",
                    count=4,
                    subject=explore_svc.resolve_subject(plan.domain),
                ),
                operation=f"generate_quiz:{kb_id}",
            )

        # 多知识库并行出题，按 kb_ids 顺序收集，行为与串行一致。
        quiz_outcomes = await asyncio.gather(
            *(_fetch_quiz(kb_id) for kb_id in kb_ids), return_exceptions=True
        )
        for kb_id, outcome in zip(kb_ids, quiz_outcomes, strict=True):
            if isinstance(outcome, BaseException):
                quiz_errors.append({"kb_id": kb_id, "error": type(outcome).__name__})
                continue
            external_questions.extend(
                _adapt_explore_question(item)
                for item in (outcome.get("questions") or [])
                if isinstance(item, dict)
            )
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
            payload = await _await_explore_call(
                explore_svc.generate_quiz(
                    node_title=node.title,
                    kb_id=None,
                    types=["choice", "judge", "short"],
                    difficulty="all",
                    count=5,
                    subject=explore_svc.resolve_subject(plan.domain),
                ),
                operation="generate_quiz:fallback",
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
            logger.warning(
                "自定义学习规则练习回退被触发: plan_id=%s node_id=%s",
                plan.id,
                node.id,
                exc_info=True,
            )
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
            map_payload = await _await_explore_call(
                explore_svc.generate_map(
                    topic=AGENT_TEAM.explore_bridge.topic_for(goal=str(plan.goal or ""), node_title=node.title),
                    kb_id=kb_id,
                    max_children=payload.max_children,
                    subject=subject,
                    model_name=payload.model,
                ),
                operation="generate_map",
            )
        except Exception as exc:
            map_payload = {
                "root": {"title": node.title, "summary": node.subtitle or ""},
                "children": [],
                "source": "fallback",
                "error": type(exc).__name__,
            }
        resources, resource_errors = await _await_explore_call(
            _gather_learning_resources(plan, node, top_k=payload.top_k),
            operation="gather_learning_resources",
        )
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
