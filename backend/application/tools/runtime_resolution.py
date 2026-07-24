"""Common value resolution for tool runtime objects."""

from __future__ import annotations

from typing import Any


def resolve_runtime_thread_id(runtime: Any) -> str | None:
    """Resolve an internal thread ID from runtime context or config."""
    context = getattr(runtime, "context", None)
    thread_id = context.get("thread_id") if context else None
    if thread_id:
        return str(thread_id)

    config = getattr(runtime, "config", None) or {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
    return str(thread_id) if thread_id else None


__all__ = ["resolve_runtime_thread_id"]
