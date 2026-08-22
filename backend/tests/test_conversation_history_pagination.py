from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.api.routers import guest_conversations
from backend.api.routers.conversations import list_conversations
from backend.db.migrate import MIGRATIONS
from backend.db.models import Conversation


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_conversation_sidebar_query_uses_lightweight_paginated_columns():
    now = datetime(2026, 7, 21, 10, 0, 0)
    session = _Session(
        [
            SimpleNamespace(
                id="conversation-1",
                thread_id="thread-1",
                title="第一页",
                created_at=now,
                updated_at=now,
            )
        ]
    )

    response = await list_conversations(
        limit=20,
        offset=40,
        include_metadata=False,
        user=SimpleNamespace(user_id="user-1"),
        session=session,
    )

    assert response[0].thread_id == "thread-1"
    assert response[0].metadata is None
    assert response[0].pinned is False
    selected_column_keys = [column.key for column in session.statement.selected_columns]
    # 除概要列外仅追加轻量 JSON_EXTRACT 置顶投影，metadata 全量 JSON 仍不加载。
    assert selected_column_keys == [
        "id",
        "thread_id",
        "title",
        "created_at",
        "updated_at",
        "pinned",
    ]
    assert session.statement._limit_clause.value == 20
    assert session.statement._offset_clause.value == 40


def test_conversation_history_query_has_matching_composite_index_and_migration():
    history_index = next(
        index
        for index in Conversation.__table__.indexes
        if index.name == "idx_conversation_user_history"
    )
    assert [column.name for column in history_index.columns] == [
        "user_id",
        "is_deleted",
        "updated_at",
        "id",
    ]
    assert any(
        migration.version == "202607210001_add_conversation_history_index"
        for migration in MIGRATIONS
    )


@pytest.mark.asyncio
async def test_guest_history_offset_counts_only_visible_conversations(monkeypatch):
    now = datetime(2026, 7, 21, 10, 0, 0)
    candidates = [
        SimpleNamespace(
            id=f"conversation-{index}",
            thread_id=f"thread-{index}",
            title=f"会话 {index}",
            created_at=now,
            updated_at=now,
        )
        for index in range(5)
    ]

    class _PagedSession:
        statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            raw_offset = statement._offset_clause.value
            raw_limit = statement._limit_clause.value
            return _Result(candidates[raw_offset : raw_offset + raw_limit])

    async def _visible(_request, internal_thread_ids):
        return {
            internal_thread_id
            for internal_thread_id in internal_thread_ids
            if any(
                internal_thread_id.endswith(f"__t_{thread_id}")
                for thread_id in ("thread-1", "thread-3", "thread-4")
            )
        }

    monkeypatch.setattr(
        guest_conversations,
        "get_thread_ids_with_checkpoint_messages",
        _visible,
    )
    session = _PagedSession()
    response = await guest_conversations.list_guest_conversations(
        request=SimpleNamespace(),
        limit=2,
        offset=1,
        include_metadata=False,
        user=SimpleNamespace(user_id="user-1"),
        session=session,
    )

    assert [item.thread_id for item in response] == ["thread-3", "thread-4"]
    assert [column.key for column in session.statements[0].selected_columns] == [
        "id",
        "thread_id",
        "title",
        "created_at",
        "updated_at",
    ]
