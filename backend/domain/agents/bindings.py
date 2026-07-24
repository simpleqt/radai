"""Agent identity helpers used by persistence adapters."""

from __future__ import annotations


def agent_binding_codes(profile_code: str) -> tuple[str, ...]:
    """Return current and legacy DB keys without encoding any KB identifiers."""
    code = str(profile_code or "").strip()
    if not code:
        return ()
    legacy = f"{code}_full_feature"
    return (code, legacy) if legacy != code else (code,)
