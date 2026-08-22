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

from backend.core.agents.resource_lease_model import (  # noqa: F401
    AsyncRedisEvalClient,
    Lease,
    LeaseIdentity,
    ResourceLeaseAcquireTimeout,
    ResourceLeaseBackendError,
    ResourceLeaseCapacityMismatch,
    ResourceLeaseError,
    ResourceLeaseExpiredError,
    ResourceLeaseManager,
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
from backend.core.agents.resource_lease_memory import (  # noqa: F401
    InMemoryResourceLeaseManager,
    _InMemoryLeaseRecord,
    _TerminalLeaseRecord,
)
from backend.core.agents.resource_lease_redis import (  # noqa: F401
    REDIS_FENCED_LEASE_ACQUIRE_SCRIPT,
    REDIS_FENCED_LEASE_RELEASE_SCRIPT,
    REDIS_FENCED_LEASE_RENEW_SCRIPT,
    RedisResourceLeaseManager,
    _REDIS_LEASE_CLEANUP_LUA,
)

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
