"""会话置顶契约：SQLite 目录排序/幂等与 PATCH 路由行为。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.routers.conversations import (
    RenameRequest,
    list_conversations,
    rename_conversation,
)
from backend.application.conversations.local_catalog import (
    get_local_conversation_catalog,
)


def configure_local_catalog(monkeypatch, tmp_path: Path) -> Path:
    catalog_path = tmp_path / "data" / "conversations" / "conversations.db"
    monkeypatch.setenv("DOC_AGENT_TEST_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_CONVERSATION_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_CONVERSATION_SQLITE_PATH", str(catalog_path))
    return catalog_path


async def _seed_conversations(catalog, count: int = 3) -> None:
    for index in range(count):
        await catalog.rename(
            user_id="user-1",
            thread_id=f"thread-{index}",
            title=f"会话 {index}",
        )


async def test_set_pinned_orders_pinned_first_and_preserves_recency(
    monkeypatch, tmp_path
):
    configure_local_catalog(monkeypatch, tmp_path)
    catalog = get_local_conversation_catalog()
    assert catalog is not None
    await _seed_conversations(catalog)

    # 未置顶时按 updated_at 倒序（后建的在前）。
    listed = await catalog.list_for_user(user_id="user-1", limit=10, offset=0)
    assert [item.thread_id for item in listed][0] == "thread-2"

    # 置顶最旧的会话后它排到最前，且 updated_at 不被改写。
    before = await catalog.get(user_id="user-1", thread_id="thread-0")
    assert await catalog.set_pinned(
        user_id="user-1", thread_id="thread-0", pinned=True
    )
    after = await catalog.get(user_id="user-1", thread_id="thread-0")
    assert after.metadata_ and after.metadata_.get("pinned_at")
    assert after.updated_at == before.updated_at

    listed = await catalog.list_for_user(user_id="user-1", limit=10, offset=0)
    assert [item.thread_id for item in listed] == [
        "thread-0",
        "thread-2",
        "thread-1",
    ]

    # 取消置顶后恢复纯 recency 排序。
    assert await catalog.set_pinned(
        user_id="user-1", thread_id="thread-0", pinned=False
    )
    restored = await catalog.get(user_id="user-1", thread_id="thread-0")
    assert not (restored.metadata_ or {}).get("pinned_at")
    listed = await catalog.list_for_user(user_id="user-1", limit=10, offset=0)
    assert [item.thread_id for item in listed] == [
        "thread-2",
        "thread-1",
        "thread-0",
    ]


async def test_set_pinned_missing_conversation_returns_false(monkeypatch, tmp_path):
    configure_local_catalog(monkeypatch, tmp_path)
    catalog = get_local_conversation_catalog()
    assert not await catalog.set_pinned(
        user_id="user-1", thread_id="missing", pinned=True
    )


async def test_pinned_ordering_survives_malformed_metadata(monkeypatch, tmp_path):
    """历史坏 metadata 行不能让 json_extract 让整个列表查询失败。"""
    configure_local_catalog(monkeypatch, tmp_path)
    catalog = get_local_conversation_catalog()
    await _seed_conversations(catalog, count=2)

    import aiosqlite

    async with aiosqlite.connect(str(catalog.path)) as connection:
        await connection.execute(
            "UPDATE conversations SET metadata = 'not-json' WHERE thread_id = 'thread-0'"
        )
        await connection.commit()

    listed = await catalog.list_for_user(user_id="user-1", limit=10, offset=0)
    assert {item.thread_id for item in listed} == {"thread-0", "thread-1"}


async def test_patch_route_pins_and_lists_pinned_flag(monkeypatch, tmp_path):
    configure_local_catalog(monkeypatch, tmp_path)
    catalog = get_local_conversation_catalog()
    await _seed_conversations(catalog, count=2)

    user = SimpleNamespace(user_id="user-1")
    result = await rename_conversation(
        thread_id="thread-0",
        request=RenameRequest(pinned=True),
        user=user,
        session=None,
    )
    assert result == {"success": True}

    listed = await list_conversations(
        limit=10,
        offset=0,
        include_metadata=False,
        user=user,
        session=None,
    )
    assert listed[0].thread_id == "thread-0"
    assert listed[0].pinned is True
    assert listed[0].metadata is None
    assert listed[1].pinned is False


async def test_patch_route_requires_title_or_pinned(monkeypatch, tmp_path):
    configure_local_catalog(monkeypatch, tmp_path)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await rename_conversation(
            thread_id="thread-0",
            request=RenameRequest(),
            user=SimpleNamespace(user_id="user-1"),
            session=None,
        )
    assert exc_info.value.status_code == 422


async def test_patch_route_pin_missing_conversation_404(monkeypatch, tmp_path):
    configure_local_catalog(monkeypatch, tmp_path)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await rename_conversation(
            thread_id="missing",
            request=RenameRequest(pinned=True),
            user=SimpleNamespace(user_id="user-1"),
            session=None,
        )
    assert exc_info.value.status_code == 404
