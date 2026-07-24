"""Self-ingestion compatibility layer.

The package is intentionally inert by default.  It only routes traffic away from
the legacy LightRAG path when global feature flags and per-KB metadata agree.
"""

from backend.domain.knowledge_base.backend_policy import (
    BLOCKED_BACKEND,
    HYBRID_RETRIEVAL_BACKEND,
    LEGACY_BACKEND,
    SELF_BACKEND,
    declared_ingestion_backend,
    declared_retrieval_backend,
    is_ingestion_blocked,
    resolve_ingestion_backend,
    resolve_retrieval_backend,
    should_use_self_ingestion,
)

__all__ = [
    "BLOCKED_BACKEND",
    "HYBRID_RETRIEVAL_BACKEND",
    "LEGACY_BACKEND",
    "SELF_BACKEND",
    "SelfIngestionRepairWorker",
    "declared_ingestion_backend",
    "declared_retrieval_backend",
    "is_ingestion_blocked",
    "resolve_ingestion_backend",
    "resolve_retrieval_backend",
    "self_ingestion_repair_worker",
    "should_use_self_ingestion",
]


def __getattr__(name):
    if name in {"SelfIngestionRepairWorker", "self_ingestion_repair_worker"}:
        from backend.rag.ingestion.repair_worker import (
            SelfIngestionRepairWorker,
            self_ingestion_repair_worker,
        )

        values = {
            "SelfIngestionRepairWorker": SelfIngestionRepairWorker,
            "self_ingestion_repair_worker": self_ingestion_repair_worker,
        }
        return values[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
