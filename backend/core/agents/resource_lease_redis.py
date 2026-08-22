"""资源租约 Redis 实现:围栏令牌 Lua 脚本(acquire/renew/release/清理)与管理器。(拆自 resource_lease.py)"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from backend.core.agents.resource_lease_model import (
    AsyncRedisEvalClient,
    Lease,
    LeaseIdentity,
    ResourceLeaseAcquireTimeout,
    ResourceLeaseBackendError,
    ResourceLeaseCapacityMismatch,
    ResourceLeaseExpiredError,
    ResourceLeaseOwnershipError,
    ResourceLeaseReleasedError,
    _LeaseHandle,
    _decode_eval_result,
    _normalize_text,
    _seconds_to_ms,
    _validate_capacity,
    _validate_identity,
    _validate_timeout,
    _validate_ttl,
)


_REDIS_LEASE_CLEANUP_LUA = r"""
local function lease_now_ms()
    local parts = redis.call('TIME')
    return tonumber(parts[1]) * 1000 + math.floor(tonumber(parts[2]) / 1000)
end

local function cleanup_leases(now_ms, tombstone_ttl_ms)
    local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now_ms)
    for _, lease_id in ipairs(expired) do
        local owner = redis.call('HGET', KEYS[2], lease_id)
        local token = redis.call('HGET', KEYS[3], lease_id)
        if owner and token then
            redis.call('HSET', KEYS[6], lease_id, owner)
            redis.call('HSET', KEYS[7], lease_id, token)
            redis.call('HSET', KEYS[8], lease_id, 'expired')
            redis.call('ZADD', KEYS[9], now_ms + tombstone_ttl_ms, lease_id)
        end
        redis.call('ZREM', KEYS[1], lease_id)
        redis.call('HDEL', KEYS[2], lease_id)
        redis.call('HDEL', KEYS[3], lease_id)
    end

    local forgotten = redis.call('ZRANGEBYSCORE', KEYS[9], '-inf', now_ms)
    for _, lease_id in ipairs(forgotten) do
        redis.call('ZREM', KEYS[9], lease_id)
        redis.call('HDEL', KEYS[6], lease_id)
        redis.call('HDEL', KEYS[7], lease_id)
        redis.call('HDEL', KEYS[8], lease_id)
    end

    -- Capacity is configuration for the currently active lease set, not part
    -- of the fencing history.  Once no lease is active it is safe to accept a
    -- new capacity while retaining KEYS[4]'s monotonic fencing counter.
    if redis.call('ZCARD', KEYS[1]) == 0 then
        redis.call('DEL', KEYS[5])
    end
end
"""


REDIS_FENCED_LEASE_ACQUIRE_SCRIPT = (
    _REDIS_LEASE_CLEANUP_LUA
    + r"""
local ttl_ms = tonumber(ARGV[1])
local tombstone_ttl_ms = tonumber(ARGV[2])
local requested_capacity = tonumber(ARGV[3])
local lease_id = ARGV[4]
local owner_id = ARGV[5]
local now_ms = lease_now_ms()
cleanup_leases(now_ms, tombstone_ttl_ms)

local configured_capacity = redis.call('GET', KEYS[5])
if configured_capacity and tonumber(configured_capacity) ~= requested_capacity then
    return {-4, tonumber(configured_capacity), now_ms}
end
if not configured_capacity then
    redis.call('SET', KEYS[5], requested_capacity)
end

if redis.call('HEXISTS', KEYS[2], lease_id) == 1
    or redis.call('HEXISTS', KEYS[8], lease_id) == 1 then
    return {-5, now_ms}
end

if redis.call('ZCARD', KEYS[1]) >= requested_capacity then
    local earliest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    local next_expiry = 0
    if earliest[2] then
        next_expiry = tonumber(earliest[2])
    end
    return {0, next_expiry, now_ms}
end

