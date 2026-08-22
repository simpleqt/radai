"""Knowledge-base catalog contracts independent from persistence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KnowledgeBaseCatalogEntry:
    kb_id: str
    kb_code: str
    kb_name: str
    owner_id: str | None
    is_default: bool
    status: str
    ingestion_backend: str | None = None
    retrieval_backend: str | None = None
    self_ingestion_enabled: bool | None = None
    self_retriever_enabled: bool | None = None
