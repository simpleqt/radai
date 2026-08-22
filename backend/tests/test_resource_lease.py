from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
from typing import Any

import pytest

from backend.core.agents.resource_lease import (
    REDIS_FENCED_LEASE_ACQUIRE_SCRIPT,
    REDIS_FENCED_LEASE_RELEASE_SCRIPT,
    REDIS_FENCED_LEASE_RENEW_SCRIPT,
    InMemoryResourceLeaseManager,
    Lease,
    LeaseIdentity,
    RedisResourceLeaseManager,
    ResourceLeaseAcquireTimeout,
    ResourceLeaseBackendError,
    ResourceLeaseCapacityMismatch,
    ResourceLeaseExpiredError,
    ResourceLeaseManager,
    ResourceLeaseOwnershipError,
    ResourceLeaseReleasedError,
)


class QueuedEvalRedis:
    """Minimal injected Redis double; constructing a manager cannot call it."""

    def __init__(self, *responses: Any) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, int, tuple[Any, ...]]] = []

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: Any,
    ) -> Any:
        self.calls.append((script, numkeys, keys_and_args))
        if not self.responses:
            raise AssertionError("unexpected Redis eval call")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.mark.asyncio
async def test_contracts_are_runtime_checkable_and_redis_constructor_is_lazy() -> None:
    memory = InMemoryResourceLeaseManager()
    redis = QueuedEvalRedis()
    distributed = RedisResourceLeaseManager(redis)

    assert isinstance(memory, ResourceLeaseManager)
    assert isinstance(distributed, ResourceLeaseManager)
    assert redis.calls == []

    lease = await memory.acquire("model:gpt", owner_id="run-1")
    assert isinstance(lease, Lease)
    assert await lease.release() is True


@pytest.mark.asyncio
async def test_in_memory_capacity_wait_release_and_monotonic_fencing() -> None:
    manager = InMemoryResourceLeaseManager(default_capacity=1)
    first = await manager.acquire("model:gpt", owner_id="run-1")

    with pytest.raises(ResourceLeaseAcquireTimeout):
        await manager.acquire(
            "model:gpt",
            owner_id="run-2",
            timeout_seconds=0,
        )

    waiter = asyncio.create_task(
        manager.acquire(
            "model:gpt",
            owner_id="run-2",
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0)
    assert not waiter.done()

    assert await first.release() is True
    assert await first.release() is False
    second = await waiter
    assert second.fencing_token > first.fencing_token
    assert await second.release() is True


@pytest.mark.asyncio
async def test_in_memory_ttl_reclaims_capacity_and_invalidates_old_token() -> None:
    manager = InMemoryResourceLeaseManager(
        default_capacity=1,
        tombstone_ttl_seconds=1,
    )
    expired = await manager.acquire(
        "tool:browser",
        owner_id="run-1",
        ttl_seconds=0.02,
    )
    await asyncio.sleep(0.04)

    replacement = await manager.acquire(
        "tool:browser",
        owner_id="run-2",
        timeout_seconds=0.2,
    )
    assert replacement.fencing_token > expired.fencing_token

    with pytest.raises(ResourceLeaseExpiredError):
        await expired.renew(ttl_seconds=1)
    with pytest.raises(ResourceLeaseExpiredError):
        await expired.release()

    assert await replacement.release() is True


@pytest.mark.asyncio
async def test_in_memory_renew_extends_ttl_without_changing_fencing_token() -> None:
    manager = InMemoryResourceLeaseManager(default_capacity=1)
    lease = await manager.acquire(
        "tool:shell",
        owner_id="run-1",
        ttl_seconds=0.05,
    )
    original_token = lease.fencing_token
    original_expiry = lease.expires_at
    await asyncio.sleep(0.01)

    renewed = await lease.renew(ttl_seconds=0.2)
    assert renewed is lease
    assert lease.fencing_token == original_token
    assert lease.expires_at > original_expiry

    await asyncio.sleep(0.06)
    with pytest.raises(ResourceLeaseAcquireTimeout):
        await manager.acquire(
            "tool:shell",
            owner_id="run-2",
            timeout_seconds=0,
        )
    assert await lease.release() is True


@pytest.mark.asyncio
async def test_in_memory_validates_owner_token_capacity_and_terminal_state() -> None:
    manager = InMemoryResourceLeaseManager(default_capacity=2)
    lease = await manager.acquire(
        "pool:agent",
        owner_id="run-1",
        capacity=2,
    )

    with pytest.raises(ResourceLeaseOwnershipError):
        await manager.renew(
            replace(lease.identity, owner_id="run-other"),
            ttl_seconds=1,
        )
    with pytest.raises(ResourceLeaseOwnershipError):
        await manager.release(
            replace(lease.identity, fencing_token=lease.fencing_token + 1)
        )
    with pytest.raises(ResourceLeaseCapacityMismatch):
        await manager.acquire(
            "pool:agent",
            owner_id="run-2",
            capacity=1,
            timeout_seconds=0,
        )

    assert await lease.release() is True
    with pytest.raises(ResourceLeaseReleasedError):
        await lease.renew(ttl_seconds=1)
    assert await lease.release() is False


@pytest.mark.asyncio
async def test_redis_handle_uses_atomic_scripts_and_cluster_local_keys() -> None:
    redis = QueuedEvalRedis(
        [1, 7, 1_700_000_030_000, 1_700_000_000_000],
        [1, 1_700_000_060_000, 1_700_000_000_000],
        [1, 1_700_000_000_000],
        [0, 1_700_000_000_001],
    )
    manager = RedisResourceLeaseManager(redis, namespace="test-lease")
    assert redis.calls == []

    lease = await manager.acquire(
        "model/gpt",
        owner_id="run-1",
        capacity=2,
        ttl_seconds=30,
        timeout_seconds=0,
    )
    assert lease.fencing_token == 7
    assert lease.expires_at == 1_700_000_030

    await lease.renew(ttl_seconds=60)
    assert lease.fencing_token == 7
    assert lease.expires_at == 1_700_000_060
    assert await lease.release() is True
    assert await lease.release() is False

    assert [call[0] for call in redis.calls] == [
        REDIS_FENCED_LEASE_ACQUIRE_SCRIPT,
        REDIS_FENCED_LEASE_RENEW_SCRIPT,
        REDIS_FENCED_LEASE_RELEASE_SCRIPT,
        REDIS_FENCED_LEASE_RELEASE_SCRIPT,
    ]
    for _, numkeys, keys_and_args in redis.calls:
        assert numkeys == 9
        keys = keys_and_args[:numkeys]
        hash_tags = {key.split("{")[1].split("}")[0] for key in keys}
        assert len(hash_tags) == 1
        assert all(key.startswith("test-lease:") for key in keys)


@pytest.mark.asyncio
async def test_redis_acquire_maps_capacity_and_timeout_results() -> None:
    capacity_redis = QueuedEvalRedis([-4, 3, 1_700_000_000_000])
    capacity_manager = RedisResourceLeaseManager(capacity_redis)
    with pytest.raises(ResourceLeaseCapacityMismatch, match="capacity 3"):
        await capacity_manager.acquire(
            "model:gpt",
            owner_id="run-1",
            capacity=2,
            timeout_seconds=0,
        )

    full_redis = QueuedEvalRedis([0, 1_700_000_001_000, 1_700_000_000_000])
    full_manager = RedisResourceLeaseManager(full_redis)
    with pytest.raises(ResourceLeaseAcquireTimeout):
        await full_manager.acquire(
            "model:gpt",
            owner_id="run-1",
            timeout_seconds=0,
        )
    assert len(full_redis.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "code", "expected_error"),
    [
        ("renew", -1, ResourceLeaseOwnershipError),
        ("renew", -2, ResourceLeaseExpiredError),
        ("renew", -3, ResourceLeaseReleasedError),
        ("renew", -99, ResourceLeaseBackendError),
        ("release", -1, ResourceLeaseOwnershipError),
        ("release", -2, ResourceLeaseExpiredError),
        ("release", -99, ResourceLeaseBackendError),
    ],
)
async def test_redis_maps_lease_validation_results(
    operation: str,
    code: int,
    expected_error: type[Exception],
) -> None:
    redis = QueuedEvalRedis([code, 1_700_000_000_000])
    manager = RedisResourceLeaseManager(redis)
    identity = LeaseIdentity(
        resource="model:gpt",
        lease_id="lease-1",
        owner_id="run-1",
        fencing_token=7,
    )

    with pytest.raises(expected_error):
        if operation == "renew":
            await manager.renew(identity, ttl_seconds=30)
        else:
            await manager.release(identity)