local fencing_token = redis.call('INCR', KEYS[4])
local expires_at = now_ms + ttl_ms
redis.call('HSET', KEYS[2], lease_id, owner_id)
redis.call('HSET', KEYS[3], lease_id, fencing_token)
redis.call('ZADD', KEYS[1], expires_at, lease_id)
return {1, fencing_token, expires_at, now_ms}
"""
)


REDIS_FENCED_LEASE_RENEW_SCRIPT = (
    _REDIS_LEASE_CLEANUP_LUA
    + r"""
local ttl_ms = tonumber(ARGV[1])
local tombstone_ttl_ms = tonumber(ARGV[2])
local lease_id = ARGV[3]
local owner_id = ARGV[4]
local fencing_token = tostring(ARGV[5])
local now_ms = lease_now_ms()
cleanup_leases(now_ms, tombstone_ttl_ms)

local current_owner = redis.call('HGET', KEYS[2], lease_id)
local current_token = redis.call('HGET', KEYS[3], lease_id)
if not current_owner or not current_token then
    local terminal_owner = redis.call('HGET', KEYS[6], lease_id)
    local terminal_token = redis.call('HGET', KEYS[7], lease_id)
    local terminal_state = redis.call('HGET', KEYS[8], lease_id)
    if terminal_owner == owner_id and terminal_token == fencing_token then
        if terminal_state == 'expired' then
            return {-2, now_ms}
        end
        if terminal_state == 'released' then
            return {-3, now_ms}
        end
    end
    return {-1, now_ms}
end

if current_owner ~= owner_id or current_token ~= fencing_token then
    return {-1, now_ms}
end

local expires_at = now_ms + ttl_ms
redis.call('ZADD', KEYS[1], expires_at, lease_id)
return {1, expires_at, now_ms}
"""
)


REDIS_FENCED_LEASE_RELEASE_SCRIPT = (
    _REDIS_LEASE_CLEANUP_LUA
    + r"""
local tombstone_ttl_ms = tonumber(ARGV[1])
local lease_id = ARGV[2]
local owner_id = ARGV[3]
local fencing_token = tostring(ARGV[4])
local now_ms = lease_now_ms()
cleanup_leases(now_ms, tombstone_ttl_ms)

local current_owner = redis.call('HGET', KEYS[2], lease_id)
local current_token = redis.call('HGET', KEYS[3], lease_id)
if not current_owner or not current_token then
    local terminal_owner = redis.call('HGET', KEYS[6], lease_id)
    local terminal_token = redis.call('HGET', KEYS[7], lease_id)
    local terminal_state = redis.call('HGET', KEYS[8], lease_id)
    if terminal_owner == owner_id and terminal_token == fencing_token then
        if terminal_state == 'released' then
            return {0, now_ms}
        end
        if terminal_state == 'expired' then
            return {-2, now_ms}
        end
    end
    return {-1, now_ms}
end

if current_owner ~= owner_id or current_token ~= fencing_token then
    return {-1, now_ms}
end

redis.call('ZREM', KEYS[1], lease_id)
redis.call('HDEL', KEYS[2], lease_id)
redis.call('HDEL', KEYS[3], lease_id)
redis.call('HSET', KEYS[6], lease_id, owner_id)
redis.call('HSET', KEYS[7], lease_id, fencing_token)
redis.call('HSET', KEYS[8], lease_id, 'released')
redis.call('ZADD', KEYS[9], now_ms + tombstone_ttl_ms, lease_id)
if redis.call('ZCARD', KEYS[1]) == 0 then
    redis.call('DEL', KEYS[5])
