from __future__ import annotations

import weakref

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import ClientDisconnect

from backend.api.routers import knowledge_proxy


def _proxy_app() -> FastAPI:
    app = FastAPI()
    app.include_router(knowledge_proxy.router, prefix="/api/v1")
    return app


def _fresh_client_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """按循环的客户端缓存在共享事件循环的测试会话间必须隔离。"""
    monkeypatch.setattr(
        knowledge_proxy,
        "_UPSTREAM_CLIENTS",
        weakref.WeakKeyDictionary(),
    )


@pytest.mark.asyncio
async def test_proxy_forwards_method_query_authentication_and_body(monkeypatch):
    observed: dict[str, object] = {}

    async def upstream(request: httpx.Request) -> httpx.Response:
        observed["method"] = request.method
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers.get("authorization")
        observed["request_id"] = request.headers.get("x-request-id")
        observed["body"] = await request.aread()
        return httpx.Response(
            202,
            headers={"x-upstream": "knowledge", "content-type": "application/json"},
            stream=httpx.ByteStream(b'{"accepted":true}'),
        )

    transport = httpx.MockTransport(upstream)
    _fresh_client_cache(monkeypatch)
    monkeypatch.setattr(
        knowledge_proxy,
        "_new_upstream_client",
        lambda: httpx.AsyncClient(transport=transport),
    )
    monkeypatch.setenv("KNOWLEDGE_SERVICE_BASE_URL", "http://knowledge:8086")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_proxy_app()),
        base_url="http://agent:8082",
    ) as client:
        response = await client.patch(
            "/api/v1/knowledge_bases/kb-1?refresh=true",
            headers={
                "Authorization": "Bearer session-token",
                "X-Request-ID": "req-123",
                "Content-Type": "application/json",
            },
            content=b'{"kb_name":"updated"}',
        )

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert response.headers["x-upstream"] == "knowledge"
    assert observed == {
        "method": "PATCH",
        "url": "http://knowledge:8086/api/v1/knowledge_bases/kb-1?refresh=true",
        "authorization": "Bearer session-token",
        "request_id": "req-123",
        "body": b'{"kb_name":"updated"}',
    }


@pytest.mark.asyncio
async def test_proxy_returns_502_when_knowledge_service_is_unavailable(monkeypatch):
    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(unavailable)
    _fresh_client_cache(monkeypatch)
    monkeypatch.setattr(
        knowledge_proxy,
        "_new_upstream_client",
        lambda: httpx.AsyncClient(transport=transport),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_proxy_app()),
        base_url="http://agent:8082",
    ) as client:
        response = await client.get("/api/v1/knowledge_bases")

    assert response.status_code == 502
    assert response.json() == {"detail": "知识库服务暂时不可用"}


@pytest.mark.asyncio
async def test_proxy_returns_499_when_client_disconnects(monkeypatch):
    class _DisconnectingClient(httpx.AsyncClient):
        async def send(self, request, **kwargs):  # noqa: ANN001
            raise ClientDisconnect()

    _fresh_client_cache(monkeypatch)
    monkeypatch.setattr(
        knowledge_proxy,
        "_new_upstream_client",
        lambda: _DisconnectingClient(),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_proxy_app()),
        base_url="http://agent:8082",
    ) as client:
        response = await client.get("/api/v1/knowledge_bases/kb-1/documents")

    assert response.status_code == 499


def test_upstream_content_skips_body_for_get_like_methods():
    class _Req:
        def __init__(self, method: str):
            self.method = method

        def stream(self):  # pragma: no cover - should not be called for GET
            raise AssertionError("bodyless methods must not stream request body")

    assert knowledge_proxy._upstream_content(_Req("GET")) is None
    assert knowledge_proxy._upstream_content(_Req("HEAD")) is None
    assert knowledge_proxy._upstream_content(_Req("DELETE")) is None
    assert knowledge_proxy._upstream_content(_Req("OPTIONS")) is None
    assert knowledge_proxy._upstream_content(_Req("POST")) is not None


@pytest.mark.asyncio
async def test_proxy_does_not_capture_non_knowledge_api_routes():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_proxy_app()),
        base_url="http://agent:8082",
    ) as client:
        response = await client.get("/api/v1/models")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_proxy_reuses_upstream_client_across_requests(monkeypatch):
    constructed: list[httpx.AsyncClient] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(b'{"ok":true}'),
        )

    transport = httpx.MockTransport(upstream)

    def factory() -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=transport)
        constructed.append(client)
        return client

    _fresh_client_cache(monkeypatch)
    monkeypatch.setattr(knowledge_proxy, "_new_upstream_client", factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_proxy_app()),
        base_url="http://agent:8082",
    ) as client:
        first = await client.get("/api/v1/knowledge_bases")
        second = await client.get("/api/v1/documents/doc-1")

    assert first.status_code == 200
    assert second.status_code == 200
    # 同一事件循环内的多次代理请求必须复用同一个 keep-alive 上游客户端,
    # 而不是每请求新建再销毁(TCP 建连/对象构造是高频转发路由的纯开销)。
    assert len(constructed) == 1

    await knowledge_proxy.close_knowledge_proxy_clients()
    assert constructed[0].is_closed


@pytest.mark.asyncio
async def test_proxy_error_keeps_shared_client_open(monkeypatch):
    constructed: list[httpx.AsyncClient] = []

    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(unavailable)

    def factory() -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=transport)
        constructed.append(client)
        return client

    _fresh_client_cache(monkeypatch)
    monkeypatch.setattr(knowledge_proxy, "_new_upstream_client", factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_proxy_app()),
        base_url="http://agent:8082",
    ) as client:
        response = await client.get("/api/v1/knowledge_bases")
        retry = await client.get("/api/v1/knowledge_bases")

    # 上游瞬断只结束本次转发;共享客户端保持打开供下次请求复用。
    assert response.status_code == 502
    assert retry.status_code == 502
    assert len(constructed) == 1
    assert not constructed[0].is_closed

    await knowledge_proxy.close_knowledge_proxy_clients()


def test_upstream_limits_defaults_and_env_override(monkeypatch):
    for name in (
        "KNOWLEDGE_PROXY_MAX_CONNECTIONS",
        "KNOWLEDGE_PROXY_MAX_KEEPALIVE_CONNECTIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    limits = knowledge_proxy._upstream_limits()
    assert limits.max_connections == 128
    assert limits.max_keepalive_connections == 32
    assert limits.keepalive_expiry == 300.0

    monkeypatch.setenv("KNOWLEDGE_PROXY_MAX_CONNECTIONS", "9")
    monkeypatch.setenv("KNOWLEDGE_PROXY_MAX_KEEPALIVE_CONNECTIONS", "3")
    limits = knowledge_proxy._upstream_limits()
    assert limits.max_connections == 9
    assert limits.max_keepalive_connections == 3

    monkeypatch.setenv("KNOWLEDGE_PROXY_MAX_CONNECTIONS", "0")
    assert knowledge_proxy._upstream_limits().max_connections == 128
