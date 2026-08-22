"""Regression tests for complete knowledge-base database deletion."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.db.crud.knowledge_base_crud import KnowledgeBaseCRUD


@pytest.mark.asyncio
async def test_delete_removes_all_kb_owned_rows_before_parent():
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.execute.return_value.rowcount = 1

    deleted = await KnowledgeBaseCRUD.delete(session, "kb-1")

    table_names = [call.args[0].table.name for call in session.execute.await_args_list]
    assert deleted is True
    assert table_names == [
        "kb_self_chunks",
        "kb_self_ingest_jobs",
        "kb_pending_files",
        "kb_documents",
        "kb_directories",
        "agent_kb_bindings",
        "knowledge_bases",
    ]
    session.commit.assert_awaited_once_with()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_rolls_back_when_a_child_delete_fails():
    session = MagicMock()
    session.execute = AsyncMock(side_effect=RuntimeError("database failure"))
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    with pytest.raises(RuntimeError, match="database failure"):
        await KnowledgeBaseCRUD.delete(session, "kb-1")

    session.rollback.assert_awaited_once_with()
    session.commit.assert_not_awaited()
