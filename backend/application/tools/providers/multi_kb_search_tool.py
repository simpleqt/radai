"""多知识库检索工具 - 优化版

同时检索多个知识库，使用倒数排名融合算法合并结果。
用于核电子学全功能智能体的 RAG 检索。

优化：
  - 并发查询多个知识库
  - 单库与 retrieve_context 一致：阶段1召回 + 内容过滤（PPR/Rerank/MMR）
  - 超时保护（每个知识库独立超时）
  - 性能统计和详细日志
  - 错误隔离（单个知识库失败不影响其他）

排障说明（与日志「向量搜索」打印次数相关）：
  - 实体召回 Path-A 会对「整句向量」打 1 次 keyword_index，再对高阶关键词 hl 的前 3 个各打 1 次，
    故有非空 hl 时同一知识库下 ``per_kb_index`` 的「返回 N 个结果」日志通常出现 4 次（N 由
    ``entity_vec_top_k`` 等配置决定，与 multi_kb 的 top_k 不是同一参数）。
  - **默认开启** ``MULTI_KB_PATH_A_QUERY_ONLY``：共享单库关键词后，各 KB 的 Path-A 仅整句 1 路
    （双库合计约 2 次 keyword_index，而非 8 次）。需要更高召回可设 ``MULTI_KB_PATH_A_QUERY_ONLY=0``。
  - 未显式传入高/低词时，仅在首个 KB 提取一次关键词并复用，避免双库各 ~2.5s 的重复提取。
  - 降低 ``MULTI_KB_SEARCH_DEFAULT_TOP_K`` / ``MULTI_KB_SEARCH_MAX_TOP_K`` 可减轻阶段1与过滤开销。
    把上限提高到 **10** 会扩大候选池与 Milvus 调用量，**通常更慢**；要提速请**调低**而非调高。
"""

import asyncio
import logging
import os
import re
import threading
import time
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from backend.config.log_privacy import text_fingerprint

from backend.application.tools.providers.multi_kb_caching import (  # noqa: F401  (含测试兼容 re-export)
    _build_request_cache_key,
    _cached_reuse_result,
    _context_budget_exhausted_result,
    _ensure_structured_result,
    _fit_result_to_output_limit,
    _mark_structured_cache_hit,
    _reserve_result_for_turn,
    _structured_result,
    _structured_text,
    _truncate_kb_result_text,
)
# 原 multi_kb_fusion 中的常量:融合在 omnigraph 服务端执行,客户端仅做上限校验。
MAX_CHUNK_TOP_K = 60
from backend.application.tools.providers.multi_kb_remote import (  # noqa: F401  (含测试兼容 re-export)
    _execute_remote_multi_kb_search,
    _get_rag_http_client,
    _rag_service_base_url,
    _remap_remote_registered_images,
    close_rag_http_clients,
)
from backend.application.tools.providers.multi_kb_types import FilterMode

logger = logging.getLogger(__name__)

# 原 multi_kb_single 中的常量:本地检索路径已移至 omnigraph,超时常量
# 仅用于请求内缓存去重的等待上限。
KB_SEARCH_TIMEOUT = 60  # 单个知识库查询超时（秒）
SUBAGENT_KB_SEARCH_TIMEOUT = int(os.getenv("SUBAGENT_KB_SEARCH_TIMEOUT", "90"))
TOTAL_SEARCH_TIMEOUT = 90  # 总查询超时（秒）
SUBAGENT_TOTAL_SEARCH_TIMEOUT = int(os.getenv("SUBAGENT_TOTAL_SEARCH_TIMEOUT", "120"))
if SUBAGENT_TOTAL_SEARCH_TIMEOUT <= SUBAGENT_KB_SEARCH_TIMEOUT:
    SUBAGENT_TOTAL_SEARCH_TIMEOUT = SUBAGENT_KB_SEARCH_TIMEOUT + 10
# 默认偏延迟优化（3/2）；可用环境变量或 tool 参数调高，但 MAX 仍硬封顶。
MAX_TOP_K = max(1, int(os.getenv("MULTI_KB_SEARCH_MAX_TOP_K", "12")))
_TOOL_DEFAULT_TOP_K = max(
    1, min(MAX_TOP_K, int(os.getenv("MULTI_KB_SEARCH_DEFAULT_TOP_K", "10")))
)
_TOOL_DEFAULT_CHUNK_TOP_K = max(
    1, min(MAX_CHUNK_TOP_K, int(os.getenv("MULTI_KB_SEARCH_DEFAULT_CHUNK_TOP_K", "6")))
)


try:
    _MULTI_KB_SELF_CONTENT_FILTER_MODE = FilterMode(_SELF_FILTER_RAW)
