"""Per-conversation-thread mutual exclusion for stateful agent streams.

Two concurrent streams on the same LangGraph thread fork the same parent
checkpoint and interleave their writes, so the surviving conversation state
depends on scheduling. The deep-research background path already rejects such
conflicts through the task registry; this guard gives the normal streaming
path the same protection.

The registry is intentionally process-local: the stateful Agent API runs with
one worker by policy (see the project architecture reference). Multi-worker
deployments would need a shared lock, the same documented limitation as
live-run cancellation.

Leases are renewed by a heartbeat (``lease_heartbeat``) while their stream is
alive, so the stale-takeover bound only fires for genuinely dead holders
(e.g. a response torn down before the guarded generator ever started) instead
of preempting a long-running live stream.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_ACTIVE_THREADS: dict[str, float] = {}
_GUARD_LOCK = threading.Lock()


def _lease_timeout_seconds() -> float:
    """Stale-lease takeover bound.

    A lease normally ends with its stream, but if a response is torn down
    before the guarded generator ever starts, the finally block never runs.
    The takeover bound turns that worst case into a bounded wait instead of a
    thread locked until process restart.
    """

    try:
        return max(
            60.0,
            float(os.getenv("CONVERSATION_THREAD_LEASE_TIMEOUT_SECONDS", "3600")),
        )
    except (TypeError, ValueError):
        return 3600.0


class ConversationThreadBusy(RuntimeError):
    """Raised when a conversation thread already has a live request."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(
            f"conversation {thread_id} already has an active request; "
            "wait for it to finish or cancel it first"
        )
        self.thread_id = thread_id


class ConversationThreadLease:
    """Held for the lifetime of one stream; release is idempotent."""

    __slots__ = ("_thread_id", "_acquired_at", "_released")

    def __init__(self, thread_id: str, acquired_at: float) -> None:
        self._thread_id = thread_id
        self._acquired_at = acquired_at
        self._released = False

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def renew(self) -> bool:
        """Refresh the lease timestamp while this holder still owns it.

        Returns False when the lease was released or already taken over by a
        newer holder; the caller must not keep writing conversation state in
        that case (its heartbeat should stop).
        """

        if self._released:
            return False
        with _GUARD_LOCK:
            if _ACTIVE_THREADS.get(self._thread_id) != self._acquired_at:
                return False
            now = time.monotonic()
            _ACTIVE_THREADS[self._thread_id] = now
            self._acquired_at = now
        return True

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with _GUARD_LOCK:
            # Only remove our own registration; a stale-takeover successor may
            # already own a newer timestamp for the same thread.
            if _ACTIVE_THREADS.get(self._thread_id) == self._acquired_at:
                _ACTIVE_THREADS.pop(self._thread_id, None)


def conversation_lease_key(
    user_id: str | None,
    thread_id: str | None,
) -> str | None:
    """Build the user-scoped lease key for one logical conversation thread.

    The actual LangGraph thread is user-scoped (``u_{user}__t_{external}``),
    but leases used to be keyed by the bare external id: two different users
    whose clients reuse the same conversation alias (e.g. fixed MCP session
    names) falsely rejected each other with 409 even though their graph
    threads never touch. Callers without an authenticated user keep the bare
    external id so anonymous flows preserve their existing scoping.
    """

    external = str(thread_id or "").strip()
    if not external:
        return None
    user = str(user_id or "").strip()
    if not user:
        return external
    return f"u_{user}__t_{external}"


def acquire_conversation_thread(
    thread_id: str | None,
) -> ConversationThreadLease | None:
    """Reserve a conversation thread for one live stream.

    Returns ``None`` for empty thread ids: a request without a conversation id
    gets a fresh internal thread and cannot conflict with anything.

    Raises:
        ConversationThreadBusy: when the thread already has an active request
            younger than the stale-lease takeover bound.
    """

    normalized = str(thread_id or "").strip()
    if not normalized:
        return None
    now = time.monotonic()
    with _GUARD_LOCK:
        held_since = _ACTIVE_THREADS.get(normalized)
        if held_since is not None:
            if now - held_since < _lease_timeout_seconds():
                raise ConversationThreadBusy(normalized)
            logger.warning(
                "[ConversationThreadGuard] taking over stale lease: thread=%s "
                "held_for=%.0fs",
                normalized,
                now - held_since,
            )
        _ACTIVE_THREADS[normalized] = now
    return ConversationThreadLease(normalized, now)


def lease_heartbeat_interval_seconds() -> float:
    """Renew period: a fraction of the takeover bound, bounded to [30, 600]s."""

    return max(30.0, min(600.0, _lease_timeout_seconds() / 4.0))


async def lease_heartbeat(lease: ConversationThreadLease | None) -> None:
    """Renew a lease periodically until cancelled or ownership is lost.

    Run as a background task next to the guarded stream. Without renewal a
    live stream longer than the takeover bound would be silently preempted by
    a second request and both would interleave writes on one thread.
    """

    if lease is None:
        return
    interval = lease_heartbeat_interval_seconds()
    try:
        while True:
            await asyncio.sleep(interval)
            if not lease.renew():
                logger.warning(
                    "[ConversationThreadGuard] lease lost during stream: thread=%s",
                    lease.thread_id,
                )
                return
    except asyncio.CancelledError:
        raise


def start_lease_heartbeat(
    lease: ConversationThreadLease | None,
) -> asyncio.Task | None:
    """Spawn the heartbeat task for a held lease (None-safe convenience)."""

    if lease is None:
        return None
    return asyncio.create_task(
        lease_heartbeat(lease),
        name=f"conversation-lease-heartbeat:{lease.thread_id}",
    )


def stop_lease_heartbeat(task: asyncio.Task | None) -> None:
    """Cancel a heartbeat task started by :func:`start_lease_heartbeat`."""

    if task is not None and not task.done():
        task.cancel()


def active_conversation_threads() -> set[str]:
    """Snapshot of currently reserved threads (diagnostics and tests)."""

    with _GUARD_LOCK:
        return set(_ACTIVE_THREADS)


def reset_conversation_thread_guard() -> None:
    """Clear all leases (tests and controlled restarts only)."""

    with _GUARD_LOCK:
        _ACTIVE_THREADS.clear()
