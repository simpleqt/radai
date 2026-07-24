"""Repair helpers for self-ingestion partial commits.

按方案 §26.4 调度：
- 扫描 status IN (partial_success, failed) 且 retry_count < max_retry 且 updated_at 早于 cutoff 的任务。
- partial_success：MySQL 已 visible，仅补写缺失的外部索引（Milvus visible / Redis corpus / Nebula）。
- failed：清理残留 Milvus 向量，标记 Redis failed。
- 每次修复尝试自增 retry_count；达到 max_retry 后标记为 failed 不再重试。
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, text, update

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.connection import async_session_context
from backend.db.models import (
    KnowledgeBase,
    KnowledgeBaseDocument,
    SelfIngestChunk,
    SelfIngestJob,
)
from backend.rag.ingestion import states
from backend.rag.ingestion.commit_writer import CommitWriter
from backend.rag.ingestion.staging_manager import load_staging_payload
from backend.rag.ingestion.storage.milvus_writer import (
    MilvusChunkWriter,
    MilvusMediaWriter,
)
from backend.rag.ingestion.storage.redis_writer import (
    mark_doc_status,
    write_chunk_corpus,
)

logger = get_logger(__name__)


class SelfIngestionRepairWorker:
    """Make v2 indexes consistent after interrupted self-ingestion jobs."""

    @asynccontextmanager
    async def _named_mysql_lock(self, session, lock_name: str):
        """Hold a MySQL advisory lock on one dedicated physical connection."""
        bind = session.get_bind()
        if bind.dialect.name != "mysql":
            yield True
            return

        async_engine = getattr(session, "bind", None)
        if async_engine is None or not hasattr(async_engine, "connect"):
            raise RuntimeError("MySQL advisory lock requires an AsyncEngine bind")

        async with async_engine.connect() as lock_connection:
            acquired = (
                await lock_connection.execute(
                    text("SELECT GET_LOCK(:lock_name, 0)"),
                    {"lock_name": lock_name},
                )
            ).scalar() == 1
            operation_failed = False
            try:
                yield acquired
            except BaseException:
                operation_failed = True
                raise
            finally:
                if acquired:
                    try:
                        released = (
                            await lock_connection.execute(
                                text("SELECT RELEASE_LOCK(:lock_name)"),
                                {"lock_name": lock_name},
                            )
                        ).scalar()
                        if released != 1:
                            raise RuntimeError(
                                f"MySQL advisory lock release was not acknowledged: {lock_name}"
                            )
                    except Exception as exc:
                        if operation_failed:
                            logger.error(
                                "释放 MySQL advisory lock 失败（保留原异常）: lock=%s error=%s",
                                lock_name,
                                exc,
                            )
                        else:
                            raise

    async def repair_job(self, job_id: str) -> dict[str, Any]:
        async with async_session_context() as session:
            async with self._job_lock(session, job_id) as acquired:
                if not acquired:
                    return {"job_id": job_id, "status": "skipped", "reason": "repair_in_progress"}

                # Read after taking the lock so retry/status decisions are current.
                row = (
                    await session.execute(
                        select(SelfIngestJob, KnowledgeBase)
                        .join(KnowledgeBase, SelfIngestJob.kb_id == KnowledgeBase.kb_id)
                        .where(SelfIngestJob.job_id == job_id)
                    )
                ).one_or_none()
                if not row:
                    return {"job_id": job_id, "status": "missing"}
                job, kb = row

                if job.status == states.JOB_FAILED:
                    if job.stage == states.STAGE_CLEANUP_COMPLETED:
                        return {
                            "job_id": job_id,
                            "status": "skipped",
                            "reason": "cleanup_already_completed",
                        }
                    # Cleanup is compensation, not another processing retry.  It
                    # must still run when the processing retry budget is spent.
                    return await self._cleanup_failed_job(session, job, kb)

                if job.status == states.JOB_PARTIAL_SUCCESS:
                    max_retry = int(job.max_retry or 3)
                    current_retry = int(job.retry_count or 0)
                    if current_retry >= max_retry:
                        await self._mark_retry_exhausted(session, job.job_id)
                        return {
                            "job_id": job_id,
                            "status": "failed",
                            "reason": "max_retry_exceeded",
                        }
                    return await self._repair_partial_job(session, job, kb)

                if job.status == states.JOB_COMPLETED:
                    # completed 也做一次幂等校验（Milvus/Redis 一致性补偿）
                    return await self._repair_completed_job(session, job, kb)
                return {"job_id": job_id, "status": "skipped", "job_status": job.status}

    @asynccontextmanager
    async def _job_lock(self, session, job_id: str):
        """Serialize repair attempts across API processes on MySQL."""
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:32]
        lock_name = f"self_ingest_repair:{digest}"
        async with self._named_mysql_lock(session, lock_name) as acquired:
            try:
                yield acquired
            except BaseException:
                if acquired:
                    await session.rollback()
                raise
            else:
                if acquired:
                    try:
                        # Persist status/retry decisions before another process can
                        # acquire the advisory lock and observe stale state.
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        raise

    @asynccontextmanager
    async def _doc_cleanup_lock(self, session, kb_id: str, doc_id: str):
        """Serialize compensating cleanup for all failed jobs of one document."""
        digest = hashlib.sha256(f"{kb_id}:{doc_id}".encode()).hexdigest()[:32]
        lock_name = f"self_ingest_doc:{digest}"
        async with self._named_mysql_lock(session, lock_name) as acquired:
            yield acquired

    @staticmethod
    def _delete_milvus_job_rows_sync(writer, job_id: str) -> bool:
        """Delete only rows owned by one failed job, never a newer doc version."""
        lock_key = f"{writer.collection_name}:{job_id}"
        alias = f"self_repair_job_{hashlib.sha256(lock_key.encode()).hexdigest()[:12]}"
        connections = None
        try:
            from pymilvus import (
                Collection,
                utility,
            )
            from pymilvus import (
                connections as milvus_connections,
            )

            connections = milvus_connections
            writer._connect(alias)
            if not utility.has_collection(writer.collection_name, using=alias):
                return True
            collection = Collection(writer.collection_name, using=alias)
            collection.load()
            escaped_job_id = str(job_id).replace("\\", "\\\\").replace('"', '\\"')
            rows = collection.query(
                expr=f'job_id == "{escaped_job_id}"',
                output_fields=["chunk_id"],
            )
            chunk_ids = [row["chunk_id"] for row in rows if row.get("chunk_id")]
            if not chunk_ids:
                return True
            MilvusChunkWriter._delete_by_chunk_ids(collection, chunk_ids)
            collection.flush()
            return True
        except Exception as exc:
            logger.warning(
                "[self-ingestion] Milvus failed-job 精确清理失败: job=%s error=%s",
                job_id,
                exc,
            )
            return False
        finally:
            if connections is not None:
                try:
                    connections.disconnect(alias)
                except Exception:
                    pass

    async def _delete_milvus_job_rows(self, writer, job_id: str) -> bool:
        return await asyncio.to_thread(self._delete_milvus_job_rows_sync, writer, job_id)

    async def cleanup_failed_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        async with async_session_context() as session:
            rows = (
                await session.execute(
                    select(SelfIngestJob.job_id)
                    .where(SelfIngestJob.status == states.JOB_FAILED, SelfIngestJob.doc_id.isnot(None))
                    .where(or_(
                        SelfIngestJob.stage.is_(None),
                        SelfIngestJob.stage != states.STAGE_CLEANUP_COMPLETED,
                    ))
                    .order_by(SelfIngestJob.updated_at.desc())
                    .limit(max(int(limit or 100), 1))
                )
            ).scalars().all()
        results = []
        for job_id in rows:
            results.append(await self.repair_job(job_id))
        return results

    async def scan_and_repair(self, *, batch_size: int = 10) -> dict[str, Any]:
        """扫描需修复的任务（方案 §26.4）。

        目标：
        - partial_success：补写缺失的外部索引
        - failed：清理残留 Milvus 向量
        已耗尽的 partial_success 会被终结为 failed，failed 会执行一次补偿清理。
        """
        cutoff = datetime.now() - timedelta(minutes=2)
        async with async_session_context() as session:
            from backend.rag.ingestion.delete_service import self_delete_service

            delete_stats = await self_delete_service.retry_pending_deletes(
                session,
                limit=max(int(batch_size), 1),
            )
            job_ids = (
                await session.execute(
                    select(SelfIngestJob.job_id)
                    .where(
                        or_(
                            SelfIngestJob.status == states.JOB_PARTIAL_SUCCESS,
                            and_(
                                SelfIngestJob.status == states.JOB_FAILED,
                                or_(
                                    SelfIngestJob.stage.is_(None),
                                    SelfIngestJob.stage != states.STAGE_CLEANUP_COMPLETED,
                                ),
                            ),
                        ),
                        SelfIngestJob.updated_at < cutoff,
                    )
                    .order_by(SelfIngestJob.updated_at.asc())
                    .limit(max(int(batch_size), 1))
                )
            ).scalars().all()

        if not job_ids:
            return {
                "scanned": 0,
                "repaired": 0,
                "cleaned": 0,
                "skipped": 0,
                "delete_cleanup_scanned": delete_stats["scanned"],
                "delete_cleanup_repaired": delete_stats["repaired"],
            }

        stats = {"scanned": len(job_ids), "repaired": 0, "cleaned": 0, "skipped": 0}
        stats["delete_cleanup_scanned"] = delete_stats["scanned"]
        stats["delete_cleanup_repaired"] = delete_stats["repaired"]
        for job_id in job_ids:
            try:
                result = await self.repair_job(job_id)
                status = result.get("status", "skipped")
                if status == "repaired":
                    stats["repaired"] += 1
                elif status == "cleaned":
                    stats["cleaned"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as exc:
                logger.warning("[self-ingestion] repair 单任务异常: job=%s error=%s", job_id, exc)
                stats["skipped"] += 1
        return stats

    async def _bump_retry(self, session, job_id: str) -> int:
        """自增 retry_count 并返回新值。"""
        job = (
            await session.execute(select(SelfIngestJob).where(SelfIngestJob.job_id == job_id))
        ).scalar_one_or_none()
        if not job:
            return 0
        new_count = int(job.retry_count or 0) + 1
        await session.execute(
            update(SelfIngestJob)
            .where(SelfIngestJob.job_id == job_id)
            .values(retry_count=new_count, updated_at=datetime.now())
        )
        return new_count

    async def _mark_retry_exhausted(self, session, job_id: str) -> None:
        await session.execute(
            update(SelfIngestJob)
            .where(SelfIngestJob.job_id == job_id)
            .values(
                status=states.JOB_FAILED,
                stage=states.STAGE_FAILED,
                error_message="max_retry_exceeded",
                finished_at=datetime.now(),
                updated_at=datetime.now(),
            )
        )

    async def _repair_partial_job(
        self, session, job: SelfIngestJob, kb: KnowledgeBase,
    ) -> dict[str, Any]:
        """补写 partial_success 任务缺失的外部索引（方案 §26.3）。

        Phase-3 升级：优先从 staging_uri 加载完整 StagingPayload 进行重放，
        这样 nebula/milvus_entities 等需要 extraction 数据的阶段也能被修复。
        """
        if not job.doc_id:
            return {"job_id": job.job_id, "status": "skipped", "reason": "missing_doc_id"}

        max_retry = int(job.max_retry or 3)
        new_retry = await self._bump_retry(session, job.job_id)
        if new_retry > max_retry:
            await self._mark_retry_exhausted(session, job.job_id)
            return {"job_id": job.job_id, "status": "failed", "reason": "max_retry_exceeded"}

        metrics = dict(job.metrics_json or {})
        failures = list(metrics.get("external_failures") or [])

        # Phase-3: try CommitWriter.replay_from_stage if staging_uri is available
        staging_uri = getattr(job, "staging_uri", None)
        if staging_uri and failures:
            try:
                payload = await load_staging_payload(staging_uri)
                writer = CommitWriter()
                replay_result = await writer.replay_from_stage(
                    payload, kb=kb, failed_stages=failures,
                )
                repaired_components = [
                    entry["stage"] for entry in replay_result.stage_log
                    if entry.get("status") == "repaired"
                ]
                still_failing = [f for f in failures if f not in repaired_components]
                still_failing.extend(replay_result.external_failures)
                still_failing = sorted(set(still_failing))

                await self._update_job_after_repair(
                    session, job, metrics, repaired_components, still_failing,
                )
                exhausted = bool(still_failing and new_retry >= max_retry)
                if exhausted:
                    await self._mark_retry_exhausted(session, job.job_id)
                await self._append_repair_note(
                    session, job.job_id,
                    {"action": "replay_from_staging", "staging_uri": staging_uri,
                     "repaired": repaired_components, "still_failing": still_failing,
                     "stage_log": replay_result.stage_log},
                )
                return {
                    "job_id": job.job_id,
                    "status": "failed" if exhausted else (
                        "partial" if still_failing else "repaired"
                    ),
                    "method": "staging_replay",
                    "repaired_components": repaired_components,
                    "still_failing": still_failing,
                    "retry_count": new_retry,
                }
            except Exception as exc:
                logger.warning(
                    "[self-ingestion] staging replay 失败，回退旧修复路径: job=%s error=%s",
                    job.job_id, exc,
                )

        # Fallback: legacy repair path (no extraction data for nebula)
        chunks = list(
            (
                await session.execute(
                    select(SelfIngestChunk).where(
                        SelfIngestChunk.kb_id == job.kb_id,
                        SelfIngestChunk.doc_id == job.doc_id,
                        SelfIngestChunk.is_active.is_(True),
                        SelfIngestChunk.is_visible.is_(True),
                        SelfIngestChunk.doc_status == states.DOC_VISIBLE,
                    )
                )
            ).scalars().all()
        )

        repaired_components: list[str] = []
        if "milvus_visible" in failures and int(job.vector_count or 0) > 0:
            writer = MilvusChunkWriter(kb.kb_code)
            chunk_ids = [chunk.chunk_id for chunk in chunks if chunk.chunk_id]
            if await writer.hide_doc(job.doc_id) and await writer.mark_doc_visible(
                job.doc_id,
                chunk_ids=chunk_ids,
            ):
                repaired_components.append("milvus_visible")
        if "redis" in failures:
            try:
                await write_chunk_corpus(kb.kb_code, job.doc_id, chunks)
                repaired_components.append("redis")
            except Exception as exc:
                logger.warning("[self-ingestion] repair redis 失败: job=%s error=%s", job.job_id, exc)

        still_failing = [f for f in failures if f not in repaired_components]
        await self._update_job_after_repair(session, job, metrics, repaired_components, still_failing)
        exhausted = bool(still_failing and new_retry >= max_retry)
        if exhausted:
            await self._mark_retry_exhausted(session, job.job_id)
        await self._append_repair_note(
            session, job.job_id,
            {"action": "repair_partial_legacy", "repaired": repaired_components, "still_failing": still_failing},
        )
        return {
            "job_id": job.job_id,
            "status": "failed" if exhausted else (
                "partial" if still_failing else "repaired"
            ),
            "method": "legacy",
            "repaired_components": repaired_components, "still_failing": still_failing,
            "retry_count": new_retry,
        }

    async def _update_job_after_repair(
        self, session, job, metrics, repaired_components, still_failing,
    ) -> None:
        new_metrics = dict(metrics)
        if still_failing:
            new_metrics["external_failures"] = still_failing
            await session.execute(
                update(SelfIngestJob)
                .where(SelfIngestJob.job_id == job.job_id)
                .values(metrics_json=new_metrics, updated_at=datetime.now())
            )
        else:
            new_metrics["external_failures"] = []
            await session.execute(
                update(SelfIngestJob)
                .where(SelfIngestJob.job_id == job.job_id)
                .values(status=states.JOB_COMPLETED, stage=states.STAGE_COMMITTED,
                        metrics_json=new_metrics, updated_at=datetime.now())
            )

    async def _repair_completed_job(self, session, job: SelfIngestJob, kb: KnowledgeBase) -> dict[str, Any]:
        if not job.doc_id:
            return {"job_id": job.job_id, "status": "skipped", "reason": "missing_doc_id"}

        doc = (
            await session.execute(
                select(KnowledgeBaseDocument).where(
                    KnowledgeBaseDocument.kb_id == job.kb_id,
                    KnowledgeBaseDocument.doc_id == job.doc_id,
                    KnowledgeBaseDocument.doc_status == states.DOC_VISIBLE,
                )
            )
        ).scalar_one_or_none()
        if not doc:
            return {"job_id": job.job_id, "status": "skipped", "reason": "doc_not_visible"}

        chunks = list(
            (
                await session.execute(
                    select(SelfIngestChunk).where(
                        SelfIngestChunk.kb_id == job.kb_id,
                        SelfIngestChunk.doc_id == job.doc_id,
                        SelfIngestChunk.is_active.is_(True),
                        SelfIngestChunk.is_visible.is_(True),
                        SelfIngestChunk.doc_status == states.DOC_VISIBLE,
                    )
                )
            ).scalars().all()
        )
        repair_failures: list[str] = []
        milvus_visible = False
        if int(job.vector_count or 0) > 0:
            writer = MilvusChunkWriter(kb.kb_code)
            chunk_ids = [chunk.chunk_id for chunk in chunks if chunk.chunk_id]
            milvus_visible = await writer.hide_doc(job.doc_id) and await writer.mark_doc_visible(
                job.doc_id,
                chunk_ids=chunk_ids,
            )
            if not milvus_visible:
                repair_failures.append("milvus_visible")
        try:
            await write_chunk_corpus(kb.kb_code, job.doc_id, chunks)
        except Exception as exc:
            logger.warning(
                "[self-ingestion] completed job Redis backfill 失败: job=%s error=%s",
                job.job_id,
                exc,
            )
            repair_failures.append("redis")
        staging_repaired: list[str] = []
        staging_failures: list[str] = []
        staging_stages = self._completed_backfill_stages(kb)
        staging_uri = getattr(job, "staging_uri", None)
        if staging_uri and staging_stages:
            try:
                payload = await load_staging_payload(staging_uri)
                replay_result = await CommitWriter().replay_from_stage(
                    payload,
                    kb=kb,
                    failed_stages=staging_stages,
                )
                staging_repaired = [
                    entry["stage"] for entry in replay_result.stage_log
                    if entry.get("status") == "repaired"
                ]
                staging_failures = sorted(set(replay_result.external_failures))
            except Exception as exc:
                logger.warning(
                    "[self-ingestion] completed job staging backfill 失败: job=%s error=%s",
                    job.job_id, exc,
                )
                staging_failures = staging_stages
        repair_failures.extend(staging_failures)
        repair_failures = sorted(set(repair_failures))
        if repair_failures:
            metrics = dict(job.metrics_json or {})
            metrics["external_failures"] = repair_failures
            await session.execute(
                update(SelfIngestJob)
                .where(SelfIngestJob.job_id == job.job_id)
                .values(
                    status=states.JOB_PARTIAL_SUCCESS,
                    retry_count=0,
                    metrics_json=metrics,
                    updated_at=datetime.now(),
                )
            )
        await self._append_repair_note(
            session, job.job_id,
            {
                "action": "repair_completed_job",
                "chunks": len(chunks),
                "milvus_visible": milvus_visible,
                "staging_repaired": staging_repaired,
                "staging_failures": staging_failures,
                "repair_failures": repair_failures,
                "staging_uri": staging_uri,
            },
        )
        return {
            "job_id": job.job_id,
            "status": "repair_pending" if repair_failures else "repaired",
            "chunks": len(chunks), "milvus_visible": milvus_visible,
            "staging_repaired": staging_repaired,
            "staging_failures": staging_failures,
            "repair_failures": repair_failures,
        }

    @staticmethod
    def _completed_backfill_stages(kb: KnowledgeBase) -> list[str]:
        """Idempotent external indexes that completed-job repair can replay from staging."""
        stages = ["milvus_entities"]
        if getattr(kb, "nebula_space", None):
            stages.append("nebula")
        return stages

    async def _cleanup_failed_job(self, session, job: SelfIngestJob, kb: KnowledgeBase) -> dict[str, Any]:
        if not job.doc_id:
            return {"job_id": job.job_id, "status": "skipped", "reason": "missing_doc_id"}

        async with self._doc_cleanup_lock(
            session,
            str(job.kb_id),
            str(job.doc_id),
        ) as acquired:
            if not acquired:
                return {
                    "job_id": job.job_id,
                    "status": "skipped",
                    "reason": "doc_cleanup_in_progress",
                }
            return await self._cleanup_failed_job_under_lock(session, job, kb)

    async def _cleanup_failed_job_under_lock(
        self,
        session,
        job: SelfIngestJob,
        kb: KnowledgeBase,
    ) -> dict[str, Any]:
        # Inspect the stable document row under a short transaction.  External
        # Milvus/Redis calls must not run while a database row lock is held.
        document = (
            await session.execute(
                select(KnowledgeBaseDocument)
                .where(
                    KnowledgeBaseDocument.kb_id == job.kb_id,
                    KnowledgeBaseDocument.doc_id == job.doc_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        protected_reason = None
        if document is not None:
            if document.doc_status in states.RETRIEVAL_VISIBLE_DOC_STATUSES:
                protected_reason = "job_has_visible_document"
            elif document.latest_job_id and document.latest_job_id != job.job_id:
                protected_reason = "newer_job_reserved_document"
        await session.commit()

        if settings.milvus_connection_uri:
            chunk_writer = MilvusChunkWriter(kb.kb_code)
            media_writer = MilvusMediaWriter(kb.kb_code)
            milvus_deleted, media_deleted = await asyncio.gather(
                self._delete_milvus_job_rows(chunk_writer, job.job_id),
                self._delete_milvus_job_rows(media_writer, job.job_id),
            )
        else:
            milvus_deleted, media_deleted = True, True
        # Without a stable document row there is no row-lock boundary shared
        # with a concurrent first insert.  Job-scoped Milvus cleanup remains
        # safe, but a doc-wide Redis status write could hide that new document.
        redis_skipped = document is None or protected_reason is not None
        redis_marked = True
        if not redis_skipped:
            # Re-check after Milvus I/O. A newer ingestion may have reserved the
            # document while cleanup was running. Commit immediately to release
            # the row lock before calling Redis; a later ingest writes its
            # visible marker after this cleanup marker.
            current_document = (
                await session.execute(
                    select(KnowledgeBaseDocument)
                    .where(
                        KnowledgeBaseDocument.kb_id == job.kb_id,
                        KnowledgeBaseDocument.doc_id == job.doc_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current_document is None:
                redis_skipped = True
                protected_reason = "document_removed_during_cleanup"
            elif current_document.doc_status in states.RETRIEVAL_VISIBLE_DOC_STATUSES:
                redis_skipped = True
                protected_reason = "job_has_visible_document"
            elif (
                current_document.latest_job_id
                and current_document.latest_job_id != job.job_id
            ):
                redis_skipped = True
                protected_reason = "newer_job_reserved_document"
            await session.commit()
        if not redis_skipped:
            redis_marked = await mark_doc_status(
                kb.kb_code,
                job.doc_id,
                commit_status=states.COMMIT_FAILED,
                is_visible=False,
            )
            # Close the small window between the pre-Redis check and the status
            # write. If a newer ingestion became visible in that interval,
            # restore the document marker. If it becomes visible after this
            # check, its own Redis commit runs later and remains authoritative.
            latest_document = (
                await session.execute(
                    select(KnowledgeBaseDocument).where(
                        KnowledgeBaseDocument.kb_id == job.kb_id,
                        KnowledgeBaseDocument.doc_id == job.doc_id,
                    )
                )
            ).scalar_one_or_none()
            newer_visible = bool(
                latest_document is not None
                and (
                    latest_document.doc_status
                    in states.RETRIEVAL_VISIBLE_DOC_STATUSES
                    or (
                        latest_document.latest_job_id
                        and latest_document.latest_job_id != job.job_id
                    )
                )
            )
            if newer_visible:
                redis_marked = await mark_doc_status(
                    kb.kb_code,
                    job.doc_id,
                    commit_status=states.COMMIT_COMMITTED,
                    is_visible=True,
                )
                protected_reason = "newer_document_became_visible_during_cleanup"
        cleanup_completed = bool(milvus_deleted and media_deleted and redis_marked)
        if cleanup_completed:
            await session.execute(
                update(SelfIngestJob)
                .where(SelfIngestJob.job_id == job.job_id)
                .values(stage=states.STAGE_CLEANUP_COMPLETED, updated_at=datetime.now())
            )
        await self._append_repair_note(
            session, job.job_id,
            {
                "action": "cleanup_failed_job",
                "milvus_deleted": milvus_deleted,
                "media_deleted": media_deleted,
                "redis_marked": redis_marked,
                "redis_skipped": redis_skipped,
                "protected_reason": protected_reason,
                "cleanup_completed": cleanup_completed,
                "retry_count": int(job.retry_count or 0),
            },
        )
        return {
            "job_id": job.job_id,
            "status": "cleaned" if cleanup_completed else "cleanup_pending",
            "milvus_deleted": milvus_deleted,
            "media_deleted": media_deleted,
            "redis_marked": redis_marked,
            "redis_skipped": redis_skipped,
            "protected_reason": protected_reason,
            "retry_count": int(job.retry_count or 0),
        }

    async def _append_repair_note(self, session, job_id: str, note: dict[str, Any]) -> None:
        note = {**note, "at": datetime.now().isoformat()}
        job = (
            await session.execute(select(SelfIngestJob).where(SelfIngestJob.job_id == job_id))
        ).scalar_one_or_none()
        if not job:
            return
        extra = dict(job.extra_json or {})
        repairs = list(extra.get("repairs") or [])
        repairs.append(note)
        extra["repairs"] = repairs[-20:]
        await session.execute(
            update(SelfIngestJob)
            .where(SelfIngestJob.job_id == job_id)
            .values(extra_json=extra, updated_at=datetime.now())
        )


self_ingestion_repair_worker = SelfIngestionRepairWorker()
