from __future__ import annotations

from types import SimpleNamespace

from backend.application.checkpointing import guest_fallback


class _Checkpointer:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.calls = 0

    async def aget(self, config):
        self.calls += 1
        return self.checkpoint


def _request(fallback=None):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(main_checkpointer=fallback))
    )


async def test_primary_checkpoint_with_messages_skips_fallback(monkeypatch):
    primary = _Checkpointer({"channel_values": {"messages": ["primary"]}})
    fallback = _Checkpointer({"channel_values": {"messages": ["fallback"]}})
    monkeypatch.setattr(
        guest_fallback,
        "get_checkpointer_from_request",
        lambda request: primary,
    )

    checkpoint = await guest_fallback.aget_checkpoint_with_fallback(
        _request(fallback),
        "user:thread",
    )

    assert checkpoint == primary.checkpoint
    assert primary.calls == 1
    assert fallback.calls == 0


async def test_empty_primary_checkpoint_reads_main_fallback(monkeypatch):
    primary = _Checkpointer({"channel_values": {"messages": []}})
    fallback = _Checkpointer({"channel_values": {"messages": ["fallback"]}})
    monkeypatch.setattr(
        guest_fallback,
        "get_checkpointer_from_request",
        lambda request: primary,
    )

    checkpoint = await guest_fallback.aget_checkpoint_with_fallback(
        _request(fallback),
        "user:thread",
    )

    assert checkpoint == fallback.checkpoint
    assert await guest_fallback.has_checkpoint_messages(
        _request(fallback),
        "user:thread",
    )
