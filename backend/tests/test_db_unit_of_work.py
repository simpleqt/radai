from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.db import connection
from backend.db.unit_of_work import (
    flush_or_commit,
    mark_outer_transaction_managed,
)


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}
        self.events: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        self.events.append("closed")

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def flush(self) -> None:
        self.events.append("flush")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "transaction_event"),
    [("GET", "rollback"), ("HEAD", "rollback"), ("POST", "commit")],
)
async def test_request_session_uses_http_method_transaction_boundary(
    monkeypatch,
    method: str,
    transaction_event: str,
) -> None:
    session = _Session()
    monkeypatch.setattr(connection, "get_async_session_factory", lambda: lambda: session)

    request = SimpleNamespace(method=method)
    async with asynccontextmanager(connection.get_async_session)(request) as owned:
        assert owned is session
        assert session.info["outer_transaction_managed"] is True

    assert session.events == [transaction_event, "closed"]


@pytest.mark.asyncio
async def test_crud_commit_is_deferred_to_outer_unit_of_work() -> None:
    owned = _Session()
    mark_outer_transaction_managed(owned)
    await flush_or_commit(owned)
    assert owned.events == ["flush"]

    legacy = _Session()
    await flush_or_commit(legacy)
    assert legacy.events == ["commit"]


@pytest.mark.asyncio
async def test_read_only_context_cannot_be_committed_by_legacy_crud(
    monkeypatch,
) -> None:
    session = _Session()
    monkeypatch.setattr(connection, "get_async_session_factory", lambda: lambda: session)

    async with connection.read_only_session_context() as owned:
        await flush_or_commit(owned)

    assert session.events == ["flush", "rollback", "closed"]
