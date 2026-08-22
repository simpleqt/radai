"""LangGraph streaming timeout and retry configuration.

All stream deadlines live in one :class:`StreamTimeoutPolicy` so the runtime
does not need to piece together env vars, class constants and handler state.
The policy can be overridden per agent profile (``stream_timeout_policy`` in
``backend/config/agents/*.yaml``) with env vars as the fallback source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any, Mapping


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return float(default)


def _optional_timeout_env(name: str, default: float | None) -> float | None:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return None if value <= 0 else value


STREAM_IDLE_TIMEOUT_SECONDS = _int_env("STREAM_IDLE_TIMEOUT_SECONDS", 180)
MODEL_ROUND_IDLE_TIMEOUT_SECONDS = _int_env("MODEL_ROUND_IDLE_TIMEOUT_SECONDS", 180)
STREAM_HEARTBEAT_INTERVAL_SECONDS = _int_env("STREAM_HEARTBEAT_INTERVAL_SECONDS", 30)
INCOMPLETE_ROUND_STREAM_RETRY_MAX = _int_env("INCOMPLETE_ROUND_STREAM_RETRY_MAX", 2)
STREAM_TOTAL_TIMEOUT_SECONDS = _optional_timeout_env("STREAM_TOTAL_TIMEOUT_SECONDS", 600.0)


@dataclass(frozen=True)
class StreamTimeoutPolicy:
    """Single source of truth for stream timeouts, retries and recursion limits."""

    stream_idle_timeout_seconds: float = 180.0
    model_round_idle_timeout_seconds: float = 180.0
    heartbeat_interval_seconds: float = 30.0
    incomplete_round_retry_max: int = 2
    normal_stream_total_timeout_seconds: float | None = 600.0
    deep_research_stream_timeout_seconds: float = 1800.0
    deep_research_idle_timeout_seconds: float = 600.0
    deep_research_finalize_idle_timeout_seconds: float = 300.0
    deep_research_finalize_grace_seconds: float = 300.0
    deep_research_task_stall_grace_seconds: float = 300.0
    recursion_limit: int | None = None

    def resolve_normal_idle_timeout(self, has_incomplete_model_round: bool) -> float:
        """Idle budget for normal streams, with the model-round extension."""

        if has_incomplete_model_round:
            return float(self.model_round_idle_timeout_seconds)
        return float(self.stream_idle_timeout_seconds)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "StreamTimeoutPolicy":
        if not mapping:
            return cls()
        allowed = {field.name for field in fields(cls)}
        clean = {
            key: value
            for key, value in mapping.items()
            if key in allowed and value is not None
        }
        return cls(**clean)

    @classmethod
    def from_env(cls) -> "StreamTimeoutPolicy":
        return cls(
            stream_idle_timeout_seconds=float(
                _int_env("STREAM_IDLE_TIMEOUT_SECONDS", 180)
            ),
            model_round_idle_timeout_seconds=float(
                _int_env("MODEL_ROUND_IDLE_TIMEOUT_SECONDS", 180)
            ),
            heartbeat_interval_seconds=float(
                _int_env("STREAM_HEARTBEAT_INTERVAL_SECONDS", 30)
            ),
            incomplete_round_retry_max=_int_env(
                "INCOMPLETE_ROUND_STREAM_RETRY_MAX", 2
            ),
            normal_stream_total_timeout_seconds=_optional_timeout_env(
                "STREAM_TOTAL_TIMEOUT_SECONDS", 600.0
            ),
            deep_research_stream_timeout_seconds=_float_env(
                "DEEP_RESEARCH_STREAM_TIMEOUT_SECONDS", 1800.0
            ),
            deep_research_idle_timeout_seconds=_float_env(
                "DEEP_RESEARCH_IDLE_TIMEOUT_SECONDS", 600.0
            ),
            deep_research_finalize_idle_timeout_seconds=_float_env(
                "DEEP_RESEARCH_FINALIZE_IDLE_TIMEOUT_SECONDS", 300.0
            ),
            deep_research_finalize_grace_seconds=_float_env(
                "DEEP_RESEARCH_FINALIZE_GRACE_SECONDS", 300.0
            ),
            deep_research_task_stall_grace_seconds=_float_env(
                "DEEP_RESEARCH_TASK_STALL_GRACE_SECONDS", 300.0
            ),
            recursion_limit=_optional_int_env("STREAM_RECURSION_LIMIT"),
        )


def _optional_int_env(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return None


def load_stream_timeout_policy(profile: Any = None) -> StreamTimeoutPolicy:
    """Resolve a policy from the agent profile, layered over env defaults."""

    policy = StreamTimeoutPolicy.from_env()
    profile_mapping = getattr(profile, "stream_timeout_policy", None)
    if not profile_mapping:
        return policy
    merged = {
        **{
            field.name: getattr(policy, field.name)
            for field in fields(StreamTimeoutPolicy)
        },
        **dict(profile_mapping),
    }
    return StreamTimeoutPolicy.from_mapping(merged)