except ValueError:
    logger.warning(
        "无效的 MULTI_KB_SELF_CONTENT_FILTER_MODE=%s，回退 raw",
        _SELF_FILTER_RAW,
    )
    _MULTI_KB_SELF_CONTENT_FILTER_MODE = FilterMode.RAW


# 非轻量全功能 Agent：单次工具返回字符上限（0 表示不截断）
MAX_RESULT_CHARS_FULL_AGENT = max(
    0, int(os.getenv("MULTI_KB_SEARCH_MAX_RESULT_CHARS", "8000"))
)
# 非轻量普通模式每轮 multi_kb_search 最大调用次数
NORMAL_MAX_CALLS_PER_TURN = max(
    1, int(os.getenv("MULTI_KB_SEARCH_NORMAL_MAX_CALLS", "2"))
)


def _normalize_keywords_value(raw: str | list[str] | None) -> list[str]:
    """Normalize model-generated keyword inputs to list[str].

    Vendored from the removed in-process retrieval module.
    """
    import json

    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]

    text = str(raw).strip()
    if not text:
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass

    parts = re.split(r"[\n,，;；|]+", text)
    return [p.strip() for p in parts if p.strip()]


@tool("multi_kb_search")
async def multi_kb_search_tool(
    query: str,
    high_level_keywords: str | list[str] | None = None,
    low_level_keywords: str | list[str] | None = None,
    query_type: str | None = None,
    kb_ids: Annotated[list[str] | None, InjectedToolArg] = None,
    top_k: int = _TOOL_DEFAULT_TOP_K,
    chunk_top_k: int = _TOOL_DEFAULT_CHUNK_TOP_K,
    merge_strategy: str = "rrf",
    config: RunnableConfig = None,
) -> str:
    """搜索多个知识库并返回融合后的上下文。

    普通模式可只传 query；也支持额外传入高/低层关键词，跳过自动关键词提取。
    """
    normalized_hl = (
        _normalize_keywords_value(high_level_keywords)
        if high_level_keywords is not None
        else None
    )
    normalized_ll = (
        _normalize_keywords_value(low_level_keywords)
        if low_level_keywords is not None
        else None
    )

    registry = None
    if config and isinstance(config, dict):
        registry = config.get("configurable", {}).get("image_url_registry")

    return await execute_multi_kb_search(
        query=query,
        kb_ids=kb_ids,
        top_k=top_k,
        chunk_top_k=chunk_top_k,
        merge_strategy=merge_strategy,
        external_hl_keywords=normalized_hl,
        external_ll_keywords=normalized_ll,
        external_query_type=query_type,
        _registry=registry,
    )


