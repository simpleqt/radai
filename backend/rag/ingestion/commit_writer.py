"""CommitWriter state machine (§14) -- the sole writer to MySQL/Milvus/Nebula/Redis.

Takes a ``StagingPayload`` as its only input and walks through an ordered
sequence of storage stages.  Each stage is idempotent so that partial failures
can be replayed by the repair worker from the same payload.

Stage order (§14.2):
  1. COMMITTING_MYSQL   -- upsert chunks/doc metadata and audits (invisible)
  2. COMMITTING_MILVUS  -- upsert chunk + entity vectors (invisible)
  3. MARKING_VISIBLE    -- MySQL mark visible, supersede old version
  4. MILVUS_VISIBLE     -- Milvus mark_doc_visible
  5. MILVUS_ENTITIES    -- Milvus entities_v2
  6. COMMITTING_REDIS   -- BM25 corpus, alias, time keys
  7. COMMITTING_NEBULA  -- entity/relation/evidence graph

If stages 4-7 fail, the job becomes ``partial_success`` and the repair
worker can resume from the stored staging_uri.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.config import settings
from backend.config.loggers import get_logger
from backend.db.connection import retry_async_db_operation
from backend.rag.ingestion import states
from backend.rag.ingestion.concurrency import commit_mysql_concurrency, semaphore
from backend.rag.ingestion.embedding import (
    embed_media_texts,
    embed_texts,
    require_embedding_success,
)
from backend.rag.ingestion.fencing import IngestFenceLostError
from backend.rag.ingestion.staging_manager import StagingPayload
from backend.rag.ingestion.storage.milvus_writer import (
    MilvusChunkWriter,
    MilvusEntityWriter,
    MilvusMediaWriter,
)
from backend.rag.ingestion.storage.nebula_writer import NebulaGraphV2Writer
from backend.rag.ingestion.storage.redis_writer import write_chunk_corpus

logger = get_logger(__name__)


def visible_entities_from_extraction(extraction) -> list:
    if extraction is None:
        return []
    return [
        entity
        for entity in extraction.entities.values()
        if getattr(entity, "status", states.ENTITY_ACTIVE) in states.RETRIEVAL_ENTITY_STATUSES
    ]


def entity_embed_texts(entities) -> list[str]:
    return [
        f"{entity.name}\n{entity.normalized_name}\n{entity.entity_type}"
        for entity in entities
    ]


def media_chunks_from_chunks(chunks) -> list:
    return [
        chunk
        for chunk in chunks
        if (chunk.block_type or "").lower() in {"image", "table", "formula"}
    ]


def _commit_mysql_retries() -> int:
    try:
        return max(int(getattr(settings, "self_ingestion_commit_mysql_retries", 8) or 8), 1)
    except (TypeError, ValueError):
        return 8


def _commit_mysql_retry_base_delay() -> float:
    try:
        return max(float(getattr(settings, "self_ingestion_commit_mysql_retry_base_delay", 1.0) or 1.0), 0.1)
    except (TypeError, ValueError):
        return 1.0


@dataclass
class CommitResult:
    """Outcome of a single CommitWriter run."""
    success: bool = False
    partial: bool = False
    vector_count: int = 0
    entity_vector_count: int = 0
    media_vector_count: int = 0
    external_failures: list[str] = field(default_factory=list)
    stage_log: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def final_status(self) -> str:
        if self.success and not self.external_failures:
            return states.JOB_COMPLETED
        if self.partial or self.external_failures:
            return states.JOB_PARTIAL_SUCCESS
        return states.JOB_FAILED

    @property
    def final_stage(self) -> str:
        if self.success and not self.external_failures:
            return states.STAGE_COMMITTED
        return states.STAGE_MARKING_VISIBLE


class CommitWriter:
    """Deterministic multi-store writer driven entirely by ``StagingPayload``.

    Usage::

        writer = CommitWriter()
        result = await writer.commit(payload, kb=kb, pending_file=pf)
    """

    async def commit(
        self,
        payload: StagingPayload,
        *,
        kb: Any,
        pending_file: Any,
        set_stage_fn=None,
        log_stage_fn=None,
        fence_check=None,
    ) -> CommitResult:
        """Execute the full commit sequence.

        Args:
            payload: The staging payload (chunks, extraction, vectors, etc.)
            kb: KnowledgeBase ORM instance (for kb_code, nebula_space, etc.)
            pending_file: PendingFile ORM instance (for created_at, etc.)
            set_stage_fn: Optional async callable(stage: str) to update job stage
            log_stage_fn: Optional callable(stage, status, **kw) for commit log
            fence_check: Optional async callback rejecting stale workers
        """
        result = CommitResult()

        async def _fence() -> None:
            if fence_check is not None:
                await fence_check()

        async def _set(stage: str) -> None:
            if set_stage_fn:
                await set_stage_fn(stage)

        def _log(stage: str, status: str, **kw) -> None:
            entry = {"stage": stage, "status": status, "at": datetime.now().isoformat()}
            entry.update(kw)
            result.stage_log.append(entry)
            if log_stage_fn:
                log_stage_fn(stage, status, **kw)

        chunks = payload.chunks
        vectors = list(payload.vectors or [])
        media_chunks = media_chunks_from_chunks(chunks)
        media_vectors = list(payload.media_vectors or [])
        extraction = payload.extraction
        embedding_required = require_embedding_success()
        if not vectors or len(vectors) != len(chunks):
            if vectors and len(vectors) != len(chunks):
                _log(
                    states.STAGE_EMBEDDING,
                    "degraded",
                    detail=f"vector/chunk count mismatch: vectors={len(vectors)} chunks={len(chunks)}",
                )
            try:
                vectors = await embed_texts([chunk.content for chunk in chunks])
            except Exception as exc:
                logger.warning(
                    "[self-ingestion] commit 阶段 chunk 重嵌入失败: doc=%s error=%s",
                    payload.doc_id,
                    exc,
                )
                if embedding_required:
                    raise RuntimeError(f"commit 阶段 chunk 重嵌入失败: {exc}") from exc
                vectors = []
                result.external_failures.append("milvus_vector_count_mismatch")
            else:
                if len(vectors) != len(chunks):
                    if embedding_required:
                        raise RuntimeError(
                            f"commit 阶段 chunk embedding 数量不齐: chunks={len(chunks)} vectors={len(vectors)}"
                        )
                    vectors = []
                    result.external_failures.append("milvus_vector_count_mismatch")

        if media_chunks and (not media_vectors or len(media_vectors) != len(media_chunks)):
            try:
                media_vectors = await embed_media_texts([chunk.content for chunk in media_chunks])
            except Exception as exc:
                logger.warning(
                    "[self-ingestion] commit 阶段 media 重嵌入失败: doc=%s error=%s",
                    payload.doc_id,
                    exc,
                )
                media_vectors = []
                result.external_failures.append("milvus_media_embeddings")
            else:
                if media_vectors and len(media_vectors) != len(media_chunks):
                    logger.warning(
                        "[self-ingestion] media embedding 数量不齐 chunks=%d vectors=%d，跳过 media_vectors_v2",
                        len(media_chunks),
                        len(media_vectors),
                    )
                    media_vectors = []
                    result.external_failures.append("milvus_media_embeddings")

        try:
            await _fence()
            # ---- Stage 1: COMMITTING_MYSQL (invisible) ----
            await _set(states.STAGE_COMMITTING_MYSQL)
            t0 = datetime.now()
            commit_sem = await semaphore(
                "self_ingestion_commit_mysql",
                commit_mysql_concurrency(),
            )
            async with commit_sem:
                await retry_async_db_operation(
                    lambda: self._commit_mysql(payload, kb=kb, pending_file=pending_file),
                    attempts=_commit_mysql_retries(),
                    base_delay=_commit_mysql_retry_base_delay(),
                    label=f"self-ingestion commit_mysql doc_id={payload.doc_id}",
                    logger=logger,
                )
            await _fence()
            _log(states.STAGE_COMMITTING_MYSQL, "ok",
                 ms=int((datetime.now() - t0).total_seconds() * 1000))

            # ---- Stage 2: COMMITTING_MILVUS (invisible) ----
            await _set(states.STAGE_COMMITTING_MILVUS)
            t0 = datetime.now()
            result.vector_count = (
                await MilvusChunkWriter(kb.kb_code).upsert_chunks(
                    chunks, vectors,
                    kb_id=payload.kb_id, kb_code=payload.kb_code, visible=False,
                )
                if vectors else 0
            )
            result.media_vector_count = (
                await MilvusMediaWriter(kb.kb_code).upsert_media(
                    media_chunks, media_vectors,
                    kb_id=payload.kb_id, kb_code=payload.kb_code, visible=False,
                )
                if media_vectors else 0
            )
            await _fence()
            if (
                vectors
                and settings.milvus_connection_uri
                and result.vector_count != len(chunks)
            ):
                result.external_failures.append("milvus_chunks")
            if (
                media_vectors
                and settings.milvus_connection_uri
                and result.media_vector_count != len(media_chunks)
            ):
                result.external_failures.append("milvus_media")
            _log(states.STAGE_COMMITTING_MILVUS,
                 "ok" if result.vector_count else ("failed" if vectors and settings.milvus_connection_uri else "skipped"),
                 vectors=result.vector_count,
                 media_vectors=result.media_vector_count,
                 ms=int((datetime.now() - t0).total_seconds() * 1000))

            # ---- Stage 3: MARKING_VISIBLE (MySQL) ----
            await _set(states.STAGE_MARKING_VISIBLE)
            t0 = datetime.now()
            commit_sem = await semaphore(
                "self_ingestion_commit_mysql",
                commit_mysql_concurrency(),
            )
            async with commit_sem:
                await retry_async_db_operation(
                    lambda: self._mark_visible_mysql(
                        payload,
                        kb=kb,
                        pending_file=pending_file,
                        vector_count=result.vector_count,
                    ),
                    attempts=_commit_mysql_retries(),
                    base_delay=_commit_mysql_retry_base_delay(),
                    label=f"self-ingestion mark_visible_mysql doc_id={payload.doc_id}",
                    logger=logger,
                )
            await _fence()
            _log(states.STAGE_MARKING_VISIBLE, "ok",
                 ms=int((datetime.now() - t0).total_seconds() * 1000))
            result.success = True

        except IngestFenceLostError:
            raise
        except Exception as exc:
            result.error = str(exc)[:500]
            _log(states.STAGE_FAILED, "failed", detail=result.error)
            return result

        # ---- Stages 4-7: external index visibility (partial failure tolerated) ----
        result.partial = True

        async def _stage_milvus_visible() -> None:
            await _fence()
            await _set(states.STAGE_COMMITTING_MILVUS + "_visible")
            t0 = datetime.now()
            if not result.vector_count and not result.media_vector_count:
                _log("milvus_mark_visible", "skipped", ms=int((datetime.now() - t0).total_seconds() * 1000))
                return
            try:
                chunk_ids = [chunk.chunk_id for chunk in chunks if chunk.chunk_id]
                hidden_old = True
                visible_ok = True
                if result.vector_count:
                    writer = MilvusChunkWriter(kb.kb_code)
                    hidden_old = await writer.hide_doc(payload.doc_id)
                    visible_ok = await writer.mark_doc_visible(payload.doc_id, chunk_ids=chunk_ids)
                media_visible_ok = True
                if result.media_vector_count:
                    media_writer = MilvusMediaWriter(kb.kb_code)
                    media_chunk_ids = [chunk.chunk_id for chunk in media_chunks if chunk.chunk_id]
                    media_hidden_old = await media_writer.hide_doc(payload.doc_id)
                    media_visible_ok = await media_writer.mark_doc_visible(
                        payload.doc_id, chunk_ids=media_chunk_ids,
                    )
                    media_visible_ok = bool(media_hidden_old and media_visible_ok)
                if hidden_old and visible_ok and media_visible_ok:
                    _log(
                        "milvus_mark_visible",
                        "ok",
                        chunks=len(chunk_ids),
                        media_chunks=len(media_chunks) if result.media_vector_count else 0,
                        ms=int((datetime.now() - t0).total_seconds() * 1000),
                    )
                else:
                    result.external_failures.append("milvus_visible")
                    _log(
                        "milvus_mark_visible",
                        "failed",
                        detail=f"hide_doc={hidden_old} mark_doc_visible={visible_ok}",
                        ms=int((datetime.now() - t0).total_seconds() * 1000),
                    )
                await _fence()
            except IngestFenceLostError:
                raise
            except Exception as exc:
                result.external_failures.append("milvus_visible")
                _log(
                    "milvus_mark_visible",
                    "failed",
                    detail=str(exc)[:200],
                    ms=int((datetime.now() - t0).total_seconds() * 1000),
                )

        async def _stage_milvus_entities() -> None:
            await _fence()
            await _set(states.STAGE_COMMITTING_MILVUS + "_entities")
            t0 = datetime.now()
            if not extraction:
                _log("milvus_entities_v2", "skipped", ms=int((datetime.now() - t0).total_seconds() * 1000))
                return
            try:
                visible_entities = visible_entities_from_extraction(extraction)
                if not visible_entities:
                    _log("milvus_entities_v2", "skipped", ms=int((datetime.now() - t0).total_seconds() * 1000))
                    return
                entity_vectors = list(payload.entity_vectors or [])
                if not entity_vectors or len(entity_vectors) != len(visible_entities):
                    entity_vectors = await embed_texts(entity_embed_texts(visible_entities))
                if len(entity_vectors) != len(visible_entities):
                    logger.warning(
                        "[self-ingestion] entity 向量数量不齐 entities=%d vectors=%d，跳过 entities_v2",
                        len(visible_entities),
                        len(entity_vectors),
                    )
                    result.external_failures.append("milvus_entities")
                    _log(
                        "milvus_entities_v2",
                        "failed",
                        detail="entity vector count mismatch",
                        ms=int((datetime.now() - t0).total_seconds() * 1000),
                    )
                    return
                result.entity_vector_count = await MilvusEntityWriter(kb.kb_code).upsert_entities(
                    visible_entities,
                    entity_vectors,
                    kb_id=payload.kb_id,
                    kb_code=payload.kb_code,
                    visible=True,
                )
                await _fence()
                if (
                    settings.milvus_connection_uri
                    and result.entity_vector_count != len(visible_entities)
                ):
                    result.external_failures.append("milvus_entities")
                _log(
                    "milvus_entities_v2",
                    "ok" if result.entity_vector_count else (
                        "failed" if settings.milvus_connection_uri else "skipped"
                    ),
                    vectors=result.entity_vector_count,
                    ms=int((datetime.now() - t0).total_seconds() * 1000),
                )
            except IngestFenceLostError:
                raise
            except Exception as exc:
                result.external_failures.append("milvus_entities")
                _log(
                    "milvus_entities_v2",
                    "failed",
                    detail=str(exc)[:200],
                    ms=int((datetime.now() - t0).total_seconds() * 1000),
                )

        async def _stage_redis() -> None:
            await _fence()
            await _set(states.STAGE_COMMITTING_REDIS)
            t0 = datetime.now()
            try:
                now = datetime.now()
                await write_chunk_corpus(
                    kb.kb_code,
                    payload.doc_id,
                    chunks,
                    extraction=extraction,
                    uploaded_at=getattr(pending_file, "created_at", now),
                    committed_at=now,
                    visible_at=now,
                )
                await _fence()
                _log(
                    states.STAGE_COMMITTING_REDIS,
                    "ok",
                    ms=int((datetime.now() - t0).total_seconds() * 1000),
                )
            except IngestFenceLostError:
                raise
            except Exception as exc:
                result.external_failures.append("redis")
                _log(
                    states.STAGE_COMMITTING_REDIS,
                    "failed",
                    detail=str(exc)[:200],
                    ms=int((datetime.now() - t0).total_seconds() * 1000),
                )

        async def _stage_nebula() -> None:
            await _fence()
            await _set(states.STAGE_COMMITTING_NEBULA)
            t0 = datetime.now()
            graph_items = bool(extraction and (extraction.entities or extraction.relations))
            if not graph_items or not getattr(kb, "nebula_space", None):
                _log(states.STAGE_COMMITTING_NEBULA, "skipped", ms=int((datetime.now() - t0).total_seconds() * 1000))
                return
            try:
                nebula_ok = await NebulaGraphV2Writer(kb.nebula_space).upsert_graph(extraction, visible=True)
                await _fence()
                if nebula_ok:
                    _log(
                        states.STAGE_COMMITTING_NEBULA,
                        "ok",
                        ms=int((datetime.now() - t0).total_seconds() * 1000),
                    )
                else:
                    result.external_failures.append("nebula")
                    _log(
                        states.STAGE_COMMITTING_NEBULA,
                        "failed",
                        detail="upsert_graph returned False",
                        ms=int((datetime.now() - t0).total_seconds() * 1000),
                    )
            except IngestFenceLostError:
                raise
            except Exception as exc:
                result.external_failures.append("nebula")
                _log(
                    states.STAGE_COMMITTING_NEBULA,
                    "failed",
                    detail=str(exc)[:200],
                    ms=int((datetime.now() - t0).total_seconds() * 1000),
                )

        if (
            fence_check is None
            and getattr(settings, "self_ingestion_commit_external_parallel", True)
        ):
            await asyncio.gather(
                _stage_milvus_visible(),
                _stage_milvus_entities(),
                _stage_redis(),
                _stage_nebula(),
            )
        else:
            await _stage_milvus_visible()
            await _stage_milvus_entities()
            await _stage_redis()
            await _stage_nebula()

        result.partial = bool(result.external_failures)
        return result

    async def replay_from_stage(
        self,
        payload: StagingPayload,
        *,
        kb: Any,
        pending_file: Any = None,
        failed_stages: list[str],
    ) -> CommitResult:
        """Replay only the failed stages from a previously stored payload.

        Used by repair_worker to resume from staging_uri.
        """
        result = CommitResult(success=True)
        extraction = payload.extraction
        chunks = payload.chunks
        media_chunks = media_chunks_from_chunks(chunks)

        stage_aliases = {
            # Earlier commit phases report the reason, while replay needs the
            # component that can repair it.  Keep the original name in the
            # result so repair_worker can remove it from external_failures.
            "milvus_vector_count_mismatch": "milvus_chunks",
            "milvus_media_embeddings": "milvus_media",
            "media_vectors": "milvus_media",
        }

        for requested_stage in failed_stages:
            stage_name = stage_aliases.get(requested_stage, requested_stage)
            try:
                if stage_name == "milvus_chunks":
                    if not payload.vectors or len(payload.vectors) != len(chunks):
                        raise RuntimeError("missing or mismatched vectors for Milvus chunk replay")
                    inserted = await MilvusChunkWriter(kb.kb_code).upsert_chunks(
                        chunks, payload.vectors,
                        kb_id=payload.kb_id, kb_code=payload.kb_code, visible=True,
                    )
                    if settings.milvus_connection_uri and inserted != len(chunks):
                        raise RuntimeError(f"Milvus chunk replay inserted {inserted}/{len(chunks)}")
                    result.vector_count = inserted
                    result.stage_log.append({
                        "stage": requested_stage,
                        "component": stage_name,
                        "status": "repaired",
                    })

                elif stage_name == "milvus_visible":
                    writer = MilvusChunkWriter(kb.kb_code)
                    chunk_ids = [chunk.chunk_id for chunk in chunks if chunk.chunk_id]
                    hidden_old = await writer.hide_doc(payload.doc_id)
                    visible_ok = await writer.mark_doc_visible(payload.doc_id, chunk_ids=chunk_ids)
                    if not hidden_old or not visible_ok:
                        raise RuntimeError(
                            f"Milvus visibility replay failed: hide_doc={hidden_old} "
                            f"mark_doc_visible={visible_ok}"
                        )
                    result.stage_log.append({
                        "stage": requested_stage,
                        "component": stage_name,
                        "status": "repaired",
                    })

                elif stage_name in {"milvus_media", "media_vectors"}:
                    media_vectors = list(payload.media_vectors or [])
                    if media_chunks and (not media_vectors or len(media_vectors) != len(media_chunks)):
                        media_vectors = await embed_media_texts([chunk.content for chunk in media_chunks])
                    if media_chunks and len(media_vectors) != len(media_chunks):
                        raise RuntimeError(
                            f"media vector replay mismatch: vectors={len(media_vectors)} "
                            f"media_chunks={len(media_chunks)}"
                        )
                    if media_chunks:
                        result.media_vector_count = await MilvusMediaWriter(kb.kb_code).upsert_media(
                            media_chunks, media_vectors,
                            kb_id=payload.kb_id, kb_code=payload.kb_code, visible=True,
                        )
                        if (
                            settings.milvus_connection_uri
                            and result.media_vector_count != len(media_chunks)
                        ):
                            raise RuntimeError(
                                f"Milvus media replay inserted {result.media_vector_count}/{len(media_chunks)}"
                            )
                    result.stage_log.append({
                        "stage": requested_stage,
                        "component": stage_name,
                        "status": "repaired",
                    })

                elif stage_name == "milvus_entities" and extraction:
                    visible_entities = visible_entities_from_extraction(extraction)
                    if visible_entities:
                        entity_vectors = list(payload.entity_vectors or [])
                        if not entity_vectors or len(entity_vectors) != len(visible_entities):
                            entity_vectors = await embed_texts(entity_embed_texts(visible_entities))
                        if len(entity_vectors) != len(visible_entities):
                            raise RuntimeError(
                                f"entity vector replay mismatch: vectors={len(entity_vectors)} "
                                f"entities={len(visible_entities)}"
                            )
                        result.entity_vector_count = await MilvusEntityWriter(kb.kb_code).upsert_entities(
                            visible_entities, entity_vectors,
                            kb_id=payload.kb_id, kb_code=payload.kb_code, visible=True,
                        )
                        if (
                            settings.milvus_connection_uri
                            and result.entity_vector_count != len(visible_entities)
                        ):
                            raise RuntimeError(
                                f"Milvus entity replay inserted {result.entity_vector_count}/{len(visible_entities)}"
                            )
                    result.stage_log.append({
                        "stage": requested_stage,
                        "component": stage_name,
                        "status": "repaired",
                    })

                elif stage_name == "redis":
                    now = datetime.now()
                    uploaded_at = getattr(pending_file, "created_at", now) if pending_file else now
                    await write_chunk_corpus(
                        kb.kb_code, payload.doc_id, chunks,
                        extraction=extraction,
                        uploaded_at=uploaded_at, committed_at=now, visible_at=now,
                    )
                    result.stage_log.append({
                        "stage": requested_stage,
                        "component": stage_name,
                        "status": "repaired",
                    })

                elif stage_name == "nebula" and extraction:
                    nebula_ok = await NebulaGraphV2Writer(kb.nebula_space).upsert_graph(extraction, visible=True)
                    if not nebula_ok:
                        raise RuntimeError("upsert_graph returned False")
                    result.stage_log.append({
                        "stage": requested_stage,
                        "component": stage_name,
                        "status": "repaired",
                    })

                else:
                    result.stage_log.append({"stage": requested_stage, "status": "skipped"})

            except Exception as exc:
                result.external_failures.append(requested_stage)
                result.stage_log.append({
                    "stage": requested_stage,
                    "component": stage_name,
                    "status": "failed",
                    "error": str(exc)[:200],
                })

        return result

    # ---- Internal MySQL helpers ----

    async def _commit_mysql(self, payload: StagingPayload, *, kb, pending_file) -> None:
        """Write chunks/doc metadata to MySQL with is_visible=False.

        Entity/relation graph data is stored in NebulaGraph v2 only.
        """
        from sqlalchemy import delete

        from backend.db.connection import async_session_context
        from backend.db.models import (
            SelfIngestChunk,
        )

        now = datetime.now()
        async with async_session_context() as session:
            await session.execute(
                delete(SelfIngestChunk).where(
                    SelfIngestChunk.kb_id == payload.kb_id,
                    SelfIngestChunk.doc_id == payload.doc_id,
                    SelfIngestChunk.commit_version == payload.commit_version,
                )
            )

            for chunk in payload.chunks:
                session.add(SelfIngestChunk(
                    kb_id=payload.kb_id,
                    doc_id=payload.doc_id,
                    chunk_id=chunk.chunk_id,
                    job_id=payload.job_id,
                    file_name=chunk.file_name or getattr(pending_file, "file_name", ""),
                    file_path=chunk.file_path or getattr(pending_file, "minio_path", None),
                    chunk_index=chunk.chunk_index,
                    block_type=chunk.block_type,
                    content=chunk.content,
                    content_hash=chunk.content_hash,
                    stable_chunk_fingerprint=chunk.stable_chunk_fingerprint,
                    token_count=chunk.token_count,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    source_hash=payload.source_document.source_hash if payload.source_document else "",
                    document_date=chunk.document_date,
                    category_path=chunk.category_path,
                    commit_version=payload.commit_version,
                    is_visible=False,
                    is_active=True,
                    doc_status=states.DOC_STAGED,
                    embedding_status=states.EMBEDDING_PENDING,
                    extra_json={
                        **dict(chunk.extra or {}),
                        "block_id": chunk.block_id,
                        "page_num": chunk.page_num,
                        "bbox": chunk.bbox,
                        "section_path": list(chunk.section_path or []),
                        "block_type": chunk.block_type,
                    },
                    created_at=now,
                    updated_at=now,
                ))

            await session.commit()

    async def _mark_visible_mysql(
        self, payload: StagingPayload, *, kb, pending_file, vector_count: int,
    ) -> None:
        from sqlalchemy import select
        from sqlalchemy import update as sql_update

        from backend.db.connection import async_session_context
        from backend.db.models import (
            KnowledgeBaseDocument,
            SelfIngestChunk,
            SelfIngestJob,
        )
        from backend.db.models import (
            PendingFile as PF,
        )

        now = datetime.now()
        async with async_session_context() as session:
            existing_doc = (
                await session.execute(
                    select(KnowledgeBaseDocument)
                    .where(
                        KnowledgeBaseDocument.kb_id == payload.kb_id,
                        KnowledgeBaseDocument.doc_id == payload.doc_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing_doc and (
                int(existing_doc.commit_version or 0) != int(payload.commit_version)
                or existing_doc.latest_job_id != payload.job_id
            ):
                raise RuntimeError(
                    "stale commit reservation: "
                    f"doc={payload.doc_id} payload_job={payload.job_id} "
                    f"reserved_job={existing_doc.latest_job_id} "
                    f"payload_version={payload.commit_version} "
                    f"reserved_version={existing_doc.commit_version}"
                )

            # Supersede old version chunks
            await session.execute(
                sql_update(SelfIngestChunk)
                .where(
                    SelfIngestChunk.kb_id == payload.kb_id,
                    SelfIngestChunk.doc_id == payload.doc_id,
                    SelfIngestChunk.is_active == True,
                    SelfIngestChunk.commit_version < payload.commit_version,
                )
                .values(
                    is_active=False,
                    is_visible=False,
                    doc_status=states.DOC_SUPERSEDED,
                    updated_at=now,
                )
            )

            # Mark new chunks visible
            await session.execute(
                sql_update(SelfIngestChunk)
                .where(
                    SelfIngestChunk.kb_id == payload.kb_id,
                    SelfIngestChunk.doc_id == payload.doc_id,
                    SelfIngestChunk.commit_version == payload.commit_version,
                    SelfIngestChunk.doc_status == states.DOC_STAGED,
                )
                .values(
                    is_visible=True,
                    doc_status=states.DOC_VISIBLE,
                    visible_at=now,
                    embedding_status=(
                        states.EMBEDDING_INDEXED if vector_count
                        else states.EMBEDDING_MYSQL_ONLY
                    ),
                    updated_at=now,
                )
            )

            # Upsert kb_documents
            if existing_doc:
                existing_doc.file_name = getattr(pending_file, "file_name", existing_doc.file_name)
                existing_doc.file_path = getattr(pending_file, "minio_path", existing_doc.file_path)
                existing_doc.file_size = getattr(pending_file, "file_size", existing_doc.file_size)
                existing_doc.object_bucket = getattr(pending_file, "object_bucket", existing_doc.object_bucket)
                existing_doc.object_key = getattr(pending_file, "object_key", existing_doc.object_key)
                existing_doc.object_storage_uri = (
                    getattr(pending_file, "original_uri", None)
                    or getattr(pending_file, "minio_path", None)
                    or existing_doc.object_storage_uri
                )
                existing_doc.mime_type = (
                    getattr(pending_file, "mime_type", None)
                    or getattr(pending_file, "file_type", None)
                    or existing_doc.mime_type
                )
                existing_doc.file_ext = getattr(pending_file, "file_ext", existing_doc.file_ext)
                existing_doc.document_date = getattr(pending_file, "document_date", existing_doc.document_date)
                existing_doc.category_path = getattr(pending_file, "category_path", existing_doc.category_path)
                existing_doc.ingestion_backend = "self_ingestion"
                existing_doc.doc_status = states.DOC_VISIBLE
                existing_doc.commit_version = payload.commit_version
                existing_doc.committed_at = now
                existing_doc.visible_at = now
                existing_doc.storage_status = 1
                existing_doc.latest_job_id = payload.job_id
                existing_doc.chunk_count = len(payload.chunks)
                existing_doc.entity_count = len(payload.extraction.entities) if payload.extraction else 0
                existing_doc.relation_count = len(payload.extraction.relations) if payload.extraction else 0
                existing_doc.updated_at = now
                if payload.source_document:
                    existing_doc.source_hash = payload.source_document.source_hash
            else:
                session.add(KnowledgeBaseDocument(
                    kb_id=payload.kb_id,
                    doc_id=payload.doc_id,
                    file_name=getattr(pending_file, "file_name", ""),
                    file_path=getattr(pending_file, "minio_path", ""),
                    source_hash=payload.source_document.source_hash if payload.source_document else "",
                    doc_status=states.DOC_VISIBLE,
                    commit_version=payload.commit_version,
                    committed_at=now,
                    visible_at=now,
                    storage_status=1,
                    latest_job_id=payload.job_id,
                    chunk_count=len(payload.chunks),
                    entity_count=len(payload.extraction.entities) if payload.extraction else 0,
                    relation_count=len(payload.extraction.relations) if payload.extraction else 0,
                    ingestion_backend="self_ingestion",
                    created_at=now,
                    updated_at=now,
                ))

            # Update pending file
            file_id = getattr(pending_file, "file_id", None)
            if file_id:
                await session.execute(
                    sql_update(PF)
                    .where(PF.file_id == file_id)
                    .values(
                        status=2,
                        ingestion_backend="self_ingestion",
                        last_ingest_finished_at=now,
                    )
                )

            # Update job
            await session.execute(
                sql_update(SelfIngestJob)
                .where(SelfIngestJob.job_id == payload.job_id)
                .values(
                    chunk_count=len(payload.chunks),
                    vector_count=vector_count,
                    updated_at=now,
                )
            )

            await session.commit()
