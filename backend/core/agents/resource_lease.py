"""Capacity-aware fenced leases for cross-process agent resources.

The module deliberately has no Redis construction or configuration logic.  A
caller that needs distributed leases must inject an already-created async Redis
client into :class:`RedisResourceLeaseManager`.  Importing this module therefore
never opens a socket and does not change the default in-process runtime.

Fencing tokens are monotonically increasing per resource.  Renewing a lease
keeps its token; releasing or expiring it never makes that token valid again.
Consumers that protect an external side effect should persist the largest token
they have accepted and reject writes carrying an older token.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class ResourceLeaseError(RuntimeError):
    """Base error for fenced resource leases."""


class ResourceLeaseAcquireTimeout(ResourceLeaseError):
    """Raised when capacity is not obtained before the acquire deadline."""


class ResourceLeaseOwnershipError(ResourceLeaseError):
    """Raised when an owner or fencing token does not match the active lease."""


class ResourceLeaseExpiredError(ResourceLeaseError):
    """Raised when an operation addresses a lease whose TTL elapsed."""


class ResourceLeaseReleasedError(ResourceLeaseError):
    """Raised when a released lease is renewed."""


class ResourceLeaseCapacityMismatch(ResourceLeaseError):
    """Raised when one resource is acquired with inconsistent capacities."""


class ResourceLeaseBackendError(ResourceLeaseError):
    """Raised when a lease backend returns a malformed or unknown result."""


@dataclass(frozen=True)
class LeaseIdentity:
    """Immutable credentials required to renew or release one lease."""

    resource: str
    lease_id: str
    owner_id: str
    fencing_token: int


@runtime_checkable
class Lease(Protocol):
    """One acquired fenced lease.

    The handle is async because renew and release may require a network round
    trip.  ``expires_at`` is a Unix timestamp for diagnostics only; Redis is the
    authoritative clock for distributed leases.
    """

    @property
    def identity(self) -> LeaseIdentity: ...

    @property
    def resource(self) -> str: ...

    @property
    def lease_id(self) -> str: ...

    @property
    def owner_id(self) -> str: ...

    @property
    def fencing_token(self) -> int: ...

    @property
    def expires_at(self) -> float: ...

    @property
    def released(self) -> bool: ...

    async def renew(self, *, ttl_seconds: float | None = None) -> Lease: ...

    async def release(self) -> bool: ...

    async def __aenter__(self) -> Lease: ...

    async def __aexit__(self, exc_type, exc, traceback) -> None: ...


@runtime_checkable
class ResourceLeaseManager(Protocol):
    """Async contract shared by local and distributed lease backends."""

    async def acquire(
        self,
        resource: str,
        *,
        owner_id: str,
        capacity: int | None = None,
        ttl_seconds: float | None = None,
        timeout_seconds: float | None = None,
    ) -> Lease: ...

    async def renew(
        self,
        identity: LeaseIdentity,
        *,
        ttl_seconds: float,
    ) -> Lease: ...

    async def release(self, identity: LeaseIdentity) -> bool: ...


@runtime_checkable
class AsyncRedisEvalClient(Protocol):
    """The only Redis capability required by the distributed manager."""

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: Any,
    ) -> Any: ...


class _LeaseHandle:
    def __init__(
        self,
        manager: ResourceLeaseManager,
        identity: LeaseIdentity,
        *,
        expires_at: float,
        ttl_seconds: float,
    ) -> None:
        self._manager = manager
        self._identity = identity
        self._expires_at = float(expires_at)
        self._ttl_seconds = float(ttl_seconds)
        self._released = False

    @property
    def identity(self) -> LeaseIdentity:
        return self._identity

    @property
    def resource(self) -> str:
        return self._identity.resource

    @property
    def lease_id(self) -> str:
        return self._identity.lease_id

    @property
    def owner_id(self) -> str:
        return self._identity.owner_id

    @property
    def fencing_token(self) -> int:
        return self._identity.fencing_token

    @property
    def expires_at(self) -> float:
        return self._expires_at

    @property
    def released(self) -> bool:
        return self._released

    async def renew(self, *, ttl_seconds: float | None = None) -> Lease:
        effective_ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        renewed = await self._manager.renew(
            self._identity,
            ttl_seconds=effective_ttl,
        )
        self._expires_at = renewed.expires_at
        self._ttl_seconds = float(effective_ttl)
        self._released = False
        return self

    async def release(self) -> bool:
        released = await self._manager.release(self._identity)
        self._released = True
        return released

    async def __aenter__(self) -> Lease:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        await self.release()


@dataclass
class _InMemoryLeaseRecord:
    owner_id: str
    fencing_token: int
    expires_at: float


@dataclass
class _TerminalLeaseRecord:
    owner_id: str
    fencing_token: int
    state: str
    forget_at: float


class InMemoryResourceLeaseManager:
    """Process-local fenced lease manager with TTL-aware capacity waiting."""

    def __init__(
        self,
        *,
        default_capacity: int = 1,
        default_ttl_seconds: float = 30.0,
        tombstone_ttl_seconds: float = 300.0,
    ) -> None:
        self._default_capacity = _validate_capacity(default_capacity)
        self._default_ttl_seconds = _validate_ttl(default_ttl_seconds)
        self._tombstone_ttl_seconds = _validate_ttl(tombstone_ttl_seconds)
        self._condition = asyncio.Condition()
        self._active: dict[str, dict[str, _InMemoryLeaseRecord]] = {}
        self._terminal: dict[tuple[str, str], _TerminalLeaseRecord] = {}
        self._capacities: dict[str, int] = {}
        self._fencing_counters: dict[str, int] = {}

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
        effective_timeout = _validate_timeout(timeout_seconds)
        loop = asyncio.get_running_loop()
        deadline = (
            None if effective_timeout is None else loop.time() + effective_timeout
        )

        async with self._condition:
            while True:
                now = time.time()
                self._cleanup_locked(normalized_resource, now=now)
                configured_capacity = self._capacities.setdefault(
                    normalized_resource,
                    effective_capacity,
                )
                if configured_capacity != effective_capacity:
                    raise ResourceLeaseCapacityMismatch(
                        f"resource {normalized_resource!r} already uses capacity "
                        f"{configured_capacity}, not {effective_capacity}"
                    )

                active = self._active.setdefault(normalized_resource, {})
                if len(active) < configured_capacity:
                    fencing_token = (
                        self._fencing_counters.get(normalized_resource, 0) + 1
                    )
                    self._fencing_counters[normalized_resource] = fencing_token
                    lease_id = uuid.uuid4().hex
                    expires_at = now + effective_ttl
                    active[lease_id] = _InMemoryLeaseRecord(
                        owner_id=normalized_owner,
                        fencing_token=fencing_token,
                        expires_at=expires_at,
                    )
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

                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    raise ResourceLeaseAcquireTimeout(
                        f"timed out acquiring capacity for {normalized_resource!r}"
                    )
                nearest_expiry = min(record.expires_at for record in active.values())
                expiry_wait = max(0.001, nearest_expiry - now)
                wait_seconds = (
                    expiry_wait
                    if remaining is None
                    else min(expiry_wait, max(0.001, remaining))
                )
                try:
                    await asyncio.wait_for(
                        self._condition.wait(),
                        timeout=wait_seconds,
                    )
                except TimeoutError:
                    # Either the acquire deadline or the nearest TTL was reached.
                    # Re-check both while holding the condition lock.
                    pass

    async def renew(
        self,
        identity: LeaseIdentity,
        *,
        ttl_seconds: float,
    ) -> Lease:
        normalized = _validate_identity(identity)
        effective_ttl = _validate_ttl(ttl_seconds)
        async with self._condition:
            now = time.time()
            self._cleanup_locked(normalized.resource, now=now)
            record = self._active.get(normalized.resource, {}).get(normalized.lease_id)
            if record is None:
                self._raise_for_terminal_locked(normalized, operation="renew")
            assert record is not None
            self._validate_record_credentials(normalized, record)
            record.expires_at = now + effective_ttl
            self._condition.notify_all()
            return _LeaseHandle(
                self,
                normalized,
                expires_at=record.expires_at,
                ttl_seconds=effective_ttl,
            )

    async def release(self, identity: LeaseIdentity) -> bool:
        normalized = _validate_identity(identity)
        async with self._condition:
            now = time.time()
            self._cleanup_locked(normalized.resource, now=now)
            active = self._active.get(normalized.resource, {})
            record = active.get(normalized.lease_id)
            if record is None:
                terminal = self._terminal.get(
                    (normalized.resource, normalized.lease_id)
                )
                if terminal is not None:
                    self._validate_terminal_credentials(normalized, terminal)
                    if terminal.state == "released":
                        return False
                    raise ResourceLeaseExpiredError(
                        f"lease {normalized.lease_id!r} has expired"
                    )
                raise ResourceLeaseOwnershipError(
                    f"lease {normalized.lease_id!r} is not owned by these credentials"
                )

            self._validate_record_credentials(normalized, record)
            del active[normalized.lease_id]
            self._terminal[(normalized.resource, normalized.lease_id)] = (
                _TerminalLeaseRecord(
                    owner_id=record.owner_id,
                    fencing_token=record.fencing_token,
                    state="released",
                    forget_at=now + self._tombstone_ttl_seconds,
                )
            )
            self._condition.notify_all()
            return True

    def _cleanup_locked(self, resource: str, *, now: float) -> None:
        active = self._active.get(resource, {})
        for lease_id, record in list(active.items()):
            if record.expires_at > now:
                continue
            del active[lease_id]
            self._terminal[(resource, lease_id)] = _TerminalLeaseRecord(
                owner_id=record.owner_id,
                fencing_token=record.fencing_token,
                state="expired",
                forget_at=now + self._tombstone_ttl_seconds,
            )

        for key, terminal in list(self._terminal.items()):
            if key[0] == resource and terminal.forget_at <= now:
                del self._terminal[key]

    def _raise_for_terminal_locked(
        self,
        identity: LeaseIdentity,
        *,
        operation: str,
    ) -> None:
        terminal = self._terminal.get((identity.resource, identity.lease_id))
        if terminal is None:
            raise ResourceLeaseOwnershipError(
                f"lease {identity.lease_id!r} is not owned by these credentials"
            )
        self._validate_terminal_credentials(identity, terminal)
        if terminal.state == "expired":
            raise ResourceLeaseExpiredError(f"lease {identity.lease_id!r} has expired")
        raise ResourceLeaseReleasedError(
            f"cannot {operation} released lease {identity.lease_id!r}"
        )

    @staticmethod
    def _validate_record_credentials(
        identity: LeaseIdentity,
        record: _InMemoryLeaseRecord,
    ) -> None:
        if (
            record.owner_id != identity.owner_id
            or record.fencing_token != identity.fencing_token
        ):
            raise ResourceLeaseOwnershipError(
                f"owner or fencing token mismatch for lease {identity.lease_id!r}"
            )

    @staticmethod
    def _validate_terminal_credentials(
        identity: LeaseIdentity,
        record: _TerminalLeaseRecord,
    ) -> None:
        if (
            record.owner_id != identity.owner_id
            or record.fencing_token != identity.fencing_token
        ):
            raise ResourceLeaseOwnershipError(
                f"owner or fencing token mismatch for lease {identity.lease_id!r}"
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
    ) -> None:
        if redis_client is None:
            raise TypeError("redis_client is required")
        self._redis = redis_client
        self._namespace = _normalize_text(namespace, field_name="namespace")
        self._default_capacity = _validate_capacity(default_capacity)
        self._default_ttl_seconds = _validate_ttl(default_ttl_seconds)
        self._tombstone_ttl_ms = _seconds_to_ms(_validate_ttl(tombstone_ttl_seconds))
        self._poll_interval_seconds = _validate_ttl(poll_interval_seconds)

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

        while True:
            lease_id = uuid.uuid4().hex
            raw = await self._redis.eval(
                REDIS_FENCED_LEASE_ACQUIRE_SCRIPT,
                len(keys),
                *keys,
                ttl_ms,
                self._tombstone_ttl_ms,
                effective_capacity,
                lease_id,
                normalized_owner,
            )
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
        raw = await self._redis.eval(
            REDIS_FENCED_LEASE_RENEW_SCRIPT,
            len(keys),
            *keys,
            _seconds_to_ms(effective_ttl),
            self._tombstone_ttl_ms,
            normalized.lease_id,
            normalized.owner_id,
            normalized.fencing_token,
        )
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
        raw = await self._redis.eval(
            REDIS_FENCED_LEASE_RELEASE_SCRIPT,
            len(keys),
            *keys,
            self._tombstone_ttl_ms,
            normalized.lease_id,
            normalized.owner_id,
            normalized.fencing_token,
        )
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


def _normalize_text(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _validate_capacity(value: int) -> int:
    if isinstance(value, bool):
        raise ValueError("capacity must be a positive integer")
    try:
        capacity = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("capacity must be a positive integer") from exc
    if capacity < 1 or capacity != value:
        raise ValueError("capacity must be a positive integer")
    return capacity


def _validate_ttl(value: float) -> float:
    try:
        ttl = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ttl_seconds must be a positive finite number") from exc
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("ttl_seconds must be a positive finite number")
    return ttl


def _validate_timeout(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "timeout_seconds must be a non-negative finite number"
        ) from exc
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout_seconds must be a non-negative finite number")
    return timeout


def _validate_identity(identity: LeaseIdentity) -> LeaseIdentity:
    if not isinstance(identity, LeaseIdentity):
        raise TypeError("identity must be a LeaseIdentity")
    resource = _normalize_text(identity.resource, field_name="resource")
    lease_id = _normalize_text(identity.lease_id, field_name="lease_id")
    owner_id = _normalize_text(identity.owner_id, field_name="owner_id")
    if isinstance(identity.fencing_token, bool):
        raise ValueError("fencing_token must be a positive integer")
    try:
        fencing_token = int(identity.fencing_token)
    except (TypeError, ValueError) as exc:
        raise ValueError("fencing_token must be a positive integer") from exc
    if fencing_token < 1 or fencing_token != identity.fencing_token:
        raise ValueError("fencing_token must be a positive integer")
    return LeaseIdentity(
        resource=resource,
        lease_id=lease_id,
        owner_id=owner_id,
        fencing_token=fencing_token,
    )


def _seconds_to_ms(seconds: float) -> int:
    return max(1, int(math.ceil(seconds * 1000.0)))


def _decode_eval_result(raw: Any) -> list[int]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ResourceLeaseBackendError(
            f"Redis lease script returned malformed result: {raw!r}"
        )
    decoded: list[int] = []
    for item in raw:
        try:
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            decoded.append(int(item))
        except (TypeError, ValueError) as exc:
            raise ResourceLeaseBackendError(
                f"Redis lease script returned non-integer value: {item!r}"
            ) from exc
    return decoded


__all__ = [
    "AsyncRedisEvalClient",
    "InMemoryResourceLeaseManager",
    "Lease",
    "LeaseIdentity",
    "REDIS_FENCED_LEASE_ACQUIRE_SCRIPT",
    "REDIS_FENCED_LEASE_RELEASE_SCRIPT",
    "REDIS_FENCED_LEASE_RENEW_SCRIPT",
    "RedisResourceLeaseManager",
    "ResourceLeaseAcquireTimeout",
    "ResourceLeaseBackendError",
    "ResourceLeaseCapacityMismatch",
    "ResourceLeaseError",
    "ResourceLeaseExpiredError",
    "ResourceLeaseManager",
    "ResourceLeaseOwnershipError",
    "ResourceLeaseReleasedError",
]
