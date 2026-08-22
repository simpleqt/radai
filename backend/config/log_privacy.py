"""Privacy-preserving summaries for user-controlled text in logs."""

from __future__ import annotations

import hashlib
from typing import Any


def text_fingerprint(value: Any) -> str:
    """Return useful correlation metadata without returning the source text."""
    if isinstance(value, bytes):
        payload = value
        character_count = len(value.decode("utf-8", errors="replace"))
    else:
        rendered = "" if value is None else str(value)
        payload = rendered.encode("utf-8", errors="replace")
        character_count = len(rendered)
    digest = hashlib.sha256(payload).hexdigest()[:12]
    return f"chars={character_count},sha256={digest}"
