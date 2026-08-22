"""Regression tests for API error and database transaction boundaries."""

from __future__ import annotations

from contextlib import asynccontextmanager

import anyio
import httpx
import pytest

from backend.application.auth_service import auth_service
from backend.application.exceptions import AuthenticationUnavailable


@pytest.mark.asyncio
async def test_authentication_backend_failure_is_not_downgraded_to_anonymous(
    monkeypatch,
):
    from backend.api import http_server

    async def unavailable(**_kwargs):
        raise AuthenticationUnavailable()

    monkeypatch.setattr(
        http_server.auth_service,
        "resolve_credentials",
        unavailable,
    )

    transport = httpx.ASGITransport(app=http_server.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/live",
            headers={"Authorization": "Bearer existing-session"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "认证服务暂时不可用",
        "code": "authentication_unavailable",
    }


@pytest.mark.asyncio
async def test_api_key_resolution_keeps_only_scalar_values_after_session_exit(
    monkeypatch,
):
    from backend.db import connection

    class Result:
        @staticmethod
        def first():
            return (
                True,
                None,
                "user-1",
                "mcp_only",
                ["query_nuclear_electronics_text"],
                "admin",
                True,
            )

    class Session:
        async def execute(self, _statement):
            return Result()

    @asynccontextmanager
    async def read_only_session():
        yield Session()

    monkeypatch.setattr(connection, "read_only_session_context", read_only_session)

    resolved = await auth_service.resolve_api_key("valid-key")

    assert resolved is not None
    assert resolved.api_key == "valid-key"
    assert resolved.user_id == "user-1"
    assert resolved.role == "admin"
    assert resolved.source == "api_key"
    assert resolved.access_scope == "mcp_only"
    assert resolved.mcp_allowed_tools == ("query_nuclear_electronics_text",)


@pytest.mark.asyncio
async def test_read_only_session_rolls_back_without_committing(monkeypatch):
    from backend.db import connection

    events: list[str] = []

    class Session:
        def __init__(self):
            self.info = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            events.append("closed")

        async def rollback(self):
            events.append("rollback")

        async def commit(self):
            events.append("commit")

    monkeypatch.setattr(connection, "get_async_session_factory", lambda: Session)

    async with connection.read_only_session_context():
        events.append("used")

    assert events == ["used", "rollback", "closed"]


@pytest.mark.asyncio
async def test_read_only_session_cleanup_survives_request_cancellation(monkeypatch):
    from backend.db import connection

    events: list[str] = []

    class Session:
        def __init__(self):
            self.info = {}

        async def __aenter__(self):
            events.append("entered")
            return self

        async def __aexit__(self, *_exc_info):
            await anyio.sleep(0)
            events.append("closed")

        async def rollback(self):
            await anyio.sleep(0)
            events.append("rollback")

    monkeypatch.setattr(connection, "get_async_session_factory", lambda: Session)

    with anyio.CancelScope() as cancel_scope:
        async with connection.read_only_session_context():
            events.append("used")
            cancel_scope.cancel()
            await anyio.sleep(0)

    assert events == ["entered", "used", "rollback", "closed"]
