"""访客 API 服务上的 checkpoint 回退读取。

访客端 (:8083) 使用 guest_checkpoints.db，但登录用户会话元数据与主站共用 MySQL。
主站 (:8082) 产生的消息在 checkpoints.db，仅查 guest 库会导致历史记录为空。
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from fastapi import Request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.application.checkpointing.checkpointer import get_checkpointer_from_request

# "Thread has messages" is monotone while the thread remains listable: the
# checkpoint GC always retains the latest checkpoint, and full checkpoint
# deletion happens only together with the conversation soft-delete that
# removes the thread from the candidate list upstream. A positive result can
# therefore be cached process-locally, which spares the history pagination
# from re-parsing one full checkpoint blob (~90KB in production) per known
# conversation on every page load.
_KNOWN_MESSAGE_THREADS: OrderedDict[str, None] = OrderedDict()
_KNOWN_MESSAGE_THREADS_LOCK = threading.Lock()
_KNOWN_MESSAGE_THREADS_MAX = 50_000


def _cached_message_threads(thread_ids: Iterable[str]) -> set[str]:
    with _KNOWN_MESSAGE_THREADS_LOCK:
        hits: set[str] = set()
        for thread_id in thread_ids:
            if thread_id in _KNOWN_MESSAGE_THREADS:
                _KNOWN_MESSAGE_THREADS.move_to_end(thread_id)
                hits.add(thread_id)
        return hits


def _remember_message_threads(thread_ids: Iterable[str]) -> None:
    with _KNOWN_MESSAGE_THREADS_LOCK:
        for thread_id in thread_ids:
            _KNOWN_MESSAGE_THREADS[thread_id] = None
            _KNOWN_MESSAGE_THREADS.move_to_end(thread_id)
        while len(_KNOWN_MESSAGE_THREADS) > _KNOWN_MESSAGE_THREADS_MAX:
            _KNOWN_MESSAGE_THREADS.popitem(last=False)


def reset_known_message_threads() -> None:
    """Clear the monotone cache (tests and controlled restarts only)."""

    with _KNOWN_MESSAGE_THREADS_LOCK:
        _KNOWN_MESSAGE_THREADS.clear()


def _checkpoint_messages(checkpoint: dict[str, Any] | None) -> list[Any]:
    if not checkpoint:
        return []
    return list((checkpoint.get("channel_values") or {}).get("messages") or [])


def get_fallback_checkpointer(request: Request) -> AsyncSqliteSaver | None:
    return getattr(request.app.state, "main_checkpointer", None)


async def aget_checkpoint_with_fallback(
    request: Request,
    internal_thread_id: str,
) -> dict[str, Any] | None:
    """优先读 guest checkpoint，访客服务上回退到主站 checkpoints.db。"""
    primary = get_checkpointer_from_request(request)
    config = {"configurable": {"thread_id": internal_thread_id}}
    checkpoint = await primary.aget(config)
    if _checkpoint_messages(checkpoint):
        return checkpoint

    fallback = get_fallback_checkpointer(request)
    if fallback is None:
        return checkpoint

    fallback_checkpoint = await fallback.aget(config)
    if _checkpoint_messages(fallback_checkpoint):
        return fallback_checkpoint
    return checkpoint or fallback_checkpoint


async def has_checkpoint_messages(
    request: Request,
    internal_thread_id: str,
) -> bool:
    if _cached_message_threads([internal_thread_id]):
        return True
    checkpoint = await aget_checkpoint_with_fallback(request, internal_thread_id)
    has_messages = len(_checkpoint_messages(checkpoint)) > 0
    if has_messages:
        _remember_message_threads([internal_thread_id])
    return has_messages


async def _bulk_thread_ids_with_messages(
    checkpointer: AsyncSqliteSaver,
    internal_thread_ids: list[str],
) -> set[str]:
    """Return message-bearing threads using one SQLite query.

    ``AsyncSqliteSaver.aget`` acquires the saver lock for every thread. Calling it
    concurrently therefore still serializes dozens of queries. Fetching the latest
    checkpoint for all candidates at once keeps the history endpoint proportional to
    the number of pages rather than the number of conversations.
    """
    if not internal_thread_ids:
        return set()

    setup_lock = getattr(checkpointer, "_bulk_query_setup_lock", None)
    if setup_lock is None:
        setup_lock = asyncio.Lock()
        checkpointer._bulk_query_setup_lock = setup_lock
    if not getattr(checkpointer, "_bulk_query_setup_done", False):
        async with setup_lock:
            if not getattr(checkpointer, "_bulk_query_setup_done", False):
                await checkpointer.setup()
                checkpointer._bulk_query_setup_done = True

    query_template = """
        SELECT checkpoint.thread_id, checkpoint.type, checkpoint.checkpoint
        FROM checkpoints AS checkpoint
        INNER JOIN (
            SELECT thread_id, MAX(checkpoint_id) AS checkpoint_id
            FROM checkpoints
            WHERE checkpoint_ns = '' AND thread_id IN ({})
            GROUP BY thread_id
        ) AS latest
          ON latest.thread_id = checkpoint.thread_id
         AND latest.checkpoint_id = checkpoint.checkpoint_id
        WHERE checkpoint.checkpoint_ns = ''
    """
    visible: set[str] = set()
    for offset in range(0, len(internal_thread_ids), 500):
        batch = internal_thread_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        query = query_template.format(placeholders)
        async with checkpointer.lock, checkpointer.conn.execute(
            query,
            tuple(batch),
        ) as cursor:
            async for thread_id, payload_type, payload in cursor:
                checkpoint = checkpointer.serde.loads_typed((payload_type, payload))
                if _checkpoint_messages(checkpoint):
                    visible.add(str(thread_id))
    return visible


async def get_thread_ids_with_checkpoint_messages(
    request: Request,
    internal_thread_ids: list[str],
) -> set[str]:
    """Bulk variant of :func:`has_checkpoint_messages` with main-DB fallback."""
    if not internal_thread_ids:
        return set()

    visible = _cached_message_threads(internal_thread_ids)
    pending = [
        thread_id
        for thread_id in internal_thread_ids
        if thread_id not in visible
    ]
    if not pending:
        return visible

    primary = get_checkpointer_from_request(request)
    visible.update(await _bulk_thread_ids_with_messages(primary, pending))
    remaining = [thread_id for thread_id in pending if thread_id not in visible]
    fallback = get_fallback_checkpointer(request)
    if fallback is not None and remaining:
        visible.update(await _bulk_thread_ids_with_messages(fallback, remaining))
    _remember_message_threads(
        thread_id for thread_id in pending if thread_id in visible
    )
    return visible
