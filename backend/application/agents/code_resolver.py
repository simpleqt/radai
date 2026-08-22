"""Normalize legacy database agent codes to runtime profile codes."""

from __future__ import annotations


class AgentCodeResolver:
    _LEGACY_TO_RUNTIME = {
        "ne_full_feature": "ne",
        "csh_full_feature": "csh",
        "fmca_full_feature": "fmca",
    }

    @classmethod
    def to_runtime(cls, agent_code: str) -> str:
        normalized = str(agent_code or "").strip().lower()
        return cls._LEGACY_TO_RUNTIME.get(normalized, normalized)

    @classmethod
    def legacy_alias(cls, runtime_code: str) -> str | None:
        """Return the known legacy DB key for a runtime profile code, if any."""
        normalized = cls.to_runtime(runtime_code)
        for legacy, runtime in cls._LEGACY_TO_RUNTIME.items():
            if runtime == normalized:
                return legacy
        # Convention used by agent_binding_codes() for any profile.
        candidate = f"{normalized}_full_feature"
        return candidate if candidate != normalized else None

    @classmethod
    def binding_codes(cls, agent_code: str) -> tuple[str, ...]:
        """Prefer runtime code, then legacy alias — same order as runtime KB lookup."""
        runtime = cls.to_runtime(agent_code)
        legacy = cls.legacy_alias(runtime)
        if legacy and legacy != runtime:
            return (runtime, legacy)
        return (runtime,)
