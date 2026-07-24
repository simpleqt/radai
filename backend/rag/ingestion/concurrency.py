"""Shared concurrency controls for self-ingestion model calls."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import uuid
import weakref
from dataclasses import dataclass
from typing import Any

from backend.config.settings import settings
from backend.core.agents.resource_lease import Lease, RedisResourceLeaseManager

logger = logging.getLogger(__name__)


_SEM_LOCK = threading.Lock()
_LOOP_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, tuple[int, DistributedSemaphore]],
] = weakref.WeakKeyDictionary()


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(parsed, 1)


def _limit(env_name: str, setting_name: str, default: int) -> int:
    raw = os.getenv(env_name)
    if raw is not None:
        return _positive_int(raw, default)
    return _positive_int(getattr(settings, setting_name, None), default)


def _positive_float_env(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class _AcquiredCapacity:
    lease: Lease | None
    renew_task: asyncio.Task[None] | None


class DistributedSemaphore:
    """Local bulkhead plus a Redis lease enforcing one deployment-wide limit."""

    def __init__(self, name: str, limit: int) -> None:
        self._name = name
        self._limit = limit
        self._local = asyncio.Semaphore(limit)
        self._states: dict[asyncio.Task[Any], list[_AcquiredCapacity]] = {}
        self._lease_manager: RedisResourceLeaseManager | None = None

    async def acquire(self) -> bool:
        await self._local.acquire()
        current = asyncio.current_task()
        if current is None:
            self._local.release()
            raise RuntimeError("distributed semaphore requires an asyncio task")

        lease: Lease | None = None
        renew_task: asyncio.Task[None] | None = None
        try:
            if _bool_env("SELF_INGESTION_DISTRIBUTED_CONCURRENCY", True):
                if self._lease_manager is None:
                    from backend.db.redis import get_redis

                    self._lease_manager = RedisResourceLeaseManager(
                        get_redis(),
                        namespace=os.getenv(
                            "SELF_INGESTION_CONCURRENCY_NAMESPACE",
                            "self-ingestion-concurrency",
                        ),
                        poll_interval_seconds=_positive_float_env(
                            "SELF_INGESTION_CONCURRENCY_POLL_SECONDS",
                            0.1,
                        ),
                    )
                ttl = _positive_float_env(
                    "SELF_INGESTION_CONCURRENCY_LEASE_TTL_SECONDS",
                    120.0,
                )
                owner_id = (
                    f"{socket.gethostname()}:{os.getpid()}:{id(current)}:"
                    f"{uuid.uuid4().hex}"
                )
                lease = await self._lease_manager.acquire(
                    self._name,
                    owner_id=owner_id,
                    capacity=self._limit,
                    ttl_seconds=ttl,
                    timeout_seconds=_positive_float_env(
                        "SELF_INGESTION_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS",
                        300.0,
                    ),
                )
                renew_task = asyncio.create_task(
                    self._renew(lease, ttl=ttl, owner_task=current),
                    name=f"renew-ingestion-capacity:{self._name}",
                )
            self._states.setdefault(current, []).append(
                _AcquiredCapacity(lease=lease, renew_task=renew_task)
            )
            return True
        except BaseException:
            if lease is not None:
                try:
                    await lease.release()
                except Exception:
                    logger.warning(
                        "release failed after distributed capacity acquisition error",
                        exc_info=True,
                    )
            self._local.release()
            raise

    async def _renew(
        self,
        lease: Lease,
        *,
        ttl: float,
        owner_task: asyncio.Task[Any],
    ) -> None:
        try:
            while True:
                await asyncio.sleep(max(1.0, ttl / 3.0))
                await lease.renew(ttl_seconds=ttl)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "distributed ingestion capacity lease was lost: %s",
                self._name,
            )
            # Continuing after the lease expires would violate the provider's
            # deployment-wide concurrency ceiling.
            owner_task.cancel()

    async def release(self) -> None:
        current = asyncio.current_task()
        stack = self._states.get(current) if current is not None else None
        if not stack:
            raise ValueError("distributed semaphore released without acquisition")
        state = stack.pop()
        if not stack and current is not None:
            self._states.pop(current, None)
        if state.renew_task is not None:
            state.renew_task.cancel()
            await asyncio.gather(state.renew_task, return_exceptions=True)
        if state.lease is not None:
            try:
                await state.lease.release()
            except Exception:
                logger.warning(
                    "failed to release distributed ingestion capacity: %s",
                    self._name,
                    exc_info=True,
                )
        self._local.release()

    async def __aenter__(self) -> DistributedSemaphore:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        await self.release()


def ingest_file_concurrency() -> int:
    return _limit("INGEST_WORKER_MAX_CONCURRENT_FILES", "max_concurrent_files", 5)


def llm_batch_concurrency() -> int:
    return _limit("SELF_INGESTION_LLM_MAX_CONCURRENCY", "llm_model_max_async", 5)


def generation_model_concurrency() -> int:
    """Deployment-wide ceiling shared by text, vision, and summary generation."""
    return _limit(
        "SELF_INGESTION_GENERATION_MAX_CONCURRENCY",
        "llm_model_max_async",
        llm_batch_concurrency(),
    )


def vision_model_concurrency() -> int:
    return _limit("SELF_INGESTION_VISION_MAX_CONCURRENCY", "max_concurrent_vision_processing", 8)


def embedding_concurrency() -> int:
    return _limit("SELF_INGESTION_EMBEDDING_MAX_CONCURRENCY", "embedding_max_concurrency", 4)


def media_embedding_concurrency() -> int:
    """Concurrency ceiling for the independent visual embedding endpoint."""
    return _limit(
        "SELF_INGESTION_MEDIA_EMBEDDING_MAX_CONCURRENCY",
        "embedding_max_concurrency",
        embedding_concurrency(),
    )


def mineru_concurrency() -> int:
    return _limit(
        "SELF_INGESTION_MINERU_MAX_CONCURRENCY",
        "self_ingestion_mineru_max_concurrency",
        2,
    )


def commit_mysql_concurrency() -> int:
    return _limit(
        "SELF_INGESTION_COMMIT_MYSQL_MAX_CONCURRENCY",
        "self_ingestion_commit_mysql_max_concurrency",
        1,
    )


async def semaphore(name: str, limit: int) -> DistributedSemaphore:
    """Return one stable process/loop + deployment-wide named semaphore.

    Replacing an in-use semaphore creates two independent concurrency domains
    and can exceed the intended provider limit.  Runtime limit changes therefore
    take effect after process restart; adaptive callers should shrink their own
    work window while continuing to share this global bulkhead.
    """
    limit = max(int(limit or 1), 1)
    loop = asyncio.get_running_loop()
    with _SEM_LOCK:
        loop_semaphores = _LOOP_SEMAPHORES.setdefault(loop, {})
        current = loop_semaphores.get(name)
        if current is None:
            current = (limit, DistributedSemaphore(name, limit))
            loop_semaphores[name] = current
        return current[1]