async def execute_multi_kb_search(
    query: str,
    kb_ids: list[str] | None = None,
    top_k: int = _TOOL_DEFAULT_TOP_K,
    chunk_top_k: int = _TOOL_DEFAULT_CHUNK_TOP_K,
    merge_strategy: str = "rrf",
    external_hl_keywords: str | list[str] | None = None,
    external_ll_keywords: str | list[str] | None = None,
    external_query_type: str | None = None,
    budget_key: str = "multi_kb_search",
    content_filter_mode: FilterMode | None = None,
    max_result_chars: int | None = None,
    allow_similar_cache: bool = True,
    include_graph: bool = True,
    deep_retrieval: bool = False,
    _structured: bool = False,
    _registry: Any = None,
) -> Any:
    """搜索多个知识库获取相关信息。

    同时检索多个知识库，使用倒数排名融合算法合并结果。
    未显式提供知识库时，读取 MySQL 中启用的默认知识库。

    Args:
        query: 搜索查询内容
        kb_ids: 知识库ID列表；未提供时使用 MySQL 中启用的默认知识库
        top_k: 每个知识库阶段1实体/文档召回上限（与路由 retrieve_context 一致），默认见 _TOOL_DEFAULT_TOP_K
        chunk_top_k: 融合前进内容块的期望条数上限，默认见 _TOOL_DEFAULT_CHUNK_TOP_K
        merge_strategy: 合并策略，rrf(倒数排名融合) 或 weighted(加权)
        content_filter_mode: 内容过滤流水线；None 时使用 KB Profile（domain 库为 MMR_ONLY）
    """
    from backend.core.tools.request_limits import (
        TURN_MAX_EVIDENCE_CHARS,
        consume_tool_budget,
        get_retrieval_request_cache,
        get_turn_kb_chars_remaining,
    )

    if _registry is None:
        from backend.application.agent_executors.image_url_registry import get_registry

        _registry = get_registry()

    # 移除查询中的运行时元数据（如 <runtime_current_time> 块）
    query = re.sub(
        r"<runtime_current_time>[\s\S]*?</runtime_current_time>",
        "",
        query,
        flags=re.IGNORECASE,
    ).strip()

    top_k = max(1, min(top_k, MAX_TOP_K))
    chunk_top_k = max(1, min(chunk_top_k, MAX_CHUNK_TOP_K))
    from backend.application.knowledge_bases.catalog_service import (
        load_knowledge_base_catalog_cached,
    )

    catalog_entries = await load_knowledge_base_catalog_cached(
        kb_ids,
        defaults_only=kb_ids is None,
    )
    effective_kb_ids = [entry.kb_id for entry in catalog_entries]
    kb_labels = {entry.kb_id: entry.kb_name for entry in catalog_entries}
    if not effective_kb_ids:
        scope = "默认" if kb_ids is None else "所选"
        text = f"MySQL 中没有可用的{scope}知识库，请检查知识库状态和智能体绑定。"
        return _structured_result(text) if _structured else text

    # Count every accepted tool invocation, including cache hits and
    # budget-exhausted retries. Provider-only accounting lets a model keep
    # issuing no-op calls forever after the context budget is full.
    allowed, _remaining = consume_tool_budget(budget_key)
    if not allowed:
        logger.warning("[多KB搜索] 已达到本轮调用上限，跳过重复检索")
        text = "知识库检索次数已达上限，请基于已有检索结果回答。"
        result = _structured_result(
            text,
            stats={"tool_budget_exhausted": True, "injected": False},
        )
        return result if _structured else text

    normalized_hl_keywords = (
        _normalize_keywords_value(external_hl_keywords)
        if external_hl_keywords is not None
        else None
    )
    normalized_ll_keywords = (
        _normalize_keywords_value(external_ll_keywords)
        if external_ll_keywords is not None
        else None
    )

    # 未显式传入关键词时：共享单库提取 + Path-A 仅整句（见 _apply_path_a_fast_keywords）。
    user_provided_keywords = (
        external_hl_keywords is not None or external_ll_keywords is not None
    )
    if not user_provided_keywords:
        normalized_hl_keywords = None
        normalized_ll_keywords = None

    turn_remaining = get_turn_kb_chars_remaining(TURN_MAX_EVIDENCE_CHARS)
    if turn_remaining <= 0:
        logger.warning("[多KB搜索] 本轮检索上下文字符预算已用尽")
        text = (
            f"本轮知识库检索上下文已达上限（约 {TURN_MAX_EVIDENCE_CHARS} 字），"
            "请基于已有检索结果回答，勿再调用 multi_kb_search。"
        )
        result = _context_budget_exhausted_result(text)
        return result if _structured else text

    if max_result_chars and max_result_chars > 0:
        requested_result_chars = max_result_chars
    elif MAX_RESULT_CHARS_FULL_AGENT > 0:
        requested_result_chars = MAX_RESULT_CHARS_FULL_AGENT
    else:
        requested_result_chars = None
    max_result_chars = (
        min(requested_result_chars, turn_remaining)
        if requested_result_chars is not None
        else None
    )

    cache = get_retrieval_request_cache()
    cache_key = _build_request_cache_key(
        query=query,
        kb_ids=effective_kb_ids,
        top_k=top_k,
        chunk_top_k=chunk_top_k,
        merge_strategy=merge_strategy,
        external_hl_keywords=normalized_hl_keywords,
        external_ll_keywords=normalized_ll_keywords,
        external_query_type=external_query_type,
        user_provided_keywords=user_provided_keywords,
        content_filter_mode=content_filter_mode,
        # Cache identity reflects the caller's stable output policy, not the
        # momentary remaining allowance. The latter only affects injection.
        max_result_chars=requested_result_chars,
        include_graph=include_graph,
        deep_retrieval=deep_retrieval,
    )
    cached_value = cache.get_exact(cache_key)
    if cached_value is not None:
        reused = _cached_reuse_result(cached_value, "exact")
        logger.info("[多KB搜索] 命中请求级精确缓存")
        return reused if _structured else _structured_text(reused)
    similar_value = cache.get_similar(cache_key) if allow_similar_cache else None
    if similar_value is not None:
        reused = _cached_reuse_result(similar_value, "similar")
        logger.info("[多KB搜索] 命中请求级相似查询缓存")
        return reused if _structured else _structured_text(reused)

    current_thread = threading.current_thread().name
    in_subagent_thread = current_thread.startswith("subagent-exec-")
    kb_timeout = SUBAGENT_KB_SEARCH_TIMEOUT if in_subagent_thread else KB_SEARCH_TIMEOUT
    total_timeout = (
        SUBAGENT_TOTAL_SEARCH_TIMEOUT if in_subagent_thread else TOTAL_SEARCH_TIMEOUT
    )

    # Deduplicate identical in-flight retrieval calls in the same request scope.
    loop_id = id(asyncio.get_running_loop())
    existing_request_task = cache.get_in_flight(cache_key, loop_id=loop_id)
    if existing_request_task is not None and not existing_request_task.done():
        logger.info("[多KB搜索] 复用同事件循环内进行中的相同检索")
        # 有界等待:极端情况下(残留条目 + id(loop) 复用)这里可能拿到一个
        # 死循环上的任务,无界 shield 会永久挂起本分支;超时后放弃复用、
        # 走自己的检索。上限对齐本次调用的总超时。
        try:
            reused_value = await asyncio.wait_for(
                asyncio.shield(existing_request_task),
                timeout=total_timeout,
            )
        except asyncio.TimeoutError:  # noqa: UP041 -- Python 3.10 compatibility
            logger.warning(
                "[多KB搜索] 等待进行中检索超时(%ss),放弃复用改为自行检索",
                total_timeout,
            )
        else:
            in_flight_value = _cached_reuse_result(reused_value, "in_flight")
            return (
                in_flight_value
                if _structured
                else _structured_text(in_flight_value)
            )

    async def run_with_provider_capacity() -> dict[str, Any]:
        slot_wait_started = time.monotonic()
        await cache.acquire_provider_slot()
        slot_waited_ms = (time.monotonic() - slot_wait_started) * 1000.0
        if slot_waited_ms >= 100.0:
            # 并发瓶颈基线观测:等待意味着 MULTI_KB_PROVIDER_CAPACITY 已打满。
            logger.info(
                "[多KB搜索] provider 槽位等待 %.0fms", slot_waited_ms
            )
        try:
            if cache.provider_requests_cancelled():
                return _structured_result(
                    "本次请求的知识库检索已取消，未继续执行排队中的检索。",
                    stats={"cancelled": True},
                )
            cached_after_wait = cache.get_exact(cache_key)
            if cached_after_wait is not None:
                logger.info("[多KB搜索] 等待 provider 容量后命中请求级精确缓存")
                return _cached_reuse_result(cached_after_wait, "exact")
            similar_after_wait = (
                cache.get_similar(cache_key) if allow_similar_cache else None
            )
            if similar_after_wait is not None:
                logger.info("[多KB搜索] 等待 provider 容量后命中请求级相似缓存")
                return _cached_reuse_result(similar_after_wait, "similar")
            rag_service_url = _rag_service_base_url()
            if not rag_service_url:
                return _structured_result(
                    "知识库服务地址未配置：请设置 KNOWLEDGE_SERVICE_BASE_URL 或 "
                    "RAG_SERVICE_BASE_URL 指向 omnigraph 知识服务。",
                    stats={"error": "knowledge_service_unconfigured"},
                )
            logger.info("[多KB搜索] 转发到独立知识库服务: %s", rag_service_url)
            return await _execute_remote_multi_kb_search(
                base_url=rag_service_url,
                query=query,
                kb_ids=effective_kb_ids,
                top_k=top_k,
                chunk_top_k=chunk_top_k,
                merge_strategy=merge_strategy,
                external_hl_keywords=normalized_hl_keywords,
                external_ll_keywords=normalized_ll_keywords,
                external_query_type=external_query_type,
                content_filter_mode=content_filter_mode,
                max_result_chars=max_result_chars,
                include_graph=include_graph,
                deep_retrieval=deep_retrieval,
                _registry=_registry,
            )
        finally:
            cache.release_provider_slot()

    run_task = asyncio.create_task(run_with_provider_capacity())
    cache.set_in_flight(cache_key, loop_id=loop_id, task=run_task)
    try:
        result = _fit_result_to_output_limit(await run_task, max_result_chars)
        if bool((result.get("stats") or {}).get("cache_reused")):
            return result if _structured else _structured_text(result)
        # Cache provider work even when another concurrent branch consumed the
        # final context slice while this retrieval was running.
        cache.set_exact(cache_key, result)
        injected = _reserve_result_for_turn(result, TURN_MAX_EVIDENCE_CHARS)
        if injected is None:
            text = "本轮剩余检索上下文预算不足，检索结果未注入。"
            exhausted = _context_budget_exhausted_result(text)
            return exhausted if _structured else text
        return injected if _structured else _structured_text(injected)
    finally:
        cache.clear_in_flight(cache_key, run_task)


async def execute_multi_kb_search_structured(
    **kwargs: Any,
) -> dict[str, Any]:
    """Internal structured variant; public tool output remains a string."""
    from backend.application.agent_executors.image_url_registry import get_registry

    registry = get_registry()
    result = await execute_multi_kb_search(
        **kwargs, _structured=True, _registry=registry
    )
    res = _ensure_structured_result(result)
    registered_images = []
    for token, entry in registry.iter_entries():
        registered_images.append(
            {
                "token": token,
                "url": entry.url,
                "caption": entry.caption,
            }
        )
    res["registered_images"] = registered_images
    return res


