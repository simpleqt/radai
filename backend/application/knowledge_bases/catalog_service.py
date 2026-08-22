"""MySQL-backed runtime knowledge-base catalog."""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence

from sqlalchemy import select

from backend.db.connection import get_async_session_factory
from backend.db.models.knowledge_base import KnowledgeBase, KnowledgeBaseStatus
from backend.domain.knowledge_base.catalog import KnowledgeBaseCatalogEntry

_catalog_cache: OrderedDict[str, tuple[float, list[KnowledgeBaseCatalogEntry]]] = (
    OrderedDict()
)
_catalog_cache_lock = threading.RLock()


def _catalog_cache_ttl_seconds() -> float:
    try:
        return max(
            1.0,
            float(os.getenv("KB_CATALOG_CACHE_TTL_SECONDS", "30") or "30"),
        )
    except (TypeError, ValueError):
        return 30.0


def _catalog_cache_max_entries() -> int:
    try:
        return max(16, int(os.getenv("KB_CATALOG_CACHE_MAX_ENTRIES", "256") or "256"))
    except (TypeError, ValueError):
        return 256


def _catalog_cache_key(
    normalized_ids: Sequence[str] | None,
    *,
    defaults_only: bool,
) -> str:
    if normalized_ids is None:
        id_part = "*"
    else:
        id_part = ",".join(normalized_ids) or "<empty>"
    return f"{id_part}:defaults={int(defaults_only)}"


def invalidate_knowledge_base_catalog_cache() -> None:
    """Drop all cached KB metadata after create/update/status/delete changes."""
    with _catalog_cache_lock:
        _catalog_cache.clear()


async def load_knowledge_base_catalog_cached(
    kb_ids: Sequence[str] | None = None,
    *,
    defaults_only: bool = False,
) -> list[KnowledgeBaseCatalogEntry]:
    """TTL-cached catalog loader for hot retrieval paths.

    Retrieval fans out per knowledge base and repeatedly needs the same
    code/name/backend metadata.  The original uncached loader remains for
    admin/management flows; this wrapper serves the hot path while keeping a
    bounded TTL so newly created or reconfigured KBs become visible quickly.
    """
    normalized_ids = list(
        dict.fromkeys(
            str(value).strip() for value in kb_ids or () if str(value).strip()
        )
    )
    if kb_ids is not None and not normalized_ids:
        return []
    key = _catalog_cache_key(
        None if kb_ids is None else normalized_ids,
        defaults_only=defaults_only,
    )
    now = time.monotonic()
    with _catalog_cache_lock:
        cached = _catalog_cache.get(key)
        if cached is not None and now - cached[0] <= _catalog_cache_ttl_seconds():
            _catalog_cache.move_to_end(key)
            return list(cached[1])

    entries = await load_knowledge_base_catalog(
        kb_ids,
        defaults_only=defaults_only,
    )
    with _catalog_cache_lock:
        _catalog_cache[key] = (time.monotonic(), list(entries))
        _catalog_cache.move_to_end(key)
        while len(_catalog_cache) > _catalog_cache_max_entries():
            _catalog_cache.popitem(last=False)
    return entries


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
        KnowledgeBase.ingestion_backend,
        KnowledgeBase.retrieval_backend,
        KnowledgeBase.self_ingestion_enabled,
        KnowledgeBase.self_retriever_enabled,
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
            ingestion_backend=getattr(row, "ingestion_backend", None),
            retrieval_backend=getattr(row, "retrieval_backend", None),
            self_ingestion_enabled=getattr(row, "self_ingestion_enabled", None),
            self_retriever_enabled=getattr(row, "self_retriever_enabled", None),
        )
        for row in rows
    ]
    if not normalized_ids:
        return entries
    by_id = {entry.kb_id: entry for entry in entries}
    return [by_id[kb_id] for kb_id in normalized_ids if kb_id in by_id]


async def resolve_knowledge_base_labels(kb_ids: Sequence[str]) -> list[str]:
    entries = await load_knowledge_base_catalog_cached(kb_ids)
    names = {entry.kb_id: entry.kb_name for entry in entries}
    return [names.get(str(kb_id), str(kb_id)) for kb_id in kb_ids]
