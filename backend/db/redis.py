"""Process-wide async Redis connection pool."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import AsyncIterator
from urllib.parse import quote

from redis.asyncio import ConnectionPool, Redis

from backend.config.settings import settings

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _positive_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _redis_url() -> str:
    credentials = (
        f":{quote(str(settings.redis_password), safe='')}@"
        if settings.redis_password
        else ""
    )
    return (
        f"redis://{credentials}{settings.redis_host}:{settings.redis_port}/"
        f"{settings.redis_db}"
    )


def get_redis_pool() -> ConnectionPool:
    """Return the process-owned pool, creating it lazily once."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool.from_url(
                _redis_url(),
                decode_responses=True,
                max_connections=_positive_int("REDIS_MAX_CONNECTIONS", 200),
                socket_connect_timeout=_positive_float(
                    "REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS", 2.0
                ),
                socket_timeout=_positive_float("REDIS_SOCKET_TIMEOUT_SECONDS", 5.0),
                health_check_interval=_positive_int(
                    "REDIS_HEALTH_CHECK_INTERVAL_SECONDS", 30
                ),
                retry_on_timeout=True,
            )
    return _pool


def get_redis() -> Redis:
    """Return a lightweight client backed by the shared process pool.

    Callers may continue to call ``aclose()`` on this wrapper; because the pool
    is externally owned, doing so returns any checked-out connection without
    destroying the shared pool.
    """
    return Redis(
        connection_pool=get_redis_pool(),
        auto_close_connection_pool=False,
    )


async def get_redis_client() -> AsyncIterator[Redis]:
    """FastAPI dependency backed by the process-wide pool."""
    client = get_redis()
    try:
        yield client
    finally:
        await client.aclose()


async def close_redis_pool() -> None:
    """Disconnect and forget the process-owned pool during graceful shutdown."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        await pool.disconnect(inuse_connections=True)
