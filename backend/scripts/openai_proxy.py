"""Optional HTTP proxy for an OpenAI-compatible upstream.

All target settings come from environment variables. This module contains no
deployment credentials and is not enabled by the RADAI production units.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from backend.scripts.openai_proxy_common import (
    build_forward_headers,
    transform_model_request,
)

logger = logging.getLogger(__name__)

TARGET_API_KEY = os.getenv("OPENAI_PROXY_TARGET_API_KEY", "")
TARGET_BASE_URL = os.getenv("OPENAI_PROXY_TARGET_BASE_URL", "")
TARGET_MODEL = os.getenv("OPENAI_PROXY_TARGET_MODEL", "")


class _ProxySession:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=300, connect=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


_proxy_session = _ProxySession()


def _upstream_url(path: str) -> str:
    if not TARGET_BASE_URL:
        raise HTTPException(status_code=503, detail="proxy upstream is not configured")
    return f"{TARGET_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def _require_api_key() -> str:
    if not TARGET_API_KEY:
        raise HTTPException(status_code=503, detail="proxy credential is not configured")
    return TARGET_API_KEY


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await _proxy_session.close()


app = FastAPI(title="OpenAI-compatible HTTP proxy", lifespan=lifespan)


async def forward_request(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes | None = None,
) -> tuple[int, bytes, str]:
    session = await _proxy_session.get()
    try:
        async with session.request(
            method,
            _upstream_url(path),
            headers=build_forward_headers(headers, api_key=_require_api_key()),
            data=body,
        ) as response:
            content_type = response.headers.get("Content-Type", "application/json")
            return response.status, await response.read(), content_type
    except aiohttp.ClientError as exc:
        logger.warning("OpenAI-compatible upstream request failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="upstream request failed") from exc


async def forward_stream_request(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes | None = None,
) -> StreamingResponse:
    """Return a stream whose upstream response stays open until consumption."""

    session = await _proxy_session.get()
    try:
        upstream = await session.request(
            method,
            _upstream_url(path),
            headers=build_forward_headers(
                headers,
                api_key=_require_api_key(),
                accept="text/event-stream",
            ),
            data=body,
        )
    except aiohttp.ClientError as exc:
        logger.warning("OpenAI-compatible upstream stream failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="upstream request failed") from exc

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.content:
                yield chunk
        finally:
            upstream.release()

    return StreamingResponse(
        chunks(),
        status_code=upstream.status,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_openai_api(request: Request, path: str) -> Response:
    body = transform_model_request(
        await request.body(), target_model=TARGET_MODEL, logger=logger
    )
    headers = dict(request.headers)
    if "text/event-stream" in headers.get("accept", "").lower() or (
        body and b'"stream":true' in body.replace(b" ", b"")
    ):
        return await forward_stream_request(request.method, f"/v1/{path}", headers, body)
    status, content, content_type = await forward_request(
        request.method, f"/v1/{path}", headers, body
    )
    return Response(content=content, status_code=status, media_type=content_type)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}
