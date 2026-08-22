"""资源租约内存实现:进程内多容量租约、过期清理与终态凭证校验。(拆自 resource_lease.py)"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import time
import uuid
from backend.core.agents.resource_lease_model import (
    Lease,
    LeaseIdentity,
    ResourceLeaseAcquireTimeout,
    ResourceLeaseCapacityMismatch,
    ResourceLeaseExpiredError,
    ResourceLeaseOwnershipError,
    ResourceLeaseReleasedError,
    _LeaseHandle,
    _normalize_text,
    _validate_capacity,
    _validate_identity,
    _validate_timeout,
    _validate_ttl,
)


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
        # One process-wide sequence is monotonic for every individual resource
        # and avoids retaining one counter entry per transient resource name.
        self._fencing_counter = 0

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
                    self._fencing_counter += 1
                    fencing_token = self._fencing_counter
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
                except asyncio.TimeoutError:
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
            self._drop_inactive_resource_locked(normalized.resource)
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
        self._drop_inactive_resource_locked(resource)

    def _drop_inactive_resource_locked(self, resource: str) -> None:
        if self._active.get(resource):
            return
        self._active.pop(resource, None)
        self._capacities.pop(resource, None)

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
