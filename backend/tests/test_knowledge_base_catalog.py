from types import SimpleNamespace

import pytest

from backend.application.knowledge_bases import catalog_service
from backend.application.knowledge_bases.catalog_service import (
    invalidate_knowledge_base_catalog_cache,
    load_knowledge_base_catalog_cached,
)


@pytest.mark.asyncio
async def test_empty_explicit_kb_scope_does_not_query_or_expand(monkeypatch):
    def fail_factory():
        raise AssertionError("database must not be queried for an empty authorized scope")

    monkeypatch.setattr(catalog_service, "get_async_session_factory", fail_factory)

    assert await catalog_service.load_knowledge_base_catalog([]) == []


@pytest.mark.asyncio
async def test_catalog_uses_mysql_rows_and_preserves_requested_order(monkeypatch):
    rows = [
        SimpleNamespace(
            kb_id="kb-b",
            kb_code="code-b",
            kb_name="Knowledge B",
            owner_id=None,
            is_default=False,
            status=SimpleNamespace(value="active"),
        ),
        SimpleNamespace(
            kb_id="kb-a",
            kb_code="code-a",
            kb_name="Knowledge A",
            owner_id="user-1",
            is_default=True,
            status=SimpleNamespace(value="active"),
        ),
    ]

    class Result:
        def all(self):
            return rows

    class Session:
        async def execute(self, _statement):
            return Result()

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    monkeypatch.setattr(
        catalog_service,
        "get_async_session_factory",
        lambda: lambda: SessionContext(),
    )

    entries = await catalog_service.load_knowledge_base_catalog(["kb-a", "kb-b"])

    assert [(entry.kb_id, entry.kb_name) for entry in entries] == [
        ("kb-a", "Knowledge A"),
        ("kb-b", "Knowledge B"),
    ]


@pytest.mark.asyncio
async def test_cached_catalog_reuses_entries_until_invalidation(monkeypatch):
    calls = 0
    entry = catalog_service.KnowledgeBaseCatalogEntry(
        kb_id="kb-a",
        kb_code="code-a",
        kb_name="Knowledge A",
        owner_id=None,
        is_default=True,
        status="active",
    )

    async def fake_load(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [entry]

    monkeypatch.setattr(
        catalog_service,
        "load_knowledge_base_catalog",
        fake_load,
    )
    invalidate_knowledge_base_catalog_cache()

    first = await load_knowledge_base_catalog_cached(["kb-a"])
    second = await load_knowledge_base_catalog_cached(["kb-a"])

    assert calls == 1
    assert first == second == [entry]

    invalidate_knowledge_base_catalog_cache()
    await load_knowledge_base_catalog_cached(["kb-a"])
    assert calls == 2


@pytest.mark.asyncio
async def test_catalog_preserves_retrieval_backend_fields(monkeypatch):
    rows = [
        SimpleNamespace(
            kb_id="kb-a",
            kb_code="code-a",
            kb_name="Knowledge A",
            owner_id=None,
            is_default=True,
            status=SimpleNamespace(value="active"),
            ingestion_backend="self_ingestion",
            retrieval_backend="self_ingestion",
            self_ingestion_enabled=True,
            self_retriever_enabled=True,
        )
    ]

    class Result:
        def all(self):
            return rows

    class Session:
        async def execute(self, _statement):
            return Result()

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    monkeypatch.setattr(
        catalog_service,
        "get_async_session_factory",
        lambda: lambda: SessionContext(),
    )

    entries = await catalog_service.load_knowledge_base_catalog(["kb-a"])
    assert entries[0].retrieval_backend == "self_ingestion"
    assert entries[0].self_retriever_enabled is True
