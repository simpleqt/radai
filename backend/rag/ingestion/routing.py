"""Compatibility facade for knowledge-base backend policy.

New code should import from ``backend.domain.knowledge_base.backend_policy``.
This module stays in place so older ingestion callers keep working during the
architecture migration.
"""

from backend.domain.knowledge_base.backend_policy import (
    BLOCKED_BACKEND,
    HYBRID_RETRIEVAL_BACKEND,
    LEGACY_BACKEND,
    SELF_BACKEND,
    VALID_INGESTION_BACKENDS,
    VALID_RETRIEVAL_BACKENDS,
    declared_ingestion_backend,
    declared_retrieval_backend,
    is_ingestion_blocked,
    resolve_ingestion_backend,
    resolve_retrieval_backend,
    should_use_self_ingestion,
    validate_backend_isolation,
)

__all__ = [
    "BLOCKED_BACKEND",
    "HYBRID_RETRIEVAL_BACKEND",
    "LEGACY_BACKEND",
    "SELF_BACKEND",
    "VALID_INGESTION_BACKENDS",
    "VALID_RETRIEVAL_BACKENDS",
    "declared_ingestion_backend",
    "declared_retrieval_backend",
    "is_ingestion_blocked",
    "resolve_ingestion_backend",
    "resolve_retrieval_backend",
    "should_use_self_ingestion",
    "validate_backend_isolation",
]
