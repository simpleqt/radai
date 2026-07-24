"""Delete/rollback helpers for self-ingestion data."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.models import (
    KnowledgeBase,
    KnowledgeBaseDocument,
    SelfIngestChunk,
)
from backend.rag.ingestion import states
from backend.rag.ingestion.kb_stats import refresh_knowledge_base_stats
from backend.rag.ingestion.storage.milvus_writer import MilvusChunkWriter, MilvusMediaWriter
from backend.rag.ingestion.storage.nebula_writer import NebulaGraphV2Writer
from backend.rag.ingestion.storage.redis_writer import delete_doc_from_corpus

logger = get_logger(__name__)

# Reuse the documented "failed" storage state while doc_status distinguishes
# deleted documents from ingestion failures; this avoids a schema migration.
DELETE_CLEANUP_PENDING = 2


class SelfDeleteService:
    """Soft-delete self-ingestion documents without touching legacy indexes."""

    async def soft_delete(
        self,
        session: AsyncSession,
        *,
        kb_id: str,
        doc_id: str,
    ) -> dict:
        now = datetime.now()
        kb = (
            await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id)
            )
        ).scalar_one_or_none()
        if not kb:
            raise ValueError(f"知识库不存在: {kb_id}")

        doc = (
            await session.execute(
                select(KnowledgeBaseDocument).where(
                    KnowledgeBaseDocument.kb_id == kb_id,
                    KnowledgeBaseDocument.doc_id == doc_id,
                )
            )
        ).scalar_one_or_none()
        if not doc:
            raise ValueError(f"文档不存在: {doc_id}")

        chunk_rows = (
            await session.execute(
                select(SelfIngestChunk.chunk_id).where(
                    SelfIngestChunk.kb_id == kb_id,
                    SelfIngestChunk.doc_id == doc_id,
                    SelfIngestChunk.is_active.is_(True),
                )
            )
        ).all()
        chunk_ids = [row[0] for row in chunk_rows]
        await session.execute(
            update(KnowledgeBaseDocument)
            .where(
                KnowledgeBaseDocument.kb_id == kb_id,
                KnowledgeBaseDocument.doc_id == doc_id,
            )
            .values(
                doc_status=states.DOC_DELETED,
                storage_status=DELETE_CLEANUP_PENDING,
                deleted_at=now,
                updated_at=now,
            )
        )
        await session.execute(
            update(SelfIngestChunk)
            .where(SelfIngestChunk.kb_id == kb_id, SelfIngestChunk.doc_id == doc_id)
            .values(
                is_active=False,
                is_visible=False,
                doc_status=states.DOC_DELETED,
                deleted_at=now,
                updated_at=now,
            )
        )
        # The database is the authoritative visibility boundary.  Commit the
        # soft-delete before touching Redis/Milvus/Nebula so an external
        # cleanup failure can never roll the document back to "visible" after
        # some indexes have already deleted it.
        await self._refresh_kb_stats(session, kb)
        await session.commit()

        redis_deleted, milvus_deleted, nebula_hidden = await self._cleanup_external(
            kb,
            doc_id,
            chunk_ids,
        )
        cleanup_failures: list[str] = []
        if settings.redis_host and not redis_deleted:
            cleanup_failures.append("redis")
        if settings.milvus_connection_uri and not milvus_deleted:
            cleanup_failures.append("milvus")
        if kb.nebula_space and (doc_id or chunk_ids) and not nebula_hidden:
            cleanup_failures.append("nebula")
        if cleanup_failures:
            raise RuntimeError(f"自研入库外部索引清理失败: {', '.join(cleanup_failures)}")
        await session.execute(
            update(KnowledgeBaseDocument)
            .where(
                KnowledgeBaseDocument.kb_id == kb_id,
                KnowledgeBaseDocument.doc_id == doc_id,
                KnowledgeBaseDocument.doc_status == states.DOC_DELETED,
            )
            .values(storage_status=0, updated_at=datetime.now())
        )
        await session.commit()
        return {
            "success": True,
            "doc_id": doc_id,
            "chunks": len(chunk_ids),
            "redis_deleted": redis_deleted,
            "milvus_deleted": milvus_deleted,
            "nebula_hidden": nebula_hidden,
            "nebula_relations_hidden": nebula_hidden,
        }

    @staticmethod
    def _cleanup_timeout_seconds() -> float:
        try:
            return max(
                1.0,
                float(os.getenv("SELF_DELETE_CLEANUP_TIMEOUT_SECONDS", "30") or "30"),
            )
        except (TypeError, ValueError):
            return 30.0

    async def _bounded_cleanup(
        self,
        label: str,
        operation: Callable[[], Awaitable[bool]],
    ) -> bool:
        try:
            attempts = max(
                1,
                int(os.getenv("SELF_DELETE_CLEANUP_ATTEMPTS", "3") or "3"),
            )
        except (TypeError, ValueError):
            attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                if await asyncio.wait_for(
                    operation(),
                    timeout=self._cleanup_timeout_seconds(),
                ):
                    return True
            except asyncio.TimeoutError:
                logger.warning(
                    "[self-ingestion] %s 清理超时: attempt=%d/%d",
                    label,
                    attempt,
                    attempts,
                )
            except Exception:
                logger.exception(
                    "[self-ingestion] %s 清理异常: attempt=%d/%d",
                    label,
                    attempt,
                    attempts,
                )
            if attempt < attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 5))
        return False

    async def _cleanup_external(
        self,
        kb: KnowledgeBase,
        doc_id: str,
        chunk_ids: list[str],
    ) -> tuple[bool, bool, bool]:
        async def completed_noop() -> bool:
            return True

        async def delete_redis() -> bool:
            return await self._delete_redis_doc(kb.kb_code, doc_id, chunk_ids)

        async def delete_milvus() -> bool:
            return await self._delete_milvus_doc(kb.kb_code, doc_id)

        async def hide_nebula() -> bool:
            return await self._hide_nebula_doc_graph(kb, doc_id, chunk_ids)

        redis_operation: Callable[[], Awaitable[bool]] = (
            delete_redis if settings.redis_host else completed_noop
        )
        milvus_operation: Callable[[], Awaitable[bool]] = (
            delete_milvus if settings.milvus_connection_uri else completed_noop
        )
        nebula_operation: Callable[[], Awaitable[bool]] = (
            hide_nebula
            if kb.nebula_space and (doc_id or chunk_ids)
            else completed_noop
        )

        return tuple(
            await asyncio.gather(
                self._bounded_cleanup("Redis", redis_operation),
                self._bounded_cleanup("Milvus", milvus_operation),
                self._bounded_cleanup("Nebula", nebula_operation),
            )
        )

    async def retry_pending_deletes(
        self,
        session: AsyncSession,
        *,
        limit: int = 20,
    ) -> dict[str, int]:
        """Retry durable external cleanup markers left by interrupted deletes."""
        rows = (
            await session.execute(
                select(KnowledgeBaseDocument, KnowledgeBase)
                .join(
                    KnowledgeBase,
                    KnowledgeBaseDocument.kb_id == KnowledgeBase.kb_id,
                )
                .where(
                    KnowledgeBaseDocument.doc_status == states.DOC_DELETED,
                    KnowledgeBaseDocument.storage_status == DELETE_CLEANUP_PENDING,
                )
                .order_by(KnowledgeBaseDocument.updated_at.asc())
                .limit(max(1, int(limit)))
            )
        ).all()
        repaired = 0
        for doc, kb in rows:
            chunk_ids = list(
                (
                    await session.execute(
                        select(SelfIngestChunk.chunk_id).where(
                            SelfIngestChunk.kb_id == doc.kb_id,
                            SelfIngestChunk.doc_id == doc.doc_id,
                        )
                    )
                ).scalars().all()
            )
            redis_ok, milvus_ok, nebula_ok = await self._cleanup_external(
                kb,
                doc.doc_id,
                chunk_ids,
            )
            complete = (
                (not settings.redis_host or redis_ok)
                and (not settings.milvus_connection_uri or milvus_ok)
                and (not kb.nebula_space or nebula_ok)
            )
            if complete:
                await session.execute(
                    update(KnowledgeBaseDocument)
                    .where(
                        KnowledgeBaseDocument.kb_id == doc.kb_id,
                        KnowledgeBaseDocument.doc_id == doc.doc_id,
                        KnowledgeBaseDocument.storage_status
                        == DELETE_CLEANUP_PENDING,
                    )
                    .values(storage_status=0, updated_at=datetime.now())
                )
                await session.commit()
                repaired += 1
        return {"scanned": len(rows), "repaired": repaired}

    async def _hide_nebula_doc_graph(
        self,
        kb: KnowledgeBase,
        doc_id: str,
        chunk_ids: list[str],
    ) -> bool:
        """Hide NebulaGraph v2 graph facts that came from the deleted document."""
        if not kb.nebula_space or (not doc_id and not chunk_ids):
            return False
        try:
            return await NebulaGraphV2Writer(kb.nebula_space).hide_doc_graph(
                doc_id=doc_id,
                chunk_ids=chunk_ids,
            )
        except Exception as exc:
            logger.warning(
                "[self-ingestion] Nebula 文档图谱隐藏失败: kb=%s doc=%s chunks=%d error=%s",
                kb.kb_code, doc_id, len(chunk_ids), exc,
            )
            return False

    async def _refresh_kb_stats(
        self, session: AsyncSession, kb: KnowledgeBase,
    ) -> None:
        """Recalculate knowledge base aggregate counts after a doc delete."""
        await refresh_knowledge_base_stats(session, kb)

    async def _refresh_extraction_summaries(
        self,
        session: AsyncSession,
        *,
        kb_id: str,
        entity_ids: set[str],
        relation_ids: set[str],
        now: datetime,
    ) -> None:
        """Deprecated no-op: graph summaries live in NebulaGraph v2 only."""
        return

    async def _delete_redis_doc(
        self,
        kb_code: str,
        doc_id: str,
        chunk_ids: list[str],
    ) -> bool:
        if not settings.redis_host:
            return True
        try:
            await delete_doc_from_corpus(kb_code, doc_id, chunk_ids)
            return True
        except Exception as exc:
            logger.warning("[self-ingestion] Redis 删除标记失败: doc=%s error=%s", doc_id, exc)
            return False

    async def _delete_milvus_doc(self, kb_code: str, doc_id: str) -> bool:
        if not settings.milvus_connection_uri:
            return False
        try:
            chunk_deleted, media_deleted = await asyncio.gather(
                MilvusChunkWriter(kb_code).delete_doc(doc_id),
                MilvusMediaWriter(kb_code).delete_doc(doc_id),
            )
            return bool(chunk_deleted or media_deleted)
        except Exception as exc:
            logger.warning("[self-ingestion] Milvus 删除失败: doc=%s error=%s", doc_id, exc)
            return False


self_delete_service = SelfDeleteService()
