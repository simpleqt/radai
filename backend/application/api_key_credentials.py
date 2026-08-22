"""Hashed storage for API keys.

An API key is a long-lived bearer credential, so storing it verbatim makes any
read of ``api_keys`` — a backup, a slow-query log, a read-only account — a full
credential compromise. Keys are looked up by ``SHA-256`` digest instead, and a
short non-secret prefix is kept purely so operators can still recognise a key in
the admin UI.

Keys are 256 bits of ``secrets.token_urlsafe`` output, so a plain digest is
sufficient: there is no low-entropy space for an attacker to enumerate and
therefore no need for a per-row salt (which would prevent lookup) or a pepper
(which would add a rotation hazard for no practical gain).

Hashed storage activates automatically once ``api_keys.api_key_hash`` exists, so
the same code runs before and after the schema migration.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from sqlalchemy import inspect

from backend.config.loggers import get_logger

logger = get_logger(__name__)

API_KEY_PREFIX_LENGTH = 12
API_KEY_HASH_COLUMN = "api_key_hash"

_capability_lock = threading.Lock()
_hashed_storage_available: bool | None = None


def api_key_digest(raw_key: str) -> str:
    """Return the lookup digest for a raw API key."""

    return hashlib.sha256(str(raw_key or "").strip().encode("utf-8")).hexdigest()


def api_key_prefix(raw_key: str) -> str:
    """Return the non-secret display prefix for a raw API key."""

    return str(raw_key or "").strip()[:API_KEY_PREFIX_LENGTH]


def mask_api_key(*, prefix: str | None = None, raw_key: str | None = None) -> str:
    """Render a key for display without exposing a usable credential."""

    visible = str(prefix or "").strip() or api_key_prefix(raw_key or "")
    return f"{visible}..." if visible else ""


def reset_hashed_storage_capability() -> None:
    """Forget the cached schema probe (tests and schema changes)."""

    global _hashed_storage_available
    with _capability_lock:
        _hashed_storage_available = None


def _record_capability(available: bool) -> bool:
    global _hashed_storage_available
    with _capability_lock:
        _hashed_storage_available = available
    return available


async def hashed_storage_available(session: Any) -> bool:
    """Report whether this database stores API keys as digests.

    The probe runs once per process. A failure resolves to ``False`` so an
    unexpected inspection error degrades to the legacy column instead of
    rejecting every authenticated request.
    """

    cached = _hashed_storage_available
    if cached is not None:
        return cached

    def _probe(sync_session: Any) -> bool:
        inspector = inspect(sync_session.bind)
        if "api_keys" not in inspector.get_table_names():
            return False
        columns = {column["name"] for column in inspector.get_columns("api_keys")}
        return API_KEY_HASH_COLUMN in columns

    try:
        available = await session.run_sync(_probe)
    except Exception:
        logger.warning(
            "API Key 哈希列探测失败，本进程回退到明文列查找",
            exc_info=True,
        )
        return _record_capability(False)
    return _record_capability(bool(available))


__all__ = [
    "API_KEY_HASH_COLUMN",
    "API_KEY_PREFIX_LENGTH",
    "api_key_digest",
    "api_key_prefix",
    "hashed_storage_available",
    "mask_api_key",
    "reset_hashed_storage_capability",
]
