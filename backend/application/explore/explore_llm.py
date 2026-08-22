"""探索 LLM 域:JSON 解析与修复、RAG/测验上下文检索、LLM JSON/文本调用与来源提取。(拆自 explore/service.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.application.llm_content import normalize_llm_content as _normalize_content
from backend.config.log_privacy import text_fingerprint
from typing import Any
import asyncio
import json
import re
import time
from backend.application.explore.explore_config import (
    QUIZ_CONTEXT_CHUNK_TOP_K,
    QUIZ_CONTEXT_MAX_CHARS,
    TIMEOUT_QUIZ_RETRIEVE,
    resolve_learning_fast_model_name,
)
from backend.application.explore.explore_resources import (
    _derive_resource_title,
    _strip_resource_noise,
    _title_from_content,
)

logger = get_logger(__name__)


async def _aclose_iterator(iterator: Any) -> None:
    """显式关闭 astream 迭代器，避免超时/提前退出后半开连接等 GC。"""
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Explore] 关闭 LLM 流迭代器失败: %s", exc)

_IMG_BLOCK_RE = re.compile(
    r"Image Content Analysis:\s*\n?Image Path:[^\n]*\n?(?:Captions:[^\n]*\n?)?(?:Footnotes:[^\n]*\n?)?",
    re.IGNORECASE,
)
_IMG_PATH_RE = re.compile(r"Image Path:\s*\S+", re.IGNORECASE)


def _strip_image_blocks(text: str) -> str:
    """移除片段中的图片分析块/原始图片路径，避免在关联资源里露出预签名 URL 文本。"""
    cleaned = _IMG_BLOCK_RE.sub("", text or "")
    cleaned = _IMG_PATH_RE.sub("", cleaned)
    return cleaned


_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


_INVALID_JSON_BACKSLASH_RE = re.compile(r"\\(?![\"\\/bfnrtu])")


def _parse_json(content: str) -> Any | None:
    """从 LLM 输出中稳健提取 JSON（兼容 ```json 包裹 / 前后多余文本）。"""
    text = str(content or "").strip()
    if not text:
        return None

    candidates: list[str] = []
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text)
    obj = _JSON_OBJ_RE.search(text)
    if obj:
        candidates.append(obj.group(0))
    arr = _JSON_ARRAY_RE.search(text)
    if arr:
        candidates.append(arr.group(0))

    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            try:
                repaired = _INVALID_JSON_BACKSLASH_RE.sub(r"\\\\", cand)
                return json.loads(repaired)
            except (json.JSONDecodeError, TypeError):
                continue
    return None


async def _retrieve_context(query: str, kb_id: str | None, *, chunk_top_k: int = 8) -> tuple[str, list[dict[str, Any]]]:
    """调用统一检索入口，返回 (context_str, sources)。失败时降级为空。

    优化: 探索式学习场景直接传入知识点标题作为外部关键词,跳过LLM关键词提取
    """
    try:
        from backend.application.knowledge.internal_client import internal_retrieve

        response = await internal_retrieve(
            query=query,
            kb_id=kb_id,
            chunk_top_k=chunk_top_k,
            hl_keywords=[query],  # 优化: 直接传入知识点标题,跳过关键词提取
            ll_keywords=[],
            seed_weights={query: 1.0},
            disable_stage1_oversample=True,  # 优化: 关闭过采样,减少召回量
            retrieval_profile_override="lightweight",
        )
        sources = _extract_sources(response)
        return (response.get("context") or ""), sources
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Explore] retrieve_context 失败 (kb=%s, query_meta=%s): %s",
            kb_id,
            text_fingerprint(query),
            exc,
        )
        return "", []


async def _retrieve_quiz_context(query: str, kb_id: str | None) -> tuple[str, list[dict[str, Any]], str]:
    """习题生成优先保证响应速度；检索慢或失败时降级为通用命题。"""
    start = time.perf_counter()
    task = asyncio.create_task(_retrieve_context(query, kb_id, chunk_top_k=QUIZ_CONTEXT_CHUNK_TOP_K))

    def _consume_late_result(done: asyncio.Task[tuple[str, list[dict[str, Any]]]]) -> None:
        try:
            done.result()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Explore] 习题后台检索结束于异常: %s", exc)
        except asyncio.CancelledError:
            logger.debug("[Explore] 习题后台检索被取消")

    try:
        context, sources = await asyncio.wait_for(asyncio.shield(task), timeout=max(1.0, TIMEOUT_QUIZ_RETRIEVE))
        elapsed = (time.perf_counter() - start) * 1000
        if len(context) > QUIZ_CONTEXT_MAX_CHARS:
            context = f"{context[:QUIZ_CONTEXT_MAX_CHARS].rstrip()}\n\n[以上为知识库上下文节选]"
        logger.info(
            "[Explore] 习题检索完成: query_meta=%s, chunks=%d, chars=%d, elapsed=%.1fms",
            text_fingerprint(query),
            len(sources),
            len(context),
            elapsed,
        )
        return context, sources, "retrieved"
    except asyncio.TimeoutError:
        task.add_done_callback(_consume_late_result)
        logger.info(
            "[Explore] 习题检索超时降级: query_meta=%s, timeout=%.1fs",
            text_fingerprint(query),
            TIMEOUT_QUIZ_RETRIEVE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Explore] 习题检索失败降级: query_meta=%s, err=%s",
            text_fingerprint(query),
            exc,
        )
    return "", [], "skipped"


def _extract_sources(retrieval_response: Any) -> list[dict[str, Any]]:
    """从知识服务检索响应提取关联资源（去重，保留最高分）。"""
    if not retrieval_response:
        return []
    chunks = retrieval_response.get("chunks") or []
    by_doc: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        raw_content = _strip_image_blocks(str(chunk.get("content", "") or "")).strip()
        content = _strip_resource_noise(raw_content)
        file_path = str(chunk.get("file_path", chunk.get("source", "")) or "")
        doc_id = str(chunk.get("doc_id", chunk.get("_doc_id", "")) or "")
        full_doc_id = str(chunk.get("full_doc_id", "") or "")
        score = float(
            chunk.get("_fused_score")
            or chunk.get("fused_score")
            or chunk.get("_rerank_score")
            or chunk.get("rerank_score")
            or chunk.get("score")
            or 0.0
        )
        key = doc_id or full_doc_id or file_path or content[:32]
        if not key:
            continue
        snippet = _strip_resource_noise(content, max_len=240) or "该资源与当前知识点相关，可作为补充阅读材料。"
        file_name = file_path.rsplit("/", 1)[-1] if file_path else ""
        doc_name = _derive_resource_title(file_name, _title_from_content(content), doc_id)
        existing = by_doc.get(key)
        if existing is None:
            by_doc[key] = {
                "doc_id": doc_id or full_doc_id,
                "doc_name": doc_name,
                "file_path": file_path,
                "snippet": snippet,
                "score": round(score, 4),
            }
            order.append(key)
        elif score > existing.get("score", 0.0):
            existing["score"] = round(score, 4)
            if len(snippet) > len(existing.get("snippet", "")):
                existing["snippet"] = snippet
    resources = [by_doc[k] for k in order]
    resources.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return resources


async def _llm_json(prompt: str, *, model_name: str | None, timeout: float, max_tokens: int = 2048, temperature: float = 0.3) -> Any | None:
    """调用 LLM 并解析 JSON 输出，带超时与异常兜底。"""
    from langchain_core.messages import HumanMessage

    from backend.core.models import create_chat_model_async

    try:
        model = await create_chat_model_async(name=resolve_learning_fast_model_name(model_name), thinking_enabled=False)
        model = model.bind(temperature=temperature, max_tokens=max_tokens)

        # 使用 astream 获取流式输出；wait_for 超时只取消 __anext__ 而不关闭
        # 生成器，半开连接要等 GC——所有提前退出口都必须显式 aclose。
        stream = model.astream([HumanMessage(content=prompt)])
        iterator = stream.__aiter__()
        try:
            # 1. 尝试在指定超时时间内获取首个 chunk
            try:
                first_chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
            except StopAsyncIteration:
                return None
            except asyncio.TimeoutError:
                logger.info("[Explore] LLM 首 token 超时 (timeout=%.1fs)", timeout)
                return None

            chunks = [first_chunk]

            # 2. 已有首包输出，后续接收不设限（或设置较长的总保护时间），读取完整个流
            # 为了防止极端情况下的无限挂起，使用 300 秒长超时保护
            while True:
                try:
                    chunk = await asyncio.wait_for(iterator.__anext__(), timeout=300.0)
                    chunks.append(chunk)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.warning("[Explore] LLM 读取后续 token 超时保护触发")
                    break
        finally:
            await _aclose_iterator(iterator)

        # 3. 拼接并解析 JSON
        content_parts = []
        reasoning_parts = []
        for chunk in chunks:
            c = getattr(chunk, "content", "")
            if c:
                content_parts.append(_normalize_content(c))

            r = ""
            if hasattr(chunk, "additional_kwargs") and isinstance(chunk.additional_kwargs, dict):
                r = chunk.additional_kwargs.get("reasoning_content") or chunk.additional_kwargs.get("reasoning") or ""
            if not r and hasattr(chunk, "reasoning_content"):
                r = getattr(chunk, "reasoning_content", "")
            if r:
                reasoning_parts.append(_normalize_content(r))

        full_content = "".join(content_parts)
        res = _parse_json(full_content)
        if res is not None:
            return res

        full_reasoning = "".join(reasoning_parts)
        res = _parse_json(full_reasoning)
        if res is not None:
            return res

        return _parse_json(full_content + "\n" + full_reasoning)

    except Exception as exc:  # noqa: BLE001
        logger.warning("[Explore] LLM 调用失败: %s", exc)
        return None


async def _llm_text(
    prompt: str,
    *,
    model_name: str | None,
    timeout: float,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> str | None:
    """调用 LLM 并返回 Markdown 文本，带超时与异常兜底。"""
    from langchain_core.messages import HumanMessage

    from backend.core.models import create_chat_model_async

    try:
        model = await create_chat_model_async(name=resolve_learning_fast_model_name(model_name), thinking_enabled=False)
        model = model.bind(temperature=temperature, max_tokens=max_tokens)

        stream = model.astream([HumanMessage(content=prompt)])
        iterator = stream.__aiter__()
        try:
            try:
                first_chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
            except StopAsyncIteration:
                return None
            except asyncio.TimeoutError:
                logger.info("[Explore] LLM 详解首 token 超时 (timeout=%.1fs)", timeout)
                return None

            chunks = [first_chunk]
            while True:
                try:
                    chunk = await asyncio.wait_for(iterator.__anext__(), timeout=300.0)
                    chunks.append(chunk)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.warning("[Explore] LLM 详解读取后续 token 超时保护触发")
                    break
        finally:
            await _aclose_iterator(iterator)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        for chunk in chunks:
            chunk_content = getattr(chunk, "content", "")
            if chunk_content:
                content_parts.append(_normalize_content(chunk_content))

            reasoning = ""
            if hasattr(chunk, "additional_kwargs") and isinstance(chunk.additional_kwargs, dict):
                reasoning = (
                    chunk.additional_kwargs.get("reasoning_content")
                    or chunk.additional_kwargs.get("reasoning")
                    or ""
                )
            if not reasoning and hasattr(chunk, "reasoning_content"):
                reasoning = getattr(chunk, "reasoning_content", "")
            if reasoning:
                reasoning_parts.append(_normalize_content(reasoning))

        full_content = "".join(content_parts).strip()
        if full_content:
            return full_content
        return "".join(reasoning_parts).strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Explore] LLM 文本生成失败: %s", exc)
        return None
