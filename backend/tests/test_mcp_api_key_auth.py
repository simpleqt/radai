"""MCP transport authentication regressions."""

from __future__ import annotations

import anyio
import httpx
import pytest
from starlette.responses import JSONResponse

from backend.application.auth_service import ResolvedAuth
from backend.application.mcp.fastmcp_app import (
    _McpPostMessageApp,
    McpApiKeyAuthMiddleware,
    create_mcp_sse_app,
)


async def _ok_app(scope, receive, send):
    await JSONResponse({"ok": True})(scope, receive, send)


def _http_scope(path: str = "/messages/") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def _capture_send(messages):
    async def send(message):
        messages.append(message)

    return send


@pytest.mark.asyncio
async def test_mcp_transport_rejects_missing_api_key():
    transport = httpx.ASGITransport(app=McpApiKeyAuthMiddleware(_ok_app))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 401
    assert response.json()["code"] == "missing_api_key"


@pytest.mark.asyncio
async def test_mcp_sse_app_factory_installs_api_key_authentication():
    transport = httpx.ASGITransport(app=create_mcp_sse_app())
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 401
    assert response.json()["code"] == "missing_api_key"


@pytest.mark.asyncio
async def test_mcp_transport_rejects_invalid_api_key(monkeypatch):
    from backend.application.mcp import fastmcp_app

    async def invalid(_api_key):
        return None

    monkeypatch.setattr(fastmcp_app.auth_service, "resolve_api_key", invalid)
    transport = httpx.ASGITransport(app=McpApiKeyAuthMiddleware(_ok_app))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/",
            headers={"Authorization": "Bearer invalid-key"},
        )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_api_key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource_error",
    [anyio.ClosedResourceError, anyio.BrokenResourceError],
)
async def test_mcp_post_message_returns_gone_for_closed_sse_session(resource_error):
    async def stale_session_app(scope, receive, send):
        await JSONResponse({"accepted": True}, status_code=202)(scope, receive, send)
        raise resource_error

    sent = []
    app = _McpPostMessageApp(stale_session_app)
    await app(_http_scope(), _empty_receive, _capture_send(sent))

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 410
    assert sent[1]["body"] == b"MCP session is closed"
    assert all(message.get("status") != 202 for message in sent)


@pytest.mark.asyncio
async def test_mcp_post_message_flushes_accepted_response_after_success():
    sent = []
    app = _McpPostMessageApp(_ok_app)
    await app(_http_scope(), _empty_receive, _capture_send(sent))

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b'{"ok":true}'


@pytest.mark.asyncio
async def test_mcp_transport_rejects_unknown_api_key_scope(monkeypatch):
    from backend.application.mcp import fastmcp_app

    async def invalid_scope(api_key):
        return ResolvedAuth(
            api_key=api_key,
            user_id="mcp-user",
            role="user",
            source="api_key",
            access_scope="unexpected",
        )

    monkeypatch.setattr(fastmcp_app.auth_service, "resolve_api_key", invalid_scope)
    transport = httpx.ASGITransport(app=McpApiKeyAuthMiddleware(_ok_app))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/",
            headers={"Authorization": "Bearer scoped-key"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "api_key_scope_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "headers"),
    [
        ("GET", {"Authorization": "Bearer valid-key"}),
        ("POST", {"X-API-Key": "valid-key"}),
    ],
)
async def test_mcp_transport_accepts_api_key_for_get_and_post(
    monkeypatch,
    method,
    headers,
):
    from backend.application.mcp import fastmcp_app

    async def valid(api_key):
        return ResolvedAuth(
            api_key=api_key,
            user_id="mcp-user",
            role="user",
            source="api_key",
        )

    monkeypatch.setattr(fastmcp_app.auth_service, "resolve_api_key", valid)
    transport = httpx.ASGITransport(app=McpApiKeyAuthMiddleware(_ok_app))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.request(method, "/", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_mcp_transport_does_not_accept_cached_login_session(monkeypatch):
    from backend.application.mcp import fastmcp_app

    async def invalid(_api_key):
        return None

    async def inject_login_session(scope, receive, send):
        scope.setdefault("state", {})["auth"] = ResolvedAuth(
            api_key="",
            user_id="session-user",
            source="session_token",
        )
        await McpApiKeyAuthMiddleware(_ok_app)(scope, receive, send)

    monkeypatch.setattr(fastmcp_app.auth_service, "resolve_api_key", invalid)
    transport = httpx.ASGITransport(app=inject_login_session)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/",
            headers={"Authorization": "Bearer login-session-token"},
        )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_api_key"
