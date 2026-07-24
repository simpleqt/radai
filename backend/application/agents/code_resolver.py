"""Normalize legacy database agent codes to runtime profile codes."""

from __future__ import annotations


class AgentCodeResolver:
    _LEGACY_TO_RUNTIME = {
        "ne_full_feature": "ne",
        "csh_full_feature": "csh",
    }

    @classmethod
    def to_runtime(cls, agent_code: str) -> str:
        normalized = str(agent_code or "").strip().lower()
        return cls._LEGACY_TO_RUNTIME.get(normalized, normalized)

