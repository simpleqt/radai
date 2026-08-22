"""Process-wide async Redis connection pools, isolated per event loop.

``redis.asyncio`` connections bind the transports of the loop that created
them.  A single process here runs more than one loop (the API main loop plus
ephemeral ``asyncio.run`` loops inside blocking tool bridges), so sharing one
pool across loops hands loop-bound connections to foreign loops and fails with
"attached to a different loop".  Each running loop therefore owns its own
pool; loop-less bootstrap/CLI callers keep a legacy fallback pool.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import weakref
from collections.abc import AsyncIterator
from urllib.parse import quote

from redis.asyncio import ConnectionPool, Redis

from backend.config.settings import settings

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_auth_pool: ConnectionPool | None = None
_pools_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, ConnectionPool]" = (
    weakref.WeakKeyDictionary()
)
_pool_lock = threading.Lock()


def _current_running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


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


def _redis_database(value: object, *, setting_name: str) -> int:
    try:
        database = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{setting_name} must be an integer Redis database") from exc
    if not 0 <= database <= 15:
        raise RuntimeError(f"{setting_name} must be between 0 and 15")
    return database


def _default_redis_database() -> int:
    return _redis_database(settings.redis_db, setting_name="REDIS_DB")


def _auth_redis_database() -> int:
    configured = os.getenv("AUTH_REDIS_DB", "").strip()
    if not configured:
        return _default_redis_database()
    return _redis_database(configured, setting_name="AUTH_REDIS_DB")


def _redis_url(database: int | None = None) -> str:
    credentials = (
        f":{quote(str(settings.redis_password), safe='')}@"
        if settings.redis_password
        else ""
    )
    return (
        f"redis://{credentials}{settings.redis_host}:{settings.redis_port}/"
        f"{_default_redis_database() if database is None else database}"
    )


def _create_pool(database: int | None = None) -> ConnectionPool:
    return ConnectionPool.from_url(
        _redis_url(database),
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


def get_redis_pool() -> ConnectionPool:
    """Return the pool owned by the current event loop (or the bootstrap pool).

    Long-lived loops (API/worker main loops) each keep exactly one pool, so
    request paths behave as before.  Callers without a running loop share the
    legacy fallback pool, matching the historical bootstrap behavior.
    """
    loop = _current_running_loop()
    if loop is None:
        global _pool
        if _pool is not None:
            return _pool
        with _pool_lock:
            if _pool is None:
                _pool = _create_pool()
        return _pool
    with _pool_lock:
        # 已关闭循环的池无法靠弱引用回收(池中连接持有对循环的强引用),
        # 主动丢弃引用;其套接字由 GC 兜底关闭。
        for dead in [item for item in _pools_by_loop.keys() if item.is_closed()]:
            _pools_by_loop.pop(dead, None)
        pool = _pools_by_loop.get(loop)
        if pool is None:
            pool = _create_pool()
            _pools_by_loop[loop] = pool
        return pool


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


def get_auth_redis_pool() -> ConnectionPool:
    """Return the auth/session pool, optionally isolated by ``AUTH_REDIS_DB``.

    Auth flows run only on API main loops, so the dedicated auth pool stays a
    process singleton; when no separate auth DB is configured it simply reuses
    the current loop's pool.
    """
    global _auth_pool
    auth_database = _auth_redis_database()
    if auth_database == _default_redis_database():
        return get_redis_pool()
    if _auth_pool is not None:
        return _auth_pool
    with _pool_lock:
        if _auth_pool is None:
            _auth_pool = _create_pool(auth_database)
    return _auth_pool


def get_auth_redis() -> Redis:
    """Return a client for login/session state without moving RAG Redis data."""
    return Redis(
        connection_pool=get_auth_redis_pool(),
        auto_close_connection_pool=False,
    )


async def get_redis_client() -> AsyncIterator[Redis]:
    """FastAPI dependency backed by the auth/session Redis pool."""
    client = get_auth_redis()
    try:
        yield client
    finally:
        await client.aclose()


async def disconnect_redis_pool_for_current_loop() -> None:
    """Disconnect and forget the pool owned by the current event loop.

    Ephemeral loops must call this before dying; otherwise their connections
    survive in a pool no loop can ever drive again.
    """
    loop = _current_running_loop()
    if loop is None:
        return
    with _pool_lock:
        pool = _pools_by_loop.pop(loop, None)
    if pool is not None:
        await pool.disconnect(inuse_connections=True)


async def close_redis_pool() -> None:
    """Disconnect and forget process-owned pools during graceful shutdown."""
    global _auth_pool, _pool
    with _pool_lock:
        auth_pool, _auth_pool = _auth_pool, None
        pool, _pool = _pool, None
        loop_pools = list(_pools_by_loop.values())
        _pools_by_loop.clear()
    seen: list[ConnectionPool] = []
    for candidate in [auth_pool, *loop_pools, pool]:
        if candidate is None or any(candidate is item for item in seen):
            continue
        seen.append(candidate)
        try:
            await candidate.disconnect(inuse_connections=True)
        except Exception:
            # Pools created by already-dead loops cannot be driven from this
            # loop; dropping the reference lets GC reclaim their sockets.
            logger.debug("skipping redis pool owned by a dead loop", exc_info=True)
