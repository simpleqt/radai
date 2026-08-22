"""Internal HTTP client for the standalone omnigraph knowledge service.

All agent-side KB access goes through this client (management operations are
proxied by ``api/routers/knowledge_proxy.py``; retrieval/tool calls use the
``X-Internal-Secret`` protected endpoints here).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from backend.config.loggers import get_logger

logger = get_logger(__name__)

_INTERNAL_TIMEOUT_SECONDS = float(os.getenv("KNOWLEDGE_INTERNAL_TIMEOUT_SECONDS", "120") or 120)


def knowledge_service_base_url() -> str:
    return (
        os.getenv("KNOWLEDGE_SERVICE_BASE_URL", "").strip()
        or os.getenv("RAG_SERVICE_BASE_URL", "").strip()
        or "http://127.0.0.1:8086"
    ).rstrip("/")


def _internal_secret() -> str:
    from backend.config.settings import settings

    return str(getattr(settings, "internal_api_secret", "") or "").strip()


def _rag_service_url() -> str:
    # 与 multi_kb_remote 相同的解析顺序,保证工具与内部客户端同源。
    return (
        os.getenv("RAG_SERVICE_BASE_URL", "").strip()
        or knowledge_service_base_url()
    ).rstrip("/")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        trust_env=False,
        timeout=httpx.Timeout(_INTERNAL_TIMEOUT_SECONDS, connect=10.0),
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=32),
    )


async def internal_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to an internal omnigraph endpoint with the shared secret."""
    base_url = _rag_service_url()
    secret = _internal_secret()
    if not secret:
        raise RuntimeError(
            "INTERNAL_API_SECRET 未配置，无法调用知识服务内部接口"
        )
    async with _client() as client:
        response = await client.post(
            f"{base_url}{path}",
            json=payload,
            headers={"X-Internal-Secret": secret},
        )
        response.raise_for_status()
        return response.json()


class KnowledgeInternalError(RuntimeError):
    """Raised when the omnigraph internal API returns an error payload."""


async def internal_retrieve(
    *,
    query: str,
    kb_id: str,
    top_k: int = 30,
    chunk_top_k: int = 10,
    hl_keywords: list[str] | None = None,
    ll_keywords: list[str] | None = None,
    query_type: str = "general",
    disable_stage1_oversample: bool = True,
    seed_weights: dict[str, float] | None = None,
    retrieval_profile_override: str | None = None,
) -> dict[str, Any]:
    """Call omnigraph ``POST /api/v1/rag/retrieve_internal``.

    Returns the raw RagRetrieveResponse dict:
    ``{context, images, entity_count, relation_count, chunk_count,
    knowledge_paths, chunks}``.
    """
    payload: dict[str, Any] = {
        "query": query,
        "kb_id": kb_id,
        "top_k": top_k,
        "chunk_top_k": chunk_top_k,
        "query_type": query_type,
        "disable_stage1_oversample": disable_stage1_oversample,
    }
    if hl_keywords is not None:
        payload["hl_keywords"] = hl_keywords
    if ll_keywords is not None:
        payload["ll_keywords"] = ll_keywords
    if seed_weights is not None:
        payload["seed_weights"] = seed_weights
    if retrieval_profile_override is not None:
        payload["retrieval_profile_override"] = retrieval_profile_override
    try:
        return await internal_post("/api/v1/rag/retrieve_internal", payload)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300]
        raise KnowledgeInternalError(
            f"知识服务内部检索失败: status={exc.response.status_code} detail={detail}"
        ) from exc
    except httpx.HTTPError as exc:
        raise KnowledgeInternalError(
            f"知识服务不可达: {type(exc).__name__}"
        ) from exc


async def internal_image_search(
    *,
    query: str,
    kb_ids: list[str] | None,
    top_k: int = 8,
) -> dict[str, Any]:
    """Call omnigraph ``POST /api/v1/rag/internal/image-search``."""
    return await internal_post(
        "/api/v1/rag/internal/image-search",
        {
            "query": query,
            "kb_ids": kb_ids or [],
            "top_k": top_k,
        },
    )


async def internal_staging_delete_prefix(
    prefix: str,
    *,
    bucket: str | None = None,
) -> int:
    """Call omnigraph ``POST /api/v1/rag/internal/staging/delete-prefix``."""
    payload: dict[str, Any] = {"prefix": prefix}
    if bucket:
        payload["bucket"] = bucket
    result = await internal_post(
        "/api/v1/rag/internal/staging/delete-prefix", payload
    )
    return int(result.get("removed_objects") or 0)
