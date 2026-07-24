"""Shared retrieval filters for self-ingestion v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from backend.rag.ingestion import states


@dataclass(slots=True)
class RetrievalFilter:
    """One filter object shared by all self-ingestion retrieval paths.

    The defaults are intentionally self-only and visible-only. Legacy retrieval
    uses its own stack and should not construct this filter.
    """

    kb_id: str
    kb_code: str = ""
    doc_statuses: tuple[str, ...] = states.RETRIEVAL_VISIBLE_DOC_STATUSES
    is_active: bool = True
    is_visible: bool = True
    commit_status: str = states.COMMIT_COMMITTED
    compatible_schema_versions: tuple[str, ...] = field(default_factory=tuple)
    source_backend: str = "self_ingestion"
    uploaded_at_gte: datetime | date | None = None
    uploaded_at_lte: datetime | date | None = None
    committed_at_gte: datetime | date | None = None
    committed_at_lte: datetime | date | None = None
    visible_at_gte: datetime | date | None = None
    visible_at_lte: datetime | date | None = None

    def with_defaults(self, *, kb_id: str, kb_code: str = "") -> "RetrievalFilter":
        if self.kb_id == kb_id and (self.kb_code or "") == (kb_code or self.kb_code or ""):
            return self
        return RetrievalFilter(
            kb_id=self.kb_id or kb_id,
            kb_code=self.kb_code or kb_code,
            doc_statuses=self.doc_statuses,
            is_active=self.is_active,
            is_visible=self.is_visible,
            commit_status=self.commit_status,
            compatible_schema_versions=self.compatible_schema_versions,
            source_backend=self.source_backend,
            uploaded_at_gte=self.uploaded_at_gte,
            uploaded_at_lte=self.uploaded_at_lte,
            committed_at_gte=self.committed_at_gte,
            committed_at_lte=self.committed_at_lte,
            visible_at_gte=self.visible_at_gte,
            visible_at_lte=self.visible_at_lte,
        )

    @property
    def has_doc_time_filters(self) -> bool:
        return any(
            value is not None
            for value in (
                self.uploaded_at_gte,
                self.uploaded_at_lte,
                self.committed_at_gte,
                self.committed_at_lte,
            )
        )

    @property
    def has_visible_time_filters(self) -> bool:
        return self.visible_at_gte is not None or self.visible_at_lte is not None
