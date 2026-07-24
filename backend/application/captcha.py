"""Shared CAPTCHA challenge generation."""

from __future__ import annotations

import secrets


def build_captcha_text() -> tuple[str, str]:
    """Return a small arithmetic challenge and its expected answer."""
    left = secrets.randbelow(8) + 2
    right = secrets.randbelow(8) + 2
    if secrets.randbelow(2) == 0:
        return f"{left} + {right} = ?", str(left + right)
    bigger = max(left, right)
    smaller = min(left, right)
    return f"{bigger} - {smaller} = ?", str(bigger - smaller)


__all__ = ["build_captcha_text"]
