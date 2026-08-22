"""Regression tests for MCP-only API keys."""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from backend.api.dependencies.auth import ApiKeyAuth
from backend.application.auth_service import (
    ResolvedAuth,
    api_key_scope_allows_path,
)


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
    )


@pytest.mark.parametrize(
    "path",
    [
        "/mcp/sse",
        "/mcp/sse/",
        "/mcp/sse/messages/",
        "/api/v1/mcp",
        "/api/v1/mcp/config",
        "/api/v1/mcp/tools/call",
    ],
)
def test_mcp_only_scope_allows_only_mcp_path_family(path):
    assert api_key_scope_allows_path("mcp_only", path) is True


@pytest.mark.parametrize(
    "path",
    ["/", "/live", "/api/v1/models", "/api/v1/mcp-admin", "/mcp/sse-other"],
)
def test_mcp_only_scope_rejects_non_mcp_paths(path):
    assert api_key_scope_allows_path("mcp_only", path) is False


def test_unknown_scope_fails_closed():
    assert api_key_scope_allows_path("unexpected", "/mcp/sse/") is False
    assert api_key_scope_allows_path("unexpected", "/api/v1/models") is False


def test_admin_api_key_payload_validates_scope_and_keeps_legacy_default():
    from backend.api.routers.admin import ApiKeyCreateRequest, ApiKeyUpdateRequest

    assert ApiKeyCreateRequest().access_scope == "full"
    assert ApiKeyCreateRequest(access_scope="mcp_only").access_scope == "mcp_only"
    assert ApiKeyUpdateRequest(access_scope="mcp_only").access_scope == "mcp_only"
    assert ApiKeyCreateRequest().mcp_allowed_tools is None
    assert ApiKeyCreateRequest(mcp_allowed_tools=[]).mcp_allowed_tools == []
    with pytest.raises(ValidationError):
        ApiKeyCreateRequest(access_scope="unexpected")


@pytest.mark.asyncio
async def test_route_dependency_rejects_mcp_key_on_regular_api(monkeypatch):
    from backend.api.dependencies import auth as auth_dependencies

    async def resolve(_key: str):
        return ApiKeyAuth(
            api_key="mcp-key",
            user_id="user-1",
            access_scope="mcp_only",
        )

    monkeypatch.setattr(auth_dependencies, "_resolve_api_key_auth", resolve)

    with pytest.raises(HTTPException) as raised:
        await auth_dependencies.verify_api_key(
            request=_request("/api/v1/models"),
            x_api_key="mcp-key",
            authorization=None,
        )

    assert raised.value.status_code == 403
    assert "仅允许访问 MCP" in str(raised.value.detail)


@pytest.mark.asyncio
async def test_route_dependency_accepts_mcp_key_on_mcp_api(monkeypatch):
    from backend.api.dependencies import auth as auth_dependencies

    async def resolve(_key: str):
        return ApiKeyAuth(
            api_key="mcp-key",
            user_id="user-1",
            access_scope="mcp_only",
        )

    monkeypatch.setattr(auth_dependencies, "_resolve_api_key_auth", resolve)

    resolved = await auth_dependencies.verify_api_key(
        request=_request("/api/v1/mcp/tools"),
        x_api_key="mcp-key",
        authorization=None,
    )

    assert resolved.access_scope == "mcp_only"


@pytest.mark.asyncio
async def test_outer_middleware_blocks_mcp_key_even_on_public_regular_path(monkeypatch):
    from backend.api import http_server

    async def resolve_credentials(**_kwargs):
        return ResolvedAuth(
            api_key="mcp-key",
            user_id="user-1",
            source="api_key",
            access_scope="mcp_only",
        )

    monkeypatch.setattr(
        http_server.auth_service,
        "resolve_credentials",
        resolve_credentials,
    )

    transport = httpx.ASGITransport(app=http_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        blocked = await client.get("/live", headers={"X-API-Key": "mcp-key"})
        allowed_prefix = await client.get(
            "/api/v1/mcp/not-a-real-route",
            headers={"X-API-Key": "mcp-key"},
        )

    assert blocked.status_code == 403
    assert blocked.json()["code"] == "api_key_scope_forbidden"
    assert allowed_prefix.status_code != 403
