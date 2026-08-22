"""Database package with lazy connection helpers.

Importing a schema or migration utility must not load deployment configuration
or establish database state.  Connection helpers remain available through the
same public names and are resolved only when a caller requests one.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "get_engine",
    "get_session_factory",
    "get_async_session",
    "get_async_session_factory",
    "read_only_session_context",
    "transactional_session_context",
    "async_session_context",
    "init_db",
    "close_db",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    connection = import_module("backend.db.connection")
    return getattr(connection, name)
