"""LangGraph streaming timeout and retry configuration."""

from __future__ import annotations

import os


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


STREAM_IDLE_TIMEOUT_SECONDS = _int_env("STREAM_IDLE_TIMEOUT_SECONDS", 180)
MODEL_ROUND_IDLE_TIMEOUT_SECONDS = _int_env("MODEL_ROUND_IDLE_TIMEOUT_SECONDS", 180)
STREAM_HEARTBEAT_INTERVAL_SECONDS = _int_env("STREAM_HEARTBEAT_INTERVAL_SECONDS", 30)
INCOMPLETE_ROUND_STREAM_RETRY_MAX = _int_env("INCOMPLETE_ROUND_STREAM_RETRY_MAX", 2)


def resolve_normal_stream_idle_timeout(*, handler, deep_research_mode: bool) -> float:
    if deep_research_mode:
        raise ValueError("deep_research_mode should use deep-research timeout budgets")
    if handler.has_incomplete_model_round():
        return float(MODEL_ROUND_IDLE_TIMEOUT_SECONDS)
    return float(STREAM_IDLE_TIMEOUT_SECONDS)
