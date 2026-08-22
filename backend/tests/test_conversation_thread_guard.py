"""Per-conversation-thread mutual exclusion for live agent streams."""

import asyncio

import pytest

from backend.application.conversations import thread_guard
from backend.application.conversations.thread_guard import (
    ConversationThreadBusy,
    acquire_conversation_thread,
    active_conversation_threads,
    conversation_lease_key,
    reset_conversation_thread_guard,
)
from backend.domain.agents.events import AgentEvent


@pytest.fixture(autouse=True)
def _clean_guard_registry():
    reset_conversation_thread_guard()
    yield
    reset_conversation_thread_guard()


def test_second_acquire_on_same_thread_is_rejected():
    lease = acquire_conversation_thread("thread-1")
    assert lease is not None

    with pytest.raises(ConversationThreadBusy) as excinfo:
        acquire_conversation_thread("thread-1")
    assert excinfo.value.thread_id == "thread-1"

    # A different thread is unaffected.
    other = acquire_conversation_thread("thread-2")
    assert other is not None
    assert active_conversation_threads() == {"thread-1", "thread-2"}


def test_release_frees_the_thread_and_is_idempotent():
    lease = acquire_conversation_thread("thread-1")
    lease.release()
    lease.release()

    assert active_conversation_threads() == set()
    assert acquire_conversation_thread("thread-1") is not None


def test_empty_thread_id_never_conflicts():
    assert acquire_conversation_thread(None) is None
    assert acquire_conversation_thread("") is None
    assert acquire_conversation_thread("   ") is None
    assert active_conversation_threads() == set()


def test_stale_lease_is_taken_over_after_timeout(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(thread_guard.time, "monotonic", lambda: clock["now"])
    monkeypatch.setenv("CONVERSATION_THREAD_LEASE_TIMEOUT_SECONDS", "300")

    stale = acquire_conversation_thread("thread-1")
    assert stale is not None

    clock["now"] = 1200.0
    with pytest.raises(ConversationThreadBusy):
        acquire_conversation_thread("thread-1")

    clock["now"] = 1400.0
    successor = acquire_conversation_thread("thread-1")
    assert successor is not None

    # The stale lease releasing later must not free the successor's hold.
    stale.release()
    with pytest.raises(ConversationThreadBusy):
        acquire_conversation_thread("thread-1")
    successor.release()
    assert active_conversation_threads() == set()


def test_renew_keeps_a_live_lease_from_being_taken_over(monkeypatch):
    """A renewed (heartbeating) holder is never preempted by the stale bound."""

    clock = {"now": 1000.0}
    monkeypatch.setattr(thread_guard.time, "monotonic", lambda: clock["now"])
    monkeypatch.setenv("CONVERSATION_THREAD_LEASE_TIMEOUT_SECONDS", "300")

    lease = acquire_conversation_thread("thread-1")
    assert lease is not None

    # Past the original takeover bound, but the holder renewed midway.
    clock["now"] = 1250.0
    assert lease.renew() is True
    clock["now"] = 1400.0
    with pytest.raises(ConversationThreadBusy):
        acquire_conversation_thread("thread-1")

    lease.release()
    assert active_conversation_threads() == set()


def test_renew_fails_after_takeover_and_never_steals_back(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(thread_guard.time, "monotonic", lambda: clock["now"])
    monkeypatch.setenv("CONVERSATION_THREAD_LEASE_TIMEOUT_SECONDS", "300")

    stale = acquire_conversation_thread("thread-1")
    clock["now"] = 1400.0
    successor = acquire_conversation_thread("thread-1")
    assert successor is not None

    # The old holder's heartbeat must observe the loss instead of stealing
    # the registration back from the successor.
    assert stale.renew() is False
    with pytest.raises(ConversationThreadBusy):
        acquire_conversation_thread("thread-1")

    successor.release()
    assert active_conversation_threads() == set()


def test_renew_after_release_is_a_noop():
    lease = acquire_conversation_thread("thread-1")
    lease.release()
    assert lease.renew() is False
    assert active_conversation_threads() == set()


async def test_guarded_stream_releases_lease_on_consumer_break():
    lease = acquire_conversation_thread("thread-1")

    async def _guarded():
        try:
            for index in range(10):
                yield index
        finally:
            lease.release()

    stream = _guarded()
    async for item in stream:
        if item == 1:
            break
    await stream.aclose()

    assert active_conversation_threads() == set()


async def test_background_task_holds_thread_lease_until_terminal_state():
    """Normal-mode and background paths share one mutual-exclusion source."""

    from backend.application.agent_tasks import AgentTaskRegistry

    registry = AgentTaskRegistry()
    release = asyncio.Event()

    async def events():
        await release.wait()
        yield AgentEvent("done", "r", "thread-x", 1, {})

    record = await registry.start(
        user_id="user-1",
        agent_code="ne",
        thread_id="thread-x",
        question="q",
        stream_factory=events,
    )

    # A normal-mode stream by the same user on the same thread is rejected
    # while the background task is live (both derive the user-scoped key).
    with pytest.raises(ConversationThreadBusy):
        acquire_conversation_thread(conversation_lease_key("user-1", "thread-x"))

    # A different user reusing the same external conversation alias runs on
    # an unrelated LangGraph thread and must not be rejected.
    other_user = acquire_conversation_thread(
        conversation_lease_key("user-2", "thread-x")
    )
    assert other_user is not None
    other_user.release()

    release.set()
    await record.runner
    assert record.status == "completed"

    follow_up = acquire_conversation_thread(
        conversation_lease_key("user-1", "thread-x")
    )
    assert follow_up is not None
    follow_up.release()


async def test_background_task_start_rejected_while_normal_stream_owns_thread():
    from backend.application.agent_tasks import AgentTaskRegistry

    registry = AgentTaskRegistry()
    lease = acquire_conversation_thread(conversation_lease_key("user-1", "thread-y"))

    async def events():
        yield AgentEvent("done", "r", "thread-y", 1, {})

    with pytest.raises(ConversationThreadBusy):
        await registry.start(
            user_id="user-1",
            agent_code="ne",
            thread_id="thread-y",
            question="q",
            stream_factory=events,
        )
    # The rejected start must not leak a registry record or a lease entry.
    assert registry._thread_leases == {}
    assert len(registry._records) == 0

    lease.release()
    record = await registry.start(
        user_id="user-1",
        agent_code="ne",
        thread_id="thread-y",
        question="q",
        stream_factory=events,
    )
    await record.runner
    assert record.status == "completed"
    assert active_conversation_threads() == set()
