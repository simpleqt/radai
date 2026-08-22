"""LLM 语义聚类与教学总结（基础力学提问分析）。

map 阶段把问题分批交给模型做语义聚类，reduce 阶段按标签做确定性归并，
最后由一次模型调用生成教学总结。任何一步失败都降级为确定性结果，
不阻塞报告生成。
"""

from __future__ import annotations

import asyncio
import json
import os
import re

from backend.config.loggers import get_logger

logger = get_logger(__name__)

_BATCH_SIZE = 80
_MAX_LABEL_CHARS = 50
_MAX_SUMMARY_CHARS = 2000

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")
_JSON_ARR_RE = re.compile(r"\[[\s\S]*\]")


def _resolve_model_name() -> str | None:
    return (os.environ.get("TEACHER_ANALYTICS_MODEL_NAME") or "").strip() or None


def _extract_json(content: str):
    text = str(content or "").strip()
    if not text:
        return None
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    candidates = []
    arr = _JSON_ARR_RE.search(text)
    if arr:
        candidates.append(arr.group(0))
    obj = _JSON_OBJ_RE.search(text)
    if obj:
        candidates.append(obj.group(0))
    candidates.append(text)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return None


async def _llm_json(prompt: str, *, model_name: str | None, timeout: float) -> dict | list | None:
    from langchain_core.messages import HumanMessage

    from backend.core.models import create_chat_model_async

    try:
        model = await create_chat_model_async(name=model_name, thinking_enabled=False)
        model = model.bind(temperature=0.2, max_tokens=2048)
        content = await asyncio.wait_for(
            model.ainvoke([HumanMessage(content=prompt)]), timeout=timeout
        )
        text = "".join(
            str(part) for part in (getattr(content, "content", "") or "")
        )
        return _extract_json(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TeacherAnalytics] LLM 聚类调用失败: %s", exc)
        return None


def _cluster_prompt(batch: list[dict], weak_points: list[str] | None = None) -> str:
    items = "\n".join(
        f"- [id={item['id']}] {item['question']}" for item in batch
    )
    weak_line = ""
    if weak_points:
        joined = "、".join(weak_points[:10])
        weak_line = (
            f"同期练习错误率较高的知识点（聚类标签与关键词尽量对齐这些命名）：{joined}\n"
        )
    return (
        "你是基础力学课程的助教。请将下列学生提问按知识点/难点做语义聚类。\n"
        "要求：\n"
        "1. 每个提问只能归入一个簇；\n"
        "2. 簇标签用简洁中文（≤12 字），并给出该簇 2-4 个关键词；\n"
        "3. 无法归类的提问放入「其他」簇；\n"
        "4. 只输出 JSON 数组，不要输出任何解释文字。\n"
        "格式：\n"
        '[{"label":"力的合成与分解","ids":["id1","id2"],"keywords":["合力","分力"]}]\n\n'
        f"{weak_line}"
        f"提问列表：\n{items}\n"
    )


def _summary_prompt(clusters: list[dict]) -> str:
    lines = []
    for cluster in clusters:
        keywords = "、".join(cluster.get("keywords") or []) or "无"
        lines.append(
            f"- {cluster['label']}（{cluster['count']} 次）：关键词 {keywords}"
        )
    joined = "\n".join(lines)
    return (
        "以下是基础力学课程学生在某段时间内的高频问题聚类统计。\n"
        "请用 150-300 字写一段教学总结，说明：学生的共性难点、可能的薄弱知识点、"
        "建议教师重点复习/讲解的方向。只输出总结正文，不要标题或列表符号。\n\n"
        f"{joined}\n"
    )


def _merge_clusters(
    batches: list[list[dict]],
    *,
    valid_ids: set[str] | None = None,
) -> list[dict]:
    """按规范化标签归并各批次的簇，并把代表问题合并计数。

    ``valid_ids``（批内真实提问 id 集合）提供时做两重护栏：幻觉 id（模型
    编造的不存在提问）直接剔除；同一 id 跨簇/跨批重复只计一次——「每个提问
    只归入一个簇」此前仅是 prompt 约束，重复计数会抬高簇 count 并污染排序。
    """
    merged: dict[str, dict] = {}
    seen_ids: set[str] = set()
    for batch in batches:
        for cluster in batch:
            label = str(cluster.get("label") or "其他").strip() or "其他"
            if len(label) > _MAX_LABEL_CHARS:
                label = label[:_MAX_LABEL_CHARS]
            key = label.lower()
            if key not in merged:
                merged[key] = {
                    "label": label,
                    "count": 0,
                    "ids": [],
                    "keywords": [],
                }
            entry = merged[key]
            ids = cluster.get("ids") or []
            kept = 0
            for raw_id in ids:
                normalized = str(raw_id)
                if valid_ids is not None and normalized not in valid_ids:
                    continue
                if normalized in seen_ids:
                    continue
                seen_ids.add(normalized)
                entry["ids"].append(normalized)
                kept += 1
            entry["count"] += kept
            for kw in cluster.get("keywords") or []:
                kw = str(kw).strip()
                if kw and kw not in entry["keywords"]:
                    entry["keywords"].append(kw)
    # 全部 id 被幻觉过滤/跨簇去重剔除的簇不再进入排序与 summary，
    # 避免教师端出现 0 题的聚类主题。
    clusters = [c for c in merged.values() if c["count"] > 0]
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


async def run_llm_clustering(
    questions: list[dict],
    *,
    weak_points: list[str] | None = None,
) -> dict:
    """对问题做 LLM 聚类 + 教学总结，返回结构化结果与降级标记。

    ``weak_points`` 是同期练习错误率较高的知识点（可选），用于让聚类主题与
    做题错误对齐；不影响降级行为。
    """
    model_name = _resolve_model_name()
    if not questions:
        return {"clusters": [], "summary": None, "llm_status": "degraded", "model_name": model_name}

    batches = [
        questions[i : i + _BATCH_SIZE]
        for i in range(0, len(questions), _BATCH_SIZE)
    ]
    cluster_batches: list[list[dict]] = []
    degraded = False
    for batch in batches:
        prompt = _cluster_prompt(batch, weak_points=weak_points)
        result = await _llm_json(prompt, model_name=model_name, timeout=60.0)
        if isinstance(result, list):
            cluster_batches.append(result)
        else:
            degraded = True

    clusters = _merge_clusters(
        cluster_batches, valid_ids={str(item["id"]) for item in questions}
    )

    summary = None
    if clusters:
        summary = await _llm_text_summary(clusters, model_name=model_name)
        if summary is None:
            degraded = True

    return {
        "clusters": clusters,
        "summary": summary,
        "llm_status": "ok" if not degraded else "degraded",
        "model_name": model_name,
    }


async def _llm_text_summary(clusters: list[dict], *, model_name: str | None) -> str | None:
    from langchain_core.messages import HumanMessage

    from backend.core.models import create_chat_model_async

    try:
        model = await create_chat_model_async(name=model_name, thinking_enabled=False)
        model = model.bind(temperature=0.3, max_tokens=1024)
        content = await asyncio.wait_for(
            model.ainvoke([HumanMessage(content=_summary_prompt(clusters))]),
            timeout=60.0,
        )
        text = "".join(str(part) for part in (getattr(content, "content", "") or ""))
        text = text.strip()
        return text[:_MAX_SUMMARY_CHARS] or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TeacherAnalytics] 教学总结生成失败: %s", exc)
        return None
