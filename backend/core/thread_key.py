"""Thread key utilities — build/parse internal thread identifiers."""

from __future__ import annotations

import re

_SAFE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")
_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_INTERNAL_THREAD_KEY_RE = re.compile(
    r"^u_(?P<user_id>[A-Za-z0-9_\-]+)__t_(?P<thread_id>[A-Za-z0-9._\-]+)$"
)
_LEGACY_INTERNAL_THREAD_KEY_RE = re.compile(
    r"^u_(?P<user_id>[A-Za-z0-9_\-]+)#t_(?P<thread_id>[A-Za-z0-9._\-]+)$"
)


def _validate_thread_id(thread_id: str) -> str:
    if not _SAFE_THREAD_ID_RE.match(thread_id):
        raise ValueError(
            f"Invalid thread_id {thread_id!r}: only alphanumeric, dots, hyphens, underscores allowed."
        )
    return thread_id


def _validate_user_id(user_id: str) -> str:
    if not _SAFE_USER_ID_RE.match(user_id):
        raise ValueError(
            f"Invalid user_id {user_id!r}: only alphanumeric, hyphens, underscores allowed."
        )
    return user_id


def build_internal_thread_key(user_id: str, thread_id: str) -> str:
    return f"u_{_validate_user_id(user_id)}__t_{_validate_thread_id(thread_id)}"


def parse_internal_thread_key(thread_id: str) -> tuple[str | None, str]:
    if match := _INTERNAL_THREAD_KEY_RE.match(thread_id):
        return match.group("user_id"), match.group("thread_id")
    if match := _LEGACY_INTERNAL_THREAD_KEY_RE.match(thread_id):
        return match.group("user_id"), match.group("thread_id")
    return None, _validate_thread_id(thread_id)
