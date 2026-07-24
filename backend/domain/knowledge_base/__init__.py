"""Knowledge-base domain metadata."""

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
from backend.domain.knowledge_base.catalog import KnowledgeBaseCatalogEntry

__all__ = [
    "BLOCKED_BACKEND",
    "HYBRID_RETRIEVAL_BACKEND",
    "LEGACY_BACKEND",
    "SELF_BACKEND",
    "VALID_INGESTION_BACKENDS",
    "VALID_RETRIEVAL_BACKENDS",
    "KnowledgeBaseCatalogEntry",
    "declared_ingestion_backend",
    "declared_retrieval_backend",
    "is_ingestion_blocked",
    "resolve_ingestion_backend",
    "resolve_retrieval_backend",
    "should_use_self_ingestion",
    "validate_backend_isolation",
]
