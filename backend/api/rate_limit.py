"""Shared HTTP admission control for the Agent and Guest API processes.

Extracted from ``http_server`` so the Guest API (:8083) can enforce the same
Redis-backed sliding-window rate limit with an outage-only in-process
fallback. The Guest process previously had no HTTP rate limiting, so an
unauthenticated caller could drive full retrieval + model inference at will.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections import OrderedDict
from dataclasses import dataclass

from backend.application.auth_security import consume_sliding_window_quota
from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.redis import get_redis

logger = get_logger(__name__)


def env_enabled(name: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


@dataclass
class _RateBucket:
    tokens: float
    updated_at: float


class RequestRateLimiter:
    """Redis-backed HTTP admission control with an outage-only local fallback."""

    def __init__(self) -> None:
        self._buckets: OrderedDict[str, _RateBucket] = OrderedDict()
        self._lock = asyncio.Lock()

    async def allow(self, key: str, limit_per_minute: int) -> tuple[bool, int]:
        limit = max(1, limit_per_minute)
        if settings.redis_host and env_enabled(
            "HTTP_RATE_LIMIT_REDIS_ENABLED", default=True
        ):
            client = get_redis()
            quota_key = "http:rate:" + hashlib.sha256(
                key.encode("utf-8")
            ).hexdigest()
            try:
                return await consume_sliding_window_quota(
                    client,
                    quota_key,
                    window_seconds=60,
                    max_requests=limit,
                )
            except Exception as exc:
                logger.warning(
                    "Redis HTTP 限流不可用，回退到进程内限流: %s",
                    type(exc).__name__,
                )
            finally:
                await client.aclose()

        now = time.monotonic()
        refill_per_second = limit / 60.0
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _RateBucket(tokens=float(limit), updated_at=now)
                self._buckets[key] = bucket
            else:
                bucket.tokens = min(
                    float(limit),
                    bucket.tokens + (now - bucket.updated_at) * refill_per_second,
                )
                bucket.updated_at = now
                self._buckets.move_to_end(key)
            allowed = bucket.tokens >= 1.0
            if allowed:
                bucket.tokens -= 1.0
            retry_after = (
                0
                if allowed
                else max(1, int((1.0 - bucket.tokens) / refill_per_second) + 1)
            )
            while len(self._buckets) > 10000:
                self._buckets.popitem(last=False)
            return allowed, retry_after


# Process-wide limiter shared by all middlewares in one API process.
request_rate_limiter = RequestRateLimiter()