end
return {1, now_ms}
"""
)


class RedisResourceLeaseManager:
    """Redis-backed fenced leases implemented with atomic Lua operations.

    The injected client must expose the redis-py asyncio ``eval`` signature.
    All keys for one resource share a Redis Cluster hash tag.  Expired active
    leases and old terminal tombstones are removed atomically at the beginning
    of every acquire, renew, and release operation.
    """

    def __init__(
        self,
        redis_client: AsyncRedisEvalClient,
        *,
        namespace: str = "agent-resource-lease",
        default_capacity: int = 1,
        default_ttl_seconds: float = 30.0,
        tombstone_ttl_seconds: float = 300.0,
        poll_interval_seconds: float = 0.1,
        backend_timeout_seconds: float = 5.0,
    ) -> None:
        if redis_client is None:
            raise TypeError("redis_client is required")
        self._redis = redis_client
        self._namespace = _normalize_text(namespace, field_name="namespace")
        self._default_capacity = _validate_capacity(default_capacity)
        self._default_ttl_seconds = _validate_ttl(default_ttl_seconds)
        self._tombstone_ttl_ms = _seconds_to_ms(_validate_ttl(tombstone_ttl_seconds))
        self._poll_interval_seconds = _validate_ttl(poll_interval_seconds)
        self._backend_timeout_seconds = _validate_ttl(backend_timeout_seconds)

    async def acquire(
        self,
        resource: str,
        *,
        owner_id: str,
        capacity: int | None = None,
        ttl_seconds: float | None = None,
        timeout_seconds: float | None = None,
    ) -> Lease:
        normalized_resource = _normalize_text(resource, field_name="resource")
        normalized_owner = _normalize_text(owner_id, field_name="owner_id")
        effective_capacity = _validate_capacity(
            self._default_capacity if capacity is None else capacity
        )
        effective_ttl = _validate_ttl(
            self._default_ttl_seconds if ttl_seconds is None else ttl_seconds
        )
        ttl_ms = _seconds_to_ms(effective_ttl)
        effective_timeout = _validate_timeout(timeout_seconds)
        loop = asyncio.get_running_loop()
        deadline = (
            None if effective_timeout is None else loop.time() + effective_timeout
        )
        keys = self._keys(normalized_resource)
        first_attempt = True

        while True:
            remaining = None if deadline is None else deadline - loop.time()
            if not first_attempt and remaining is not None and remaining <= 0:
                raise ResourceLeaseAcquireTimeout(
                    f"timed out acquiring capacity for {normalized_resource!r}"
                )
            eval_timeout = (
                self._backend_timeout_seconds
                if remaining is None or remaining <= 0
                else min(self._backend_timeout_seconds, remaining)
            )
            first_attempt = False
            lease_id = uuid.uuid4().hex
            try:
                raw = await asyncio.wait_for(
                    self._redis.eval(
                        REDIS_FENCED_LEASE_ACQUIRE_SCRIPT,
                        len(keys),
                        *keys,
                        ttl_ms,
                        self._tombstone_ttl_ms,
                        effective_capacity,
                        lease_id,
                        normalized_owner,
                    ),
                    timeout=eval_timeout,
                )
            except asyncio.TimeoutError as exc:
                if (
                    remaining is not None
                    and remaining > 0
                    and remaining <= self._backend_timeout_seconds
                ):
                    raise ResourceLeaseAcquireTimeout(
                        f"timed out acquiring capacity for {normalized_resource!r}"
                    ) from exc
                raise ResourceLeaseBackendError(
                    f"Redis acquire timed out for resource {normalized_resource!r}"
                ) from exc
            result = _decode_eval_result(raw)
            code = result[0]
            if code == 1:
                if len(result) < 3:
                    raise ResourceLeaseBackendError(
                        "Redis acquire result is missing fencing or expiry data"
                    )
                fencing_token = result[1]
                expires_at = result[2] / 1000.0
                return _LeaseHandle(
                    self,
                    LeaseIdentity(
                        resource=normalized_resource,
                        lease_id=lease_id,
                        owner_id=normalized_owner,
                        fencing_token=fencing_token,
                    ),
                    expires_at=expires_at,
                    ttl_seconds=effective_ttl,
                )
            if code == -4:
                configured = result[1] if len(result) > 1 else "unknown"
                raise ResourceLeaseCapacityMismatch(
                    f"resource {normalized_resource!r} already uses capacity "
                    f"{configured}, not {effective_capacity}"
                )
            if code == -5:
                # UUID collision with an active lease or a recent tombstone.
                # Generate a fresh id without consuming a fencing token.
                continue
            if code != 0:
                raise ResourceLeaseBackendError(
                    f"unknown Redis acquire result code: {code}"
                )

            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                raise ResourceLeaseAcquireTimeout(
                    f"timed out acquiring capacity for {normalized_resource!r}"
                )
            next_expiry_ms = result[1] if len(result) > 1 else 0
            server_now_ms = result[2] if len(result) > 2 else 0
            expiry_hint = (
                max(0.001, (next_expiry_ms - server_now_ms) / 1000.0)
                if next_expiry_ms and server_now_ms
                else self._poll_interval_seconds
            )
            delay = min(self._poll_interval_seconds, expiry_hint)
            if remaining is not None:
                delay = min(delay, max(0.001, remaining))
            await asyncio.sleep(delay)

    async def renew(
        self,
        identity: LeaseIdentity,
        *,
        ttl_seconds: float,
    ) -> Lease:
        normalized = _validate_identity(identity)
        effective_ttl = _validate_ttl(ttl_seconds)
        keys = self._keys(normalized.resource)
        try:
            raw = await asyncio.wait_for(
                self._redis.eval(
                    REDIS_FENCED_LEASE_RENEW_SCRIPT,
                    len(keys),
                    *keys,
                    _seconds_to_ms(effective_ttl),
                    self._tombstone_ttl_ms,
                    normalized.lease_id,
                    normalized.owner_id,
                    normalized.fencing_token,
                ),
                timeout=self._backend_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ResourceLeaseBackendError(
                f"Redis renew timed out for resource {normalized.resource!r}"
            ) from exc
        result = _decode_eval_result(raw)
        code = result[0]
        if code == 1:
            if len(result) < 2:
                raise ResourceLeaseBackendError(
                    "Redis renew result is missing expiry data"
                )
            return _LeaseHandle(
                self,
                normalized,
                expires_at=result[1] / 1000.0,
                ttl_seconds=effective_ttl,
            )
        self._raise_for_redis_lease_code(
            code,
            identity=normalized,
            operation="renew",
        )
        raise AssertionError("unreachable")

    async def release(self, identity: LeaseIdentity) -> bool:
        normalized = _validate_identity(identity)
        keys = self._keys(normalized.resource)
        try:
            raw = await asyncio.wait_for(
                self._redis.eval(
                    REDIS_FENCED_LEASE_RELEASE_SCRIPT,
                    len(keys),
                    *keys,
                    self._tombstone_ttl_ms,
                    normalized.lease_id,
                    normalized.owner_id,
                    normalized.fencing_token,
                ),
                timeout=self._backend_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ResourceLeaseBackendError(
                f"Redis release timed out for resource {normalized.resource!r}"
            ) from exc
        result = _decode_eval_result(raw)
        code = result[0]
        if code == 1:
            return True
        if code == 0:
            return False
        self._raise_for_redis_lease_code(
            code,
            identity=normalized,
            operation="release",
        )
        raise AssertionError("unreachable")

    def _keys(self, resource: str) -> tuple[str, ...]:
        digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()
        base = f"{self._namespace}:{{{digest}}}"
        return (
            f"{base}:active",
            f"{base}:owners",
            f"{base}:tokens",
            f"{base}:fencing-counter",
            f"{base}:capacity",
            f"{base}:terminal-owners",
            f"{base}:terminal-tokens",
            f"{base}:terminal-states",
            f"{base}:terminal-expiries",
        )

    @staticmethod
    def _raise_for_redis_lease_code(
        code: int,
        *,
        identity: LeaseIdentity,
        operation: str,
    ) -> None:
        if code == -1:
            raise ResourceLeaseOwnershipError(
                f"owner or fencing token mismatch for lease {identity.lease_id!r}"
            )
        if code == -2:
            raise ResourceLeaseExpiredError(f"lease {identity.lease_id!r} has expired")
        if code == -3:
            raise ResourceLeaseReleasedError(
                f"cannot {operation} released lease {identity.lease_id!r}"
            )
        raise ResourceLeaseBackendError(
            f"unknown Redis {operation} result code: {code}"
        )
