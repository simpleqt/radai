"""MySQL-backed runtime knowledge-base catalog."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from backend.db.connection import get_async_session_factory
from backend.db.models.knowledge_base import KnowledgeBase, KnowledgeBaseStatus
from backend.domain.knowledge_base.catalog import KnowledgeBaseCatalogEntry


async def load_knowledge_base_catalog(
    kb_ids: Sequence[str] | None = None,
    *,
    defaults_only: bool = False,
) -> list[KnowledgeBaseCatalogEntry]:
    """Load active KB metadata in caller order, or DB order when unscoped."""
    normalized_ids = list(
        dict.fromkeys(
            str(value).strip()
            for value in kb_ids or ()
            if str(value).strip()
        )
    )
    if kb_ids is not None and not normalized_ids:
        return []
    stmt = select(
        KnowledgeBase.kb_id,
        KnowledgeBase.kb_code,
        KnowledgeBase.kb_name,
        KnowledgeBase.owner_id,
        KnowledgeBase.is_default,
        KnowledgeBase.status,
    ).where(KnowledgeBase.status == KnowledgeBaseStatus.ACTIVE)
    if normalized_ids:
        stmt = stmt.where(KnowledgeBase.kb_id.in_(normalized_ids))
    elif defaults_only:
        stmt = stmt.where(KnowledgeBase.is_default.is_(True))
    stmt = stmt.order_by(
        KnowledgeBase.is_default.desc(),
        KnowledgeBase.updated_at.desc(),
        KnowledgeBase.created_at.desc(),
    )

    factory = get_async_session_factory()
    async with factory() as session:
        rows = (await session.execute(stmt)).all()

    entries = [
        KnowledgeBaseCatalogEntry(
            kb_id=str(row.kb_id),
            kb_code=str(row.kb_code),
            kb_name=str(row.kb_name),
            owner_id=str(row.owner_id) if row.owner_id else None,
            is_default=bool(row.is_default),
            status=str(getattr(row.status, "value", row.status) or ""),
        )
        for row in rows
    ]
    if not normalized_ids:
        return entries
    by_id = {entry.kb_id: entry for entry in entries}
    return [by_id[kb_id] for kb_id in normalized_ids if kb_id in by_id]


async def resolve_knowledge_base_labels(kb_ids: Sequence[str]) -> list[str]:
    entries = await load_knowledge_base_catalog(kb_ids)
    names = {entry.kb_id: entry.kb_name for entry in entries}
    return [names.get(str(kb_id), str(kb_id)) for kb_id in kb_ids]