def test_lua_scripts_clean_expiry_and_enforce_fenced_ownership_atomically() -> None:
    scripts = (
        REDIS_FENCED_LEASE_ACQUIRE_SCRIPT,
        REDIS_FENCED_LEASE_RENEW_SCRIPT,
        REDIS_FENCED_LEASE_RELEASE_SCRIPT,
    )
    for script in scripts:
        assert "redis.call('TIME')" in script
        assert "cleanup_leases(now_ms, tombstone_ttl_ms)" in script
        assert "redis.call('ZRANGEBYSCORE', KEYS[1]" in script
        assert "redis.call('HGET', KEYS[3]" in script

    assert "redis.call('ZCARD', KEYS[1])" in REDIS_FENCED_LEASE_ACQUIRE_SCRIPT
    assert "redis.call('INCR', KEYS[4])" in REDIS_FENCED_LEASE_ACQUIRE_SCRIPT
    assert "current_owner ~= owner_id" in REDIS_FENCED_LEASE_RENEW_SCRIPT
    assert "current_token ~= fencing_token" in REDIS_FENCED_LEASE_RENEW_SCRIPT
    assert "current_owner ~= owner_id" in REDIS_FENCED_LEASE_RELEASE_SCRIPT
    assert "current_token ~= fencing_token" in REDIS_FENCED_LEASE_RELEASE_SCRIPT
    assert "terminal_state == 'released'" in REDIS_FENCED_LEASE_RELEASE_SCRIPT


@pytest.mark.asyncio
async def test_redis_rejects_malformed_script_results() -> None:
    manager = RedisResourceLeaseManager(QueuedEvalRedis(None))
    with pytest.raises(ResourceLeaseBackendError, match="malformed"):
        await manager.acquire(
            "model:gpt",
            owner_id="run-1",
            timeout_seconds=0,
        )
