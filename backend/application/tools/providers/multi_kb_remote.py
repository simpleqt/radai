"""多库检索:RAG 远程 HTTP 客户端、并发信号量与远程结果图片重映射。(拆自 multi_kb_search_tool.py)"""

from __future__ import annotations

import logging

import asyncio
import httpx
import os
import re
import threading
import weakref

from backend.application.tools.providers.multi_kb_types import FilterMode
from typing import Any


logger = logging.getLogger(__name__)

try:
    _MULTI_KB_MAX_ACTIVE_KB_SEARCHES = max(
        1,
        int(os.getenv("MULTI_KB_MAX_ACTIVE_KB_SEARCHES", "16") or "16"),
    )
except (TypeError, ValueError):
    _MULTI_KB_MAX_ACTIVE_KB_SEARCHES = 16


_RAG_HTTP_CLIENTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


_RAG_HTTP_CLIENTS_LOCK = threading.Lock()


_KB_SEARCH_SEMAPHORES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


_KB_SEARCH_SEMAPHORES_LOCK = threading.Lock()


def _get_kb_search_semaphore() -> asyncio.Semaphore:
    """Process-wide cap on simultaneously running single-KB searches."""
    loop = asyncio.get_running_loop()
    with _KB_SEARCH_SEMAPHORES_LOCK:
        semaphore = _KB_SEARCH_SEMAPHORES.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(_MULTI_KB_MAX_ACTIVE_KB_SEARCHES)
            _KB_SEARCH_SEMAPHORES[loop] = semaphore
        return semaphore


def _get_rag_http_client(base_url: str, timeout: float) -> httpx.AsyncClient:
    """Return one keep-alive client per event loop for internal KB retrieval."""
    loop = asyncio.get_running_loop()
    key = (base_url, timeout)
    with _RAG_HTTP_CLIENTS_LOCK:
        cached = _RAG_HTTP_CLIENTS.get(loop)
        if cached is not None:
            cached_key, client = cached
            if cached_key == key and not client.is_closed:
                return client
            if not client.is_closed:
                loop.create_task(client.aclose())
        client = httpx.AsyncClient(
            trust_env=False,
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=32,
                max_keepalive_connections=32,
                keepalive_expiry=300.0,
            ),
        )
        _RAG_HTTP_CLIENTS[loop] = (key, client)
        return client


async def close_rag_http_clients() -> None:
    """Close loop-local RAG service HTTP clients during API shutdown."""
    with _RAG_HTTP_CLIENTS_LOCK:
        clients = [client for _key, client in _RAG_HTTP_CLIENTS.values()]
        _RAG_HTTP_CLIENTS.clear()
    await asyncio.gather(
        *(client.aclose() for client in clients if not client.is_closed),
        return_exceptions=True,
    )


def _rag_service_base_url() -> str:
    """Resolve the omnigraph knowledge service endpoint.

    RAG_SERVICE_BASE_URL 保持兼容;未设置时回落到 KNOWLEDGE_SERVICE_BASE_URL
    (与 knowledge_proxy 相同的默认值 http://127.0.0.1:8086)。
    """
    return (
        os.getenv("RAG_SERVICE_BASE_URL", "").strip()
        or os.getenv("KNOWLEDGE_SERVICE_BASE_URL", "").strip()
        or "http://127.0.0.1:8086"
    ).rstrip("/")


