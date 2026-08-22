"""探索式学习服务层。

提供发现态主题、思维导图（生成/展开）、知识点详解（流式）、关联资源、
习题练习（生成/评分）等能力。所有专业内容均先经 `retrieve_context` 检索
知识库上下文后再交由 LLM 生成，保证接地（grounded）与可溯源。

LLM 调用范式与 `backend/custom/services/follow_up_suggestions.py` 保持一致：
`create_chat_model(...).bind(...)` + `ainvoke` / `astream`，并对 JSON 输出做
正则容错解析，失败时回退到模板兜底，避免前端崩溃。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator


from backend.application.llm_content import normalize_llm_content as _normalize_content

from backend.application.explore.explore_config import (  # noqa: F401  (兼容 re-export)
    AGENT_CODE_BY_DOMAIN,
    SUBJECT_BY_DOMAIN,
    DEFAULT_DOMAIN,
    DEFAULT_LEARNING_FAST_MODEL_NAME,
    DEFAULT_SUBJECT,
    EXPLORE_DETAIL_WORKFLOW,
    ExploreDetailRuntimeConfig,
    PRESET_TOPICS,
    QUIZ_CONTEXT_CHUNK_TOP_K,
    QUIZ_CONTEXT_MAX_CHARS,
    TIMEOUT_DETAIL,
    TIMEOUT_EXPAND,
    TIMEOUT_GRADE,
    TIMEOUT_MAP,
    TIMEOUT_QUIZ,
    TIMEOUT_QUIZ_RETRIEVE,
    _MAX_MINDMAP_CACHE_SIZE,
    _MINDMAP_CACHE_TTL,
    _PRESET_MAPS,
    _load_preset_maps,
    resolve_agent_code,
    resolve_detail_runtime_config,
    resolve_domain,
    resolve_learning_fast_model_name,
    resolve_subject,
)
from backend.application.explore.explore_llm import (  # noqa: F401  (兼容 re-export)
    _IMG_BLOCK_RE,
    _IMG_PATH_RE,
    _strip_image_blocks,
    _FENCE_RE,
    _INVALID_JSON_BACKSLASH_RE,
    _JSON_ARRAY_RE,
    _JSON_OBJ_RE,
    _extract_sources,
    _llm_json,
    _llm_text,
    _parse_json,
    _retrieve_context,
    _retrieve_quiz_context,
)
from backend.application.explore.explore_resources import (  # noqa: F401  (兼容 re-export)
    _build_content_disposition,
    _derive_resource_title,
    _guess_mime_type,
    _looks_like_machine_id,
    _resolve_kb_document_storage,
    _strip_resource_noise,
    _title_from_content,
    build_resource_download_url,
    download_kb_resource,
)

logger = logging.getLogger(__name__)

# 判分采分点条数上限：grade 是匿名可调用的 LLM 端点（见 router），条数与
# 单条长度都封顶，防止无界列表把 prompt 撑到任意大小。
_MAX_SCORING_POINTS = 20

# 优化: 脑图缓存机制
_mindmap_cache: dict[str, tuple[dict, float]] = {}  # cache_key -> (result, timestamp)
# 同 key 构建单飞:冷启动/TTL 到期时 N 个并发同题请求会各自发起一次
# 25 秒级 LLM 调用(惊群),让后到者共享第一个构建任务。仅单事件循环访问。
_mindmap_inflight: dict[str, "asyncio.Task[dict]"] = {}


def _mindmap_cache_lookup(cache_key: str) -> dict | None:
    cached = _mindmap_cache.get(cache_key)
    if cached is None:
        return None
    result, timestamp = cached
    if time.time() - timestamp >= _MINDMAP_CACHE_TTL:
        return None
    # 深拷贝隔离:缓存的是共享条目,调用方(如 custom_learning 装配)就地
    # 修改返回值不能污染后续命中的结果。
    return copy.deepcopy(result)


def _mindmap_cache_store(cache_key: str, result: dict) -> None:
    _mindmap_cache[cache_key] = (copy.deepcopy(result), time.time())
    if len(_mindmap_cache) > _MAX_MINDMAP_CACHE_SIZE:
        oldest_key = min(_mindmap_cache, key=lambda k: _mindmap_cache[k][1])
        del _mindmap_cache[oldest_key]
        logger.info("[Explore] 探索缓存已满,清理最旧条目: %s", oldest_key)


async def _mindmap_cached_build(
    cache_key: str,
    build: Callable[[], Awaitable[dict]],
) -> tuple[dict, bool]:
    """带 TTL 缓存与单飞的构建入口,返回 (结果, 是否命中缓存)。"""
    hit = _mindmap_cache_lookup(cache_key)
    if hit is not None:
        return hit, True
    task = _mindmap_inflight.get(cache_key)
    if task is None or task.done():

        async def _run() -> dict:
            result = await build()
            _mindmap_cache_store(cache_key, result)
            return result

        task = asyncio.create_task(_run())
        _mindmap_inflight[cache_key] = task

        def _clear(done: "asyncio.Task[dict]", key: str = cache_key) -> None:
            if _mindmap_inflight.get(key) is done:
                _mindmap_inflight.pop(key, None)

        task.add_done_callback(_clear)
    # shield:等待者被取消不拖累共享构建;构建异常照常传给所有等待者。
    return await asyncio.shield(task), False


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _gen_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _clean_text(value: Any, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        text = text[:limit]
    return text


_PLACEHOLDER_TITLES = {
    "子知识点",
    "子节点",
    "知识点",
    "节点",
    "主题名",
    "标题",
    "title",
    "node title",
}
_PLACEHOLDER_SUMMARIES = {
    "一句话说明",
    "一句话概述",
    "一句话引导",
    "简要说明",
    "说明",
    "summary",
}


def _is_placeholder_text(text: str, placeholders: set[str]) -> bool:
    normalized = re.sub(r"[\s:：,，.。;；、\-_/\\|]+", "", str(text or "").strip()).lower()
    return not normalized or normalized in {p.lower() for p in placeholders}


# ---------------------------------------------------------------------------
# 能力函数
# ---------------------------------------------------------------------------

async def generate_topics(
    *, kb_id: str | None, count: int = 24, seed: str = "", subject: str | None = None, model_name: str | None = None
) -> dict[str, Any]:
    """发现态主题为**预制清单**（即时返回、不调用 LLM），每次随机抽取以支持「换一批」。"""
    import random

    subject = subject or DEFAULT_SUBJECT
    count = max(8, min(28, int(count or 24)))
    presets = list(PRESET_TOPICS.get(subject) or PRESET_TOPICS.get(DEFAULT_SUBJECT) or [])
    random.shuffle(presets)
    selected = presets[:count]
    topics = [{"id": _gen_id("t"), "title": t["title"], "hint": t.get("hint", "")} for t in selected]
    return {"topics": topics, "kb_id": kb_id, "source": "preset"}


async def generate_map(
    *, topic: str, kb_id: str | None, max_children: int = 6, subject: str | None = None, model_name: str | None = None
) -> dict[str, Any]:
    """生成思维导图（根 + 一级子节点）。

    优化: 添加服务端缓存机制,相同主题直接返回缓存结果
    """
    subject = subject or DEFAULT_SUBJECT
    topic = _clean_text(topic, 40) or subject
    max_children = max(3, min(8, int(max_children or 6)))

    # 首页预制主题直接使用离线脑图，不进入模型调用链路。
    preset_titles = {item["title"] for item in PRESET_TOPICS.get(subject, [])}
    preset = _PRESET_MAPS.get(f"{subject}:{topic}")
    if preset is None and topic in preset_titles:
        preset = _PRESET_MAPS.get(topic)
    if isinstance(preset, dict):
        raw_root = preset.get("root") if isinstance(preset.get("root"), dict) else {}
        children = _normalize_nodes(preset.get("children"), max_children)
        if children:
            logger.info("[Explore] 命中预制脑图: subject=%s, topic=%s", subject, topic)
            return {
                "root": {
                    "id": "n_root",
                    "title": _clean_text(raw_root.get("title"), 40) or topic,
                    "summary": _clean_text(raw_root.get("summary"), 60),
                },
                "children": children,
                "kb_id": kb_id,
                "source": "preset",
            }

    # 优化: 检查缓存（带单飞,见 _mindmap_cached_build）
    # 键必须含 subject 与 model_name：脑图不检索知识库，kb_id 常为 None，内容完全
    # 由 subject（学科，来自 domain：ne/csh/fmca）与模型决定。缺 subject 会让不同
    # 学科对同名主题（如「传感器」）互相命中，最长 TTL 内返回错误学科内容。
    cache_key = f"map:{subject}:{model_name or ''}:{topic}:{kb_id}:{max_children}"

    async def _build() -> dict[str, Any]:
        # 轻量化构建：脑图仅依据提示词直接调用模型生成，不检索知识库（避免延迟与超时）。
        from backend.application.explore.prompts import build_map_prompt

        prompt = build_map_prompt(subject=subject, topic=topic, max_children=max_children, context="")
        parsed = await _llm_json(prompt, model_name=model_name, timeout=TIMEOUT_MAP, max_tokens=1024, temperature=0.5)

        root = {"id": "n_root", "title": topic, "summary": ""}
        children: list[dict[str, Any]] = []
        if isinstance(parsed, dict):
            raw_root = parsed.get("root")
            if isinstance(raw_root, dict):
                root["title"] = _clean_text(raw_root.get("title"), 40) or topic
                root["summary"] = _clean_text(raw_root.get("summary"), 60)
            children = _normalize_nodes(parsed.get("children"), max_children)

        source = "llm"
        if not children:
            children = [
                {"id": _gen_id("n"), "title": t, "summary": "", "expandable": True}
                for t in _fallback_children(topic)
            ][:max_children]
            source = "fallback"

        return {"root": root, "children": children, "kb_id": kb_id, "source": source}

    result, from_cache = await _mindmap_cached_build(cache_key, _build)
    if from_cache:
        logger.info("[Explore] 命中脑图缓存: %s", cache_key)
        return {**result, "source": "cache"}
    return result


async def expand_node(
    *, node_title: str, path: list[str], kb_id: str | None, max_children: int = 5, subject: str | None = None, model_name: str | None = None
) -> dict[str, Any]:
    """展开节点的子知识点。

    优化: 添加服务端缓存机制,相同节点直接返回缓存结果
    """
    subject = subject or DEFAULT_SUBJECT
    node_title = _clean_text(node_title, 40)
    max_children = max(2, min(6, int(max_children or 5)))
    # 用解析后的实际模型名入键（与 _build 内 _llm_json 调用一致）
    resolved_model = resolve_learning_fast_model_name(model_name)

    # 优化: 检查缓存（带单飞,见 _mindmap_cached_build）
    # 键含 subject 与解析后的模型名：展开内容由学科（domain）与模型决定，缺 subject
    # 会让不同学科的同名节点跨域串味（与 generate_map 同理）。
    cache_key = (
        f"expand:{subject}:{resolved_model or ''}:{node_title}:"
        f"{'|'.join(path or [])}:{kb_id}:{max_children}"
    )

    async def _build() -> dict[str, Any]:
        # 轻量化构建：节点展开仅依据提示词直接调用模型生成，不检索知识库。
        from backend.application.explore.prompts import build_expand_prompt

        prompt = build_expand_prompt(
            subject=subject, node_title=node_title, path=path or [], max_children=max_children, context=""
        )
        parsed = await _llm_json(
            prompt,
            model_name=resolved_model,
            timeout=TIMEOUT_EXPAND,
            max_tokens=1024,
            temperature=0.5,
        )

        children: list[dict[str, Any]] = []
        if isinstance(parsed, dict):
            children = _normalize_nodes(parsed.get("children"), max_children)
        elif isinstance(parsed, list):
            children = _normalize_nodes(parsed, max_children)

        source = "llm"
        if not children:
            children = [
                {"id": _gen_id("n"), "title": title, "summary": summary, "expandable": True}
                for title, summary in _fallback_child_nodes(node_title, subject)
            ][:max_children]
            source = "fallback"

        return {"children": children, "source": source}

    result, from_cache = await _mindmap_cached_build(cache_key, _build)
    if from_cache:
        logger.info("[Explore] 命中节点展开缓存: %s", cache_key)
        return {**result, "source": "cache"}
    return result


async def gather_resources(
    *, node_title: str, kb_id: str | None, top_k: int = 8
) -> dict[str, Any]:
    """获取知识点关联资源。

    优化: 添加缓存机制,相同知识点直接返回缓存结果
    """
    node_title = _clean_text(node_title, 60)
    top_k = max(3, min(15, int(top_k or 8)))

    # 优化: 检查缓存（带单飞,见 _mindmap_cached_build）
    cache_key = f"resources:{node_title}:{kb_id}:{top_k}"

    async def _build() -> dict[str, Any]:
        _context, sources = await _retrieve_context(node_title, kb_id, chunk_top_k=top_k)
        for s in sources:
            s["kb_id"] = kb_id
            s["download_url"] = build_resource_download_url(kb_id, s.get("doc_id"))

        return {"resources": sources[:top_k], "grounded": bool(sources)}

    result, from_cache = await _mindmap_cached_build(cache_key, _build)
    if from_cache:
        logger.info("[Explore] 命中资源缓存: %s", cache_key)
    return result


async def generate_quiz(
    *,
    node_title: str,
    kb_id: str | None,
    types: list[str],
    difficulty: str = "all",
    count: int = 6,
    subject: str | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    subject = subject or DEFAULT_SUBJECT
    node_title = _clean_text(node_title, 60)
    valid_types = [t for t in (types or []) if t in {"choice", "multi", "judge", "short", "calc"}]
    if not valid_types:
        valid_types = ["choice", "judge", "short"]
    difficulty = difficulty if difficulty in {"all", "easy", "medium", "hard"} else "all"
    count = max(2, min(12, int(count or 6)))
    started_at = time.perf_counter()
    context, sources, context_source = await _retrieve_quiz_context(node_title, kb_id)

    from backend.application.explore.prompts import build_quiz_prompt

    prompt = build_quiz_prompt(
        subject=subject, node_title=node_title, types=valid_types, difficulty=difficulty, count=count, context=context
    )
    parsed = await _llm_json(prompt, model_name=model_name, timeout=TIMEOUT_QUIZ, max_tokens=1800, temperature=0.45)

    questions: list[dict[str, Any]] = []
    raw_questions = []
    if isinstance(parsed, dict):
        raw_questions = parsed.get("questions") or []
    elif isinstance(parsed, list):
        raw_questions = parsed
    source_ids = [s.get("doc_id") for s in sources if s.get("doc_id")][:3]
    for item in raw_questions:
        q = _normalize_question(item, source_ids)
        if q:
            questions.append(q)
        if len(questions) >= count:
            break

    llm_question_count = len(questions)
    source = "llm"
    if len(questions) < count:
        fallback_questions = _fallback_questions(node_title, valid_types, difficulty, count)
        existing_stems = {q.get("stem") for q in questions}
        for q in fallback_questions:
            if q.get("stem") in existing_stems:
                continue
            questions.append(q)
            existing_stems.add(q.get("stem"))
            if len(questions) >= count:
                break
        source = "mixed" if llm_question_count else "fallback"

    logger.info(
        "[Explore] 习题生成完成: node=%s, source=%s, context=%s, count=%d, elapsed=%.1fms",
        node_title,
        source,
        context_source,
        len(questions),
        (time.perf_counter() - started_at) * 1000,
    )
    return {"questions": questions, "source": source}


async def grade_answer(
    *, question: dict[str, Any], user_answer: str, subject: str | None = None, model_name: str | None = None
) -> dict[str, Any]:
    subject = subject or DEFAULT_SUBJECT
    stem = _clean_text(question.get("stem"), 400)
    reference = _clean_text(question.get("reference_answer"), 800)
    # 采分点逐条截断 + 条数封顶：这是匿名可调用的 LLM 端点，无界列表可把
    # prompt 撑到任意大小（成本/上下文双风险）。
    scoring_points = question.get("scoring_points") or []
    if not isinstance(scoring_points, list):
        scoring_points = []
    scoring_points = [
        _clean_text(point, 200) for point in scoring_points[:_MAX_SCORING_POINTS]
    ]
    scoring_points = [point for point in scoring_points if point]

    from backend.application.explore.prompts import build_grade_prompt

    prompt = build_grade_prompt(
        subject=subject,
        stem=stem,
        reference_answer=reference,
        scoring_points=scoring_points,
        user_answer=_clean_text(user_answer, 1500),
    )
    parsed = await _llm_json(prompt, model_name=model_name, timeout=TIMEOUT_GRADE, max_tokens=1024, temperature=0.2)
    if isinstance(parsed, dict):
        try:
            score = int(parsed.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))
        verdict = str(parsed.get("verdict", "")).strip() or ("正确" if score >= 80 else "部分正确" if score >= 40 else "错误")
        missing = parsed.get("missing_points") or []
        if not isinstance(missing, list):
            missing = []
        return {
            "score": score,
            "verdict": verdict,
            "feedback": _clean_text(parsed.get("feedback"), 200),
            "missing_points": [_clean_text(m, 60) for m in missing if str(m).strip()],
        }
    return {"score": 0, "verdict": "无法评分", "feedback": "评分服务暂时不可用，请稍后重试。", "missing_points": []}


async def generate_detail(
    *,
    node_title: str,
    path: list[str],
    kb_id: str | None,
    subject: str | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    """生成知识点详解（非流式，基于知识库检索 + LLM）。"""
    subject = subject or DEFAULT_SUBJECT
    node_title = _clean_text(node_title, 60) or subject
    context, sources = await _retrieve_context(node_title, kb_id, chunk_top_k=10)
    if not context.strip():
        return {"content": "", "sources": sources, "grounded": False}

    from backend.application.explore.prompts import build_detail_prompt

    prompt = build_detail_prompt(
        subject=subject,
        node_title=node_title,
        path=path or [node_title],
        context=context,
    )
    content = await _llm_text(
        prompt,
        model_name=model_name,
        timeout=TIMEOUT_DETAIL,
        max_tokens=2048,
        temperature=0.3,
    )
    content = str(content or "").strip()
    for source in sources:
        source["kb_id"] = kb_id
        source["download_url"] = build_resource_download_url(kb_id, source.get("doc_id"))
    return {
        "content": content,
        "sources": sources[:8],
        "grounded": bool(content and sources),
    }


def build_detail_question(node_title: str, path: list[str]) -> str:
    """将知识点构造为面向轻量问答智能体的提问。"""
    node_title = _clean_text(node_title, 60)
    ctx = " → ".join([p for p in (path or []) if p][-3:])
    ctx_hint = f"（学习脉络：{ctx}）" if ctx else ""
    return (
        f"请系统、详细地讲解知识点「{node_title}」{ctx_hint}。"
        "要求使用规范 Markdown 分节组织（基本定义、核心原理、典型结构、关键要点），"
        "如有公式请使用 KaTeX 语法，内容需基于知识库、专业准确。"
    )


def build_detail_followup_question(
    *,
    node_title: str,
    path: list[str],
    user_question: str,
    detail_content: str = "",
    history: list[dict[str, Any]] | None = None,
) -> str:
    """构造详解输出内容的多轮追问提示。"""
    node_title = _clean_text(node_title, 60)
    path_text = " → ".join([_clean_text(p, 40) for p in (path or []) if str(p).strip()])
    detail = str(detail_content or "").strip()
    if len(detail) > 8000:
        detail = f"{detail[:8000]}\n\n[以上为当前详解内容节选]"
    history_lines: list[str] = []
    for item in (history or [])[-6:]:
        role = "用户" if item.get("role") == "user" else "助手"
        content = _clean_text(item.get("content"), 500)
        if content:
            history_lines.append(f"{role}: {content}")
    history_block = "\n".join(history_lines) if history_lines else "无"
    return (
        "你是探索式学习中的详解内容追问助手。请围绕用户正在阅读的详解内容回答追问，"
        "回答要直接、准确、适合继续学习；不要输出思考过程。\n\n"
        f"当前知识点：{node_title}\n"
        f"学习路径：{path_text or node_title}\n"
        f"当前详解内容：\n{detail or '当前详解内容仍在生成或为空，请结合知识点和知识库上下文回答。'}\n\n"
        f"最近对话：\n{history_block}\n\n"
        f"用户追问：{_clean_text(user_question, 1200)}\n\n"
        "回答要求：\n"
        "1. 先直接回答问题，再给出必要解释。\n"
        "2. 若问题适合延伸，补充 1-3 个可继续探索的方向。\n"
        "3. 如果知识库证据不足，请明确说明，并用通用专业知识谨慎补充。\n"
        "4. 使用 Markdown，避免过长。"
    )


async def stream_detail_followup_answer(
    *,
    node_title: str,
    path: list[str],
    user_question: str,
    detail_content: str = "",
    kb_id: str | None,
    subject: str | None = None,
    history: list[dict[str, Any]] | None = None,
    model_name: str | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """流式生成围绕当前详解输出内容的多轮追问，yield (event_type, payload)。"""
    from langchain_core.messages import HumanMessage

    from backend.core.models import create_chat_model_async

    subject = subject or DEFAULT_SUBJECT
    question = _clean_text(user_question, 1200)
    if not question:
        yield ("error", "请输入想追问的问题")
        return

    retrieval_query = f"{node_title} {question}".strip()
    context, sources = await _retrieve_context(retrieval_query, kb_id, chunk_top_k=8)
    for source in sources:
        source["kb_id"] = kb_id
        source["download_url"] = build_resource_download_url(kb_id, source.get("doc_id"))
    if sources:
        yield ("sources", sources[:8])

    prompt = build_detail_followup_question(
        node_title=node_title,
        path=path,
        user_question=question,
        detail_content=detail_content,
        history=history,
    )
    if context.strip():
        prompt = (
            f"{prompt}\n\n"
            f"学科领域：{subject}\n"
            "可参考的知识库片段如下。请优先依据这些片段回答，并在不确定时说明限制。\n"
            f"{context}"
        )
    else:
        prompt = (
            f"{prompt}\n\n"
            f"学科领域：{subject}\n"
            "当前未检索到可用知识库片段，请用通用专业知识谨慎回答。"
        )

    try:
        model = (
            await create_chat_model_async(
                name=resolve_learning_fast_model_name(model_name),
                thinking_enabled=False,
            )
        ).bind(temperature=0.2, max_tokens=1400)
        async for chunk in model.astream([HumanMessage(content=prompt)]):
            content = _normalize_content(getattr(chunk, "content", ""))
            if content:
                yield ("chunk", content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Explore] 详解追问失败: %s", exc)
        yield ("error", "追问回答生成失败，请稍后重试")


# ---------------------------------------------------------------------------
# 归一化与兜底
# ---------------------------------------------------------------------------

def _normalize_nodes(raw: Any, max_children: int) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return nodes
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            title = _clean_text(
                item.get("title")
                or item.get("name")
                or item.get("label")
                or item.get("text")
                or item.get("knowledge_point")
                or item.get("point"),
                28,
            )
            summary = _clean_text(
                item.get("summary")
                or item.get("description")
                or item.get("desc")
                or item.get("hint")
                or item.get("explanation"),
                60,
            )
            expandable = item.get("expandable", True)
            if isinstance(expandable, str):
                expandable = expandable.strip().lower() not in {"false", "0", "no", "否", "不可展开"}
            else:
                expandable = bool(expandable)
        else:
            title, summary, expandable = _clean_text(item, 28), "", True
        title = re.sub(r"^\s*(?:[-*]|\d+[.)、]|[A-Z][.)、])\s*", "", title).strip()
        if _is_placeholder_text(title, _PLACEHOLDER_TITLES):
            continue
        if _is_placeholder_text(summary, _PLACEHOLDER_SUMMARIES):
            summary = ""
        if not title or title in seen:
            continue
        seen.add(title)
        nodes.append({"id": _gen_id("n"), "title": title, "summary": summary, "expandable": expandable})
        if len(nodes) >= max_children:
            break
    return nodes


def _normalize_question(item: Any, source_ids: list[str]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    qtype = str(item.get("type") or item.get("question_type") or item.get("题型") or "").strip().lower()
    qtype = {
        "单选": "choice",
        "单选题": "choice",
        "选择题": "choice",
        "多选": "multi",
        "多选题": "multi",
        "判断": "judge",
        "判断题": "judge",
        "简答": "short",
        "简答题": "short",
        "问答题": "short",
        "计算": "calc",
        "计算题": "calc",
    }.get(qtype, qtype)
    if qtype not in {"choice", "multi", "judge", "short", "calc"}:
        return None
    stem = _clean_text(item.get("stem") or item.get("question") or item.get("title") or item.get("题干"), 400)
    if not stem:
        return None
    difficulty = str(item.get("difficulty") or item.get("难度") or "medium").strip().lower()
    difficulty = {"简单": "easy", "容易": "easy", "中等": "medium", "困难": "hard", "较难": "hard"}.get(
        difficulty,
        difficulty,
    )
    if difficulty not in {"easy", "medium", "hard"}:
        difficulty = "medium"
    q: dict[str, Any] = {
        "id": _gen_id("q"),
        "type": qtype,
        "difficulty": difficulty,
        "stem": stem,
        "explanation": _clean_text(item.get("explanation"), 400),
    }
    if source_ids:
        q["source_refs"] = source_ids

    if qtype in {"choice", "multi"}:
        options = []
        raw_options = item.get("options") or []
        if isinstance(raw_options, dict):
            raw_options = [{"key": key, "text": value} for key, value in raw_options.items()]
        for idx, opt in enumerate(raw_options):
            if isinstance(opt, dict):
                key = str(opt.get("key", "")).strip() or chr(ord("A") + idx)
                text = _clean_text(opt.get("text") or opt.get("label") or opt.get("content") or opt.get("value"), 160)
            else:
                key = chr(ord("A") + idx)
                text = _clean_text(opt, 160)
            if text:
                options.append({"key": key, "text": text})
        if len(options) < 2:
            return None
        q["options"] = options
        answer = item.get("answer")
        if isinstance(answer, list):
            q["answer"] = "".join(str(a).strip() for a in answer)
        else:
            q["answer"] = str(answer or "").strip()
    elif qtype == "judge":
        ans = str(item.get("answer", "")).strip()
        q["answer"] = "正确" if ans in {"正确", "对", "true", "True", "T", "是"} else "错误"
    else:  # short / calc
        q["reference_answer"] = _clean_text(
            item.get("reference_answer") or item.get("answer") or item.get("参考答案"),
            800,
        )
        points = item.get("scoring_points") or []
        if isinstance(points, list):
            q["scoring_points"] = [_clean_text(p, 80) for p in points if str(p).strip()]
        else:
            q["scoring_points"] = []
    return q


def _fallback_children(topic: str) -> list[str]:
    return [f"{topic}·基本概念", f"{topic}·工作原理", f"{topic}·典型结构", f"{topic}·关键要点", f"{topic}·应用"]


def _fallback_child_nodes(topic: str, subject: str) -> list[tuple[str, str]]:
    topic = _clean_text(topic, 18) or _clean_text(subject, 18) or "知识点"
    if _is_placeholder_text(topic, _PLACEHOLDER_TITLES):
        topic = _clean_text(subject, 18) or "知识点"
    templates = [
        ("基本概念", "明确术语内涵与学习边界"),
        ("工作原理", "理解关键机制与作用过程"),
        ("系统组成", "梳理主要模块及其关系"),
        ("关键参数", "掌握影响性能的核心指标"),
        ("典型应用", "联系实际场景与使用方法"),
        ("常见问题", "辨析易错点与处理思路"),
    ]
    return [(f"{topic}{suffix}", summary) for suffix, summary in templates]


def _fallback_questions(
    node_title: str,
    types: list[str],
    difficulty: str,
    count: int,
) -> list[dict[str, Any]]:
    node = _clean_text(node_title, 40) or "该知识点"
    requested = [t for t in types if t in {"choice", "multi", "judge", "short", "calc"}] or ["choice", "judge", "short"]
    diff = difficulty if difficulty in {"easy", "medium", "hard"} else "medium"
    factories = {
        "choice": lambda: {
            "id": _gen_id("q"),
            "type": "choice",
            "difficulty": diff,
            "stem": f"学习「{node}」时，首先应重点把握哪一类内容？",
            "options": [
                {"key": "A", "text": "概念定义、基本原理与适用条件"},
                {"key": "B", "text": "与主题无关的历史轶事"},
                {"key": "C", "text": "脱离场景的随机参数"},
                {"key": "D", "text": "不需要验证的经验结论"},
            ],
            "answer": "A",
            "explanation": "概念、原理和适用条件是继续深入学习的基础。",
        },
        "multi": lambda: {
            "id": _gen_id("q"),
            "type": "multi",
            "difficulty": diff,
            "stem": f"围绕「{node}」做知识梳理时，哪些维度通常是有价值的？",
            "options": [
                {"key": "A", "text": "核心概念"},
                {"key": "B", "text": "工作机理"},
                {"key": "C", "text": "关键参数或影响因素"},
                {"key": "D", "text": "完全无关的背景信息"},
            ],
            "answer": "ABC",
            "explanation": "知识梳理应优先覆盖概念、机理和关键影响因素。",
        },
        "judge": lambda: {
            "id": _gen_id("q"),
            "type": "judge",
            "difficulty": diff,
            "stem": f"判断：学习「{node}」时，只记住名称而不理解原理也能支持后续应用。",
            "answer": "错误",
            "explanation": "名称记忆不足以支撑分析和应用，仍需理解原理与条件。",
        },
        "short": lambda: {
            "id": _gen_id("q"),
            "type": "short",
            "difficulty": diff,
            "stem": f"请简述「{node}」的学习要点。",
            "reference_answer": "可从基本定义、核心原理、系统组成、关键参数、典型应用和常见问题等方面概括。",
            "scoring_points": ["说明基本定义", "概括核心原理", "列出关键影响因素", "联系典型应用"],
            "explanation": "开放题重点考查是否形成结构化理解。",
        },
        "calc": lambda: {
            "id": _gen_id("q"),
            "type": "calc",
            "difficulty": diff,
            "stem": f"若「{node}」涉及定量分析，请说明建立计算题解题步骤时应包含哪些环节。",
            "reference_answer": "应先明确已知量与待求量，再选择适用公式或模型，代入计算，最后检查单位、量级和适用条件。",
            "scoring_points": ["明确已知量和待求量", "选择适用关系式", "完成计算过程", "检查单位和条件"],
            "explanation": "未掌握具体数值时，可先考查定量分析流程。",
        },
    }
    questions: list[dict[str, Any]] = []
    idx = 0
    while len(questions) < count and idx < count * 2:
        qtype = requested[idx % len(requested)]
        q = factories[qtype]()
        round_no = idx // len(requested)
        if round_no:
            q["stem"] = f"{q['stem']}（拓展{round_no + 1}）"
        questions.append(q)
        idx += 1
    return questions[:count]
