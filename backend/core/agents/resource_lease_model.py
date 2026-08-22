"""资源租约模型层:异常族、租约身份/协议、租约句柄与参数校验、Redis eval 解码。(拆自 resource_lease.py)"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Protocol
from typing import runtime_checkable
import math


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
