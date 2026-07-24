"""Shared knowledge-base aggregate statistics for self-ingestion flows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import KnowledgeBase, KnowledgeBaseDocument, SelfIngestChunk
from backend.domain.knowledge_base.backend_policy import SELF_BACKEND
from backend.rag.ingestion import states


async def refresh_knowledge_base_stats(
    session: AsyncSession,
    kb: KnowledgeBase,
) -> None:
    """Recalculate document, chunk, entity, and relation counts."""
    visible_doc_filter = (
        KnowledgeBaseDocument.kb_id == kb.kb_id,
        KnowledgeBaseDocument.storage_status == 1,
        or_(
            KnowledgeBaseDocument.doc_status.is_(None),
            KnowledgeBaseDocument.doc_status != states.DOC_DELETED,
        ),
    )
    await session.execute(
        update(KnowledgeBase)
        .where(KnowledgeBase.kb_id == kb.kb_id)
        .values(
            doc_count=(
                select(func.count(KnowledgeBaseDocument.id))
                .where(*visible_doc_filter)
                .scalar_subquery()
            ),
            entity_count=(
                select(func.coalesce(func.sum(KnowledgeBaseDocument.entity_count), 0))
                .where(*visible_doc_filter)
                .scalar_subquery()
            ),
            relation_count=(
                select(func.coalesce(func.sum(KnowledgeBaseDocument.relation_count), 0))
                .where(*visible_doc_filter)
                .scalar_subquery()
            ),
            chunk_count=(
                select(func.coalesce(func.sum(KnowledgeBaseDocument.chunk_count), 0))
                .where(
                    *visible_doc_filter,
                    or_(
                        KnowledgeBaseDocument.ingestion_backend.is_(None),
                        KnowledgeBaseDocument.ingestion_backend != SELF_BACKEND,
                    ),
                )
                .scalar_subquery()
                + select(func.count(SelfIngestChunk.id))
                .where(
                    SelfIngestChunk.kb_id == kb.kb_id,
                    SelfIngestChunk.is_active == True,
                    SelfIngestChunk.is_visible == True,
                    SelfIngestChunk.doc_status == states.DOC_VISIBLE,
                )
                .scalar_subquery()
            ),
            updated_at=datetime.now(),
        )
    )


__all__ = ["refresh_knowledge_base_stats"]
