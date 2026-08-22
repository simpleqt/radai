"""SQLite checkpointer WAL 读写连接分离回归。

全应用此前共享一条 aiosqlite 连接,所有会话的 checkpoint 读写在其内部
单线程队列上串行;读写分连接后互不排队(WAL 读不阻塞写)。
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.application.checkpointing.checkpointer import (
    CheckpointerConnections,
    ReadRoutedAsyncSqliteSaver,
    init_sqlite_checkpointer,
)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _metadata() -> dict:
    return {"source": "input", "step": 0, "parents": {}}


@pytest.mark.asyncio
async def test_pooled_saver_routes_reads_to_reader_connections(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_CHECKPOINT_SQLITE_READ_CONNECTIONS", "2")
    saver, connections, _db_path = await init_sqlite_checkpointer(
        "routing.db", data_dir=tmp_path
    )
    try:
        assert isinstance(saver, ReadRoutedAsyncSqliteSaver)
        assert isinstance(connections, CheckpointerConnections)
        assert len(saver._readers) == 2

        reader_get_calls: list[int] = []
        reader_list_calls: list[int] = []
        for index, reader in enumerate(saver._readers):
            original_get = reader.aget_tuple
            original_list = reader.alist

            def make_get(orig, idx):
                async def wrapped(config):
                    reader_get_calls.append(idx)
                    return await orig(config)

                return wrapped

            def make_list(orig, idx):
                def wrapped(config, **kwargs):
                    reader_list_calls.append(idx)
                    return orig(config, **kwargs)

                return wrapped

            monkeypatch.setattr(reader, "aget_tuple", make_get(original_get, index))
            monkeypatch.setattr(reader, "alist", make_list(original_list, index))

        saved_config = await saver.aput(
            _config("thread-route"), empty_checkpoint(), _metadata(), {}
        )
        loaded = await saver.aget_tuple(saved_config)
        assert loaded is not None
        # 写经写连接落库后,读连接(独立 WAL 快照)必须立刻可见
        assert (
            loaded.checkpoint["id"]
            == saved_config["configurable"]["checkpoint_id"]
        )
        listed = [item async for item in saver.alist(_config("thread-route"))]
        assert len(listed) == 1

        assert reader_get_calls, "aget_tuple 必须路由到读连接"
        assert reader_list_calls, "alist 必须路由到读连接"

        # 轮转:读调用(get/list 合计)必须覆盖全部读连接
        await saver.aget_tuple(saved_config)
        assert set(reader_get_calls) | set(reader_list_calls) == {0, 1}
    finally:
        await connections.close()


@pytest.mark.asyncio
async def test_zero_readers_restores_legacy_single_connection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_CHECKPOINT_SQLITE_READ_CONNECTIONS", "0")
    saver, conn, _db_path = await init_sqlite_checkpointer(
        "legacy.db", data_dir=tmp_path
    )
    try:
        assert type(saver) is AsyncSqliteSaver
        assert not isinstance(conn, CheckpointerConnections)

        saved_config = await saver.aput(
            _config("thread-legacy"), empty_checkpoint(), _metadata(), {}
        )
        assert await saver.aget_tuple(saved_config) is not None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_connections_handle_closes_writer_and_readers(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_CHECKPOINT_SQLITE_READ_CONNECTIONS", "2")
    saver, connections, _db_path = await init_sqlite_checkpointer(
        "closing.db", data_dir=tmp_path
    )
    saved_config = await saver.aput(
        _config("thread-close"), empty_checkpoint(), _metadata(), {}
    )
    assert await saver.aget_tuple(saved_config) is not None

    await connections.close()

    with pytest.raises(Exception):
        await saver.aput(
            _config("thread-close"), empty_checkpoint(), _metadata(), {}
        )
    with pytest.raises(Exception):
        await saver.aget_tuple(saved_config)


@pytest.mark.asyncio
async def test_concurrent_reads_and_writes_stay_consistent(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CHECKPOINT_SQLITE_READ_CONNECTIONS", "2")
    saver, connections, _db_path = await init_sqlite_checkpointer(
        "concurrent.db", data_dir=tmp_path
    )
    try:

        async def write_then_read(thread_index: int) -> None:
            config = _config(f"thread-{thread_index}")
            saved = await saver.aput(config, empty_checkpoint(), _metadata(), {})
            loaded = await saver.aget_tuple(saved)
            assert loaded is not None
            assert (
                loaded.checkpoint["id"]
                == saved["configurable"]["checkpoint_id"]
            )

        await asyncio.gather(*(write_then_read(index) for index in range(12)))

        # 每个线程各有一条最新 checkpoint
        for index in range(12):
            listed = [
                item async for item in saver.alist(_config(f"thread-{index}"))
            ]
            assert len(listed) == 1
    finally:
        await connections.close()
