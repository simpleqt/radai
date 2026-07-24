"""HTTP proxy from the lightweight agent API to the knowledge service."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from backend.config.loggers import get_logger

logger = get_logger(__name__)

router = APIRouter()

_PROXY_METHODS = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "DELETE", "OPTIONS"})
_KNOWLEDGE_ROUTE_PREFIXES = (
    "knowledge_bases",
    "uploads",
    "documents",
    "keywords",
    "ingestion",
    "admin/self-ingestion",
    "health/self-ingestion",
    "rag",
)
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def knowledge_service_base_url() -> str:
    """Resolve the dedicated service used by agent-role API processes."""
    return (
        os.getenv("KNOWLEDGE_SERVICE_BASE_URL", "").strip()
        or os.getenv("RAG_SERVICE_BASE_URL", "").strip()
        or "http://127.0.0.1:8086"
    ).rstrip("/")


def _proxy_timeout() -> httpx.Timeout:
    try:
        timeout_seconds = max(
            float(os.getenv("KNOWLEDGE_PROXY_TIMEOUT_SECONDS", "3600") or "3600"),
            1.0,
        )
    except ValueError:
        timeout_seconds = 3600.0
    return httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0))


def _new_upstream_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        trust_env=False,
        timeout=_proxy_timeout(),
        follow_redirects=False,
    )


def _forward_request_headers(request: Request) -> list[tuple[str, str]]:
    excluded = _HOP_BY_HOP_HEADERS | {
        "host",
        "content-length",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
    }
    headers = [
        (raw_name.decode("latin-1"), raw_value.decode("latin-1"))
        for raw_name, raw_value in request.headers.raw
        if raw_name.decode("latin-1").lower() not in excluded
    ]

    if request.client and request.client.host:
        forwarded_for = request.headers.get("x-forwarded-for", "").strip()
        headers.append(
            (
                "x-forwarded-for",
                f"{forwarded_for}, {request.client.host}"
                if forwarded_for
                else request.client.host,
            )
        )
    headers.append(("x-forwarded-proto", request.url.scheme))
    headers.append(("x-forwarded-host", request.headers.get("host", "")))
    return headers


def _forward_response_headers(response: httpx.Response) -> dict[str, str]:
    # The gateway response layer supplies its own transport metadata.
    excluded = _HOP_BY_HOP_HEADERS | {"date", "server"}
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in excluded
    }


async def _close_upstream(
    response: httpx.Response,
    client: httpx.AsyncClient,
) -> None:
    await response.aclose()
    await client.aclose()


async def _stream_request_body(request: Request) -> AsyncIterator[bytes]:
    async for chunk in request.stream():
        yield chunk


def _upstream_content(request: Request) -> AsyncIterator[bytes] | None:
    """Avoid reading ASGI receive for methods that never carry a body."""
    if request.method.upper() in _BODYLESS_METHODS:
        return None
    return _stream_request_body(request)


async def proxy_knowledge_request(request: Request):
    """Forward a whitelisted knowledge API request without buffering its body."""
    client = _new_upstream_client()
    upstream_url = f"{knowledge_service_base_url()}{request.url.path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    upstream_request = client.build_request(
        method=request.method,
        url=upstream_url,
        headers=_forward_request_headers(request),
        content=_upstream_content(request),
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except ClientDisconnect:
        await client.aclose()
        logger.info(
            "客户端断开连接，取消知识库代理: method=%s path=%s",
            request.method,
            request.url.path,
        )
        return Response(status_code=499)
    except httpx.RequestError as exc:
        await client.aclose()
        logger.warning(
            "知识库服务代理失败: method=%s path=%s error=%s",
            request.method,
            request.url.path,
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=502,
            content={"detail": "知识库服务暂时不可用"},
        )

    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=_forward_response_headers(upstream_response),
        background=BackgroundTask(_close_upstream, upstream_response, client),
    )


async def proxy_nested_knowledge_request(
    request: Request,
    _proxy_path: str,
):
    return await proxy_knowledge_request(request)


for _prefix in _KNOWLEDGE_ROUTE_PREFIXES:
    router.add_api_route(
        f"/{_prefix}",
        proxy_knowledge_request,
        methods=_PROXY_METHODS,
        include_in_schema=False,
    )
    router.add_api_route(
        f"/{_prefix}/{{_proxy_path:path}}",
        proxy_nested_knowledge_request,
        methods=_PROXY_METHODS,
        include_in_schema=False,
    )


__all__ = ["knowledge_service_base_url", "proxy_knowledge_request", "router"]