async def _execute_remote_multi_kb_search(
    *,
    base_url: str,
    query: str,
    kb_ids: list[str],
    top_k: int,
    chunk_top_k: int,
    merge_strategy: str,
    external_hl_keywords: list[str] | None,
    external_ll_keywords: list[str] | None,
    external_query_type: str | None,
    content_filter_mode: FilterMode | None,
    max_result_chars: int | None,
    include_graph: bool = True,
    deep_retrieval: bool = False,
    _registry: Any = None,
) -> dict[str, Any]:
    """Delegate retrieval to the long-lived knowledge service."""
    from backend.config.settings import settings

    secret = str(settings.internal_api_secret or "").strip()
    if not secret:
        raise RuntimeError(
            "RAG_SERVICE_BASE_URL requires INTERNAL_API_SECRET to protect internal retrieval"
        )
    timeout_seconds = max(
        1.0,
        float(os.getenv("RAG_SERVICE_REQUEST_TIMEOUT_SECONDS", "150") or "150"),
    )
    payload = {
        "query": query,
        "kb_ids": kb_ids,
        "top_k": top_k,
        "chunk_top_k": chunk_top_k,
        "merge_strategy": merge_strategy,
        "external_hl_keywords": external_hl_keywords,
        "external_ll_keywords": external_ll_keywords,
        "external_query_type": external_query_type,
        "content_filter_mode": (
            content_filter_mode.value
            if isinstance(content_filter_mode, FilterMode)
            else None
        ),
        "max_result_chars": max_result_chars,
        "include_graph": include_graph,
        "deep_retrieval": deep_retrieval,
    }
    try:
        client = _get_rag_http_client(base_url, timeout_seconds)
        response = await client.post(
            f"{base_url}/api/v1/rag/internal/multi-kb-search",
            json=payload,
            headers={"X-Internal-Secret": secret},
        )
        response.raise_for_status()
        result = response.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"知识库服务请求失败: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("知识库服务返回了无效响应")

    _remap_remote_registered_images(result, _registry=_registry)

    return result


def _remap_remote_registered_images(
    result: dict[str, Any],
    *,
    _registry: Any = None,
) -> dict[str, Any]:
    """Map knowledge-service image tokens into the request-local registry.

    Every internal HTTP retrieval owns a fresh registry, so separate Agentic
    RAG rounds may both return ``[IMAGE:001]`` for different URLs.  Blindly
    inserting those entries overwrites the first image.  Registering by URL in
    the agent request and rewriting all returned textual fields gives every
    image a stable, collision-free token across retrieval rounds.
    """

    registered_images = result.get("registered_images") or []
    if not isinstance(registered_images, list) or not registered_images:
        return result
    if _registry is None:
        from backend.application.agent_executors.image_url_registry import get_registry

        _registry = get_registry()

    token_mapping: dict[str, str] = {}
    local_images: list[dict[str, str]] = []
    seen_local_tokens: set[str] = set()
    for image in registered_images:
        if not isinstance(image, dict):
            continue
        remote_token = str(image.get("token") or "").strip()
        url = str(image.get("url") or "").strip()
        caption = str(image.get("caption") or "").strip()
        if not remote_token or not url:
            continue
        local_token = str(_registry.register(url, caption) or "").strip()
        if not local_token:
            continue
        token_mapping[remote_token] = local_token
        if local_token in seen_local_tokens:
            continue
        seen_local_tokens.add(local_token)
        local_images.append({"token": local_token, "url": url, "caption": caption})

    if token_mapping:
        token_pattern = re.compile(
            "|".join(
                re.escape(token)
                for token in sorted(token_mapping, key=len, reverse=True)
            )
        )

        def replace_tokens(value: Any) -> Any:
            if isinstance(value, str):
                return token_pattern.sub(
                    lambda match: token_mapping.get(match.group(0), match.group(0)),
                    value,
                )
            if isinstance(value, list):
                return [replace_tokens(item) for item in value]
            if isinstance(value, dict):
                return {
                    key: replace_tokens(item)
                    for key, item in value.items()
                    if key != "registered_images"
                }
            return value

        remapped = replace_tokens(result)
        if isinstance(remapped, dict):
            result.clear()
            result.update(remapped)
    result["registered_images"] = local_images
    logger.info(
        "[_execute_remote_multi_kb_search] remapped %s image token(s) into registry id=%s",
        len(token_mapping),
        id(_registry),
    )
    return result
