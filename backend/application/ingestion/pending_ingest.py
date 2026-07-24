"""Unified pending-file ingestion use case.

Both the API startup task processor and the standalone ingest worker call this
module so locking, backend routing, status transitions, and document enrichment
stay consistent.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from backend.config import settings
from backend.config.loggers import get_logger
from backend.db.connection import async_session_context
from backend.db.models import KnowledgeBase, KnowledgeBaseDocument, PendingFile
from backend.domain.knowledge_base.backend_policy import (
    BLOCKED_BACKEND,
    LEGACY_BACKEND,
    SELF_BACKEND,
    resolve_ingestion_backend,
)
from backend.rag.ingestion.concurrency import ingest_file_concurrency, semaphore
from backend.rag.ingestion.fencing import IngestFenceLostError
from backend.rag.ingestion.pipeline import SelfIngestionPipeline
from backend.rag.service import get_rag_service
from backend.storage import download_minio_object_to_path

logger = get_logger(__name__)

MD_MIME_TYPES = {"text/markdown", "text/x-markdown"}
BLOCKED_ERROR_MESSAGE = (
    "blocked: 知识库标记为 self_ingestion，"
    "但全局 SELF_INGESTION_ENABLED 未启用或未在白名单中，"
    "已阻塞以防止写入 legacy 索引"
)
MISSING_KB_ERROR_MESSAGE = "failed: 目标知识库不存在或已被删除，无法完成入库"


@dataclass(frozen=True)
class PendingIngestFence:
    lock_key: str
    counter_key: str
    owner: str
    fencing_token: int
    redis_client: Any

    @property
    def credential(self) -> str:
        return f"{self.fencing_token}:{self.owner}"


_ACTIVE_INGEST_FENCE: ContextVar[PendingIngestFence | None] = ContextVar(
    "active_pending_ingest_fence",
    default=None,
)


def build_pending_file_cache_path(cache_dir: Path, file_id: str, file_name: str) -> Path:
    """为待入库文件生成唯一且保留原始扩展名的本地缓存路径。"""
    original_name = Path(file_name or "").name or "pending_file"
    suffix = Path(original_name).suffix
    stem = Path(original_name).stem or "pending_file"
    safe_stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in stem).strip("_")
    safe_stem = safe_stem or "pending_file"
    return cache_dir / f"{file_id}_{safe_stem}{suffix}"


@dataclass(frozen=True)
class PendingIngestSnapshot:
    file_id: str
    file_name: str
    minio_path: str
    kb_id: str
    category_path: str | None = None
    document_date: str | None = None
    ingestion_backend: str = LEGACY_BACKEND
    upload_id: str | None = None
    file_size: int | None = None
    file_type: str | None = None
    source_hash: str | None = None
    object_bucket: str | None = None
    object_key: str | None = None
    original_uri: str | None = None
    mime_type: str | None = None
    file_ext: str | None = None
    ingest_job_id: str | None = None
    kb_code: str | None = None
    kb_found: bool = True

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> PendingIngestSnapshot:
        return cls(
            file_id=str(data.get("file_id") or ""),
            file_name=str(data.get("file_name") or ""),
            minio_path=str(data.get("minio_path") or ""),
            kb_id=str(data.get("kb_id") or ""),
            category_path=data.get("category_path"),
            document_date=data.get("document_date"),
            ingestion_backend=str(data.get("ingestion_backend") or LEGACY_BACKEND),
            upload_id=data.get("upload_id"),
            file_size=data.get("file_size"),
            file_type=data.get("file_type"),
            source_hash=data.get("source_hash"),
            object_bucket=data.get("object_bucket"),
            object_key=data.get("object_key"),
            original_uri=data.get("original_uri"),
            mime_type=data.get("mime_type"),
            file_ext=data.get("file_ext"),
            ingest_job_id=data.get("ingest_job_id"),
            kb_code=data.get("kb_code"),
            kb_found=bool(data.get("kb_found", True)),
        )

    @classmethod
    def from_models(
        cls,
        pending_file: PendingFile,
        kb: KnowledgeBase | None,
        *,
        ingestion_backend: str | None = None,
    ) -> PendingIngestSnapshot:
        kb_found = kb is not None
        return cls(
            file_id=pending_file.file_id,
            file_name=pending_file.file_name,
            minio_path=pending_file.minio_path,
            kb_id=pending_file.kb_id,
            category_path=pending_file.category_path,
            document_date=pending_file.document_date,
            ingestion_backend=ingestion_backend
            or (
                resolve_ingestion_backend(settings, kb=kb, pending_file=pending_file)
                if kb_found
                else LEGACY_BACKEND
            ),
            upload_id=pending_file.upload_id,
            file_size=pending_file.file_size,
            file_type=pending_file.file_type,
            source_hash=pending_file.source_hash,
            object_bucket=pending_file.object_bucket,
            object_key=pending_file.object_key,
            original_uri=pending_file.original_uri,
            mime_type=pending_file.mime_type,
            file_ext=pending_file.file_ext,
            ingest_job_id=pending_file.ingest_job_id,
            kb_code=kb.kb_code if kb_found else None,
            kb_found=kb_found,
        )

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


class PendingIngestUseCase:
    """Process one pending file under the shared ingestion contract."""

    _LOCK_TTL_SECONDS = 7200
    _LOCK_RENEW_INTERVAL_SECONDS = 1200
    _ACQUIRE_LOCK_SCRIPT = """
        if redis.call('exists', KEYS[1]) == 1 then
            return {0, 0}
        end
        local token = redis.call('incr', KEYS[2])
        local credential = tostring(token) .. ':' .. ARGV[1]
        redis.call('set', KEYS[1], credential, 'EX', ARGV[2])
        return {1, token}
    """
    _RENEW_LOCK_SCRIPT = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('expire', KEYS[1], ARGV[2])
        end
        return 0
    """
    _RELEASE_LOCK_SCRIPT = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        end
        return 0
    """
    _ASSERT_FENCE_SCRIPT = """
        if redis.call('get', KEYS[1]) ~= ARGV[1] then
            return 0
        end
        if tostring(redis.call('get', KEYS[2]) or '') ~= ARGV[2] then
            return 0
        end
        return 1
    """

    def __init__(
        self,
        *,
        source_label: str,
        legacy_md_direct: bool,
    ) -> None:
        self.source_label = source_label
        self.legacy_md_direct = legacy_md_direct
        self.rag_service = get_rag_service()
        self.self_ingestion_pipeline = SelfIngestionPipeline()
        self.cache_dir = Path(__file__).resolve().parents[2] / "data" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def process_file_id(self, file_id: str) -> bool:
        """Load a pending file by id, lock it, and process it."""
        capacity = await semaphore(
            "pending_ingest_files",
            ingest_file_concurrency(),
        )
        async with capacity:
            result = await self._with_file_lock(
                file_id,
                display_name=file_id,
                callback=lambda: self._load_and_process_file_id(file_id),
            )
        return result is True

    async def process_snapshot(self, snapshot_or_mapping: PendingIngestSnapshot | dict[str, Any]) -> bool | None:
        """Process a preloaded pending-file snapshot, returning None when lock is held elsewhere."""
        snapshot = (
            PendingIngestSnapshot.from_mapping(snapshot_or_mapping)
            if isinstance(snapshot_or_mapping, dict)
            else snapshot_or_mapping
        )
        capacity = await semaphore(
            "pending_ingest_files",
            ingest_file_concurrency(),
        )
        async with capacity:
            return await self._with_file_lock(
                snapshot.file_id,
                display_name=snapshot.file_name,
                callback=lambda: self._process_snapshot(snapshot),
            )

    async def _with_file_lock(self, file_id: str, *, display_name: str, callback) -> bool | None:
        from backend.db.redis import get_redis

        redis_client = get_redis()
        hash_tag = "{" + file_id + "}"
        lock_key = f"kb_ingest:{hash_tag}:lock"
        counter_key = f"kb_ingest:{hash_tag}:fence"
        lock_owner = uuid.uuid4().hex
        try:
            acquired_result = await redis_client.eval(
                self._ACQUIRE_LOCK_SCRIPT,
                2,
                lock_key,
                counter_key,
                lock_owner,
                self._LOCK_TTL_SECONDS,
            )
            acquired = bool(acquired_result and int(acquired_result[0]) == 1)
            if not acquired:
                logger.info("[%s] 文件正在由其他 worker 处理，跳过: %s", self.source_label, display_name)
                return None
            fence = PendingIngestFence(
                lock_key=lock_key,
                counter_key=counter_key,
                owner=lock_owner,
                fencing_token=int(acquired_result[1]),
                redis_client=redis_client,
            )
            renew_task = asyncio.create_task(
                self._renew_file_lock(fence),
                name=f"renew-ingest-lock-{file_id}",
            )
            context_token = _ACTIVE_INGEST_FENCE.set(fence)
            try:
                work_task = asyncio.create_task(
                    callback(),
                    name=f"process-ingest-file-{file_id}",
                )
            finally:
                _ACTIVE_INGEST_FENCE.reset(context_token)
            work_succeeded = False
            try:
                done, _ = await asyncio.wait(
                    {work_task, renew_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if work_task in done:
                    result = await work_task
                    await self._assert_file_fence(fence)
                    work_succeeded = True
                    return result

                # A failed renewal means another worker may acquire the key as
                # soon as its TTL expires.  Stop this attempt instead of
                # silently allowing two writers to continue.
                await renew_task
                raise RuntimeError(f"入库锁所有权已丢失: {display_name}")
            finally:
                if not work_task.done():
                    work_task.cancel()
                renew_task.cancel()
                await asyncio.gather(work_task, renew_task, return_exceptions=True)
                try:
                    released = await redis_client.eval(
                        self._RELEASE_LOCK_SCRIPT,
                        1,
                        fence.lock_key,
                        fence.credential,
                    )
                    if not released:
                        logger.warning(
                            "[%s] 入库锁已过期或所有者已改变，未删除他人锁: %s",
                            self.source_label,
                            display_name,
                        )
                        if work_succeeded:
                            raise RuntimeError(
                                f"入库处理完成前锁所有权已丢失: {display_name}"
                            )
                except Exception as exc:
                    logger.warning("[%s] 释放入库锁失败: %s", self.source_label, exc)
                    # A completed callback is not safe to acknowledge when we
                    # cannot prove that this worker still owned the lock at the
                    # completion boundary.  Preserve an existing work error,
                    # but turn an otherwise successful result into a retry.
                    if work_succeeded:
                        raise
        finally:
            await redis_client.aclose()

    async def _renew_file_lock(self, fence: PendingIngestFence) -> None:
        while True:
            await asyncio.sleep(self._LOCK_RENEW_INTERVAL_SECONDS)
            renewed = await fence.redis_client.eval(
                self._RENEW_LOCK_SCRIPT,
                1,
                fence.lock_key,
                fence.credential,
                self._LOCK_TTL_SECONDS,
            )
            if not renewed:
                logger.error(
                    "[%s] 入库锁续租失败，锁所有权已丢失: %s",
                    self.source_label,
                    fence.lock_key,
                )
                return

    async def _assert_file_fence(self, fence: PendingIngestFence) -> None:
        current = await fence.redis_client.eval(
            self._ASSERT_FENCE_SCRIPT,
            2,
            fence.lock_key,
            fence.counter_key,
            fence.credential,
            fence.fencing_token,
        )
        if not current:
            raise IngestFenceLostError(
                "入库处理完成前锁所有权已丢失"
                f"（fencing token={fence.fencing_token} 已失效）"
            )

    async def _assert_current_fence(self) -> None:
        fence = _ACTIVE_INGEST_FENCE.get()
        if fence is not None:
            await self._assert_file_fence(fence)

    async def _load_and_process_file_id(self, file_id: str) -> bool:
        async with async_session_context() as session:
            result = await session.execute(
                select(PendingFile, KnowledgeBase)
                .outerjoin(KnowledgeBase, PendingFile.kb_id == KnowledgeBase.kb_id)
                .where(PendingFile.file_id == file_id)
            )
            row = result.one_or_none()
            if not row:
                logger.warning("[%s] 未找到待入库文件: %s", self.source_label, file_id)
                return False

            pending_file, kb = row
            if pending_file.status != 1:
                logger.info(
                    "[%s] 跳过非入库中状态文件: %s, status=%s",
                    self.source_label,
                    file_id,
                    pending_file.status,
                )
                return False

            snapshot = PendingIngestSnapshot.from_models(pending_file, kb)

        return await self._process_snapshot(snapshot)

    async def _process_snapshot(self, snapshot: PendingIngestSnapshot) -> bool:
        try:
            await self._assert_current_fence()
            logger.info(
                "[%s] 开始处理: %s, file_id=%s, route=%s",
                self.source_label,
                snapshot.file_name,
                snapshot.file_id,
                self._route_name(snapshot),
            )

            if not snapshot.kb_found:
                await self._mark_pending_file_missing_kb(snapshot.file_id, snapshot.kb_id)
                logger.warning(
                    "[%s] 入库失败: file_id=%s 引用的知识库不存在: kb_id=%s",
                    self.source_label,
                    snapshot.file_id,
                    snapshot.kb_id,
                )
                return False

            if snapshot.ingestion_backend == BLOCKED_BACKEND:
                await self._mark_pending_file_blocked(snapshot.file_id)
                logger.warning(
                    "[%s] 阻塞入库: file_id=%s kb=%s 标记 self_ingestion 但全局开关未满足",
                    self.source_label,
                    snapshot.file_id,
                    snapshot.kb_code or snapshot.kb_id,
                )
                return False

            await self._mark_pending_file_started(snapshot.file_id, snapshot.ingestion_backend)

            if snapshot.ingestion_backend == SELF_BACKEND:
                await self._assert_current_fence()
                result = await self.self_ingestion_pipeline.ingest_pending_file(
                    snapshot.file_id,
                    fence_check=self._assert_current_fence,
                )
                await self._assert_current_fence()
                return result

            if self.legacy_md_direct and self._is_md_file(snapshot):
                return await self._process_md_file(snapshot)
            return await self._process_multimodal_file(snapshot)
        except IngestFenceLostError as exc:
            logger.warning(
                "[%s] 检测到更新的入库 worker，停止旧 token 写入: file_id=%s, error=%s",
                self.source_label,
                snapshot.file_id,
                exc,
            )
            return False
        except Exception as exc:
            logger.error(
                "[%s] 入库失败: file_id=%s, file_name=%s, error=%s",
                self.source_label,
                snapshot.file_id,
                snapshot.file_name,
                exc,
                exc_info=True,
            )
            try:
                await self._update_pending_file_status(snapshot.file_id, 3, str(exc))
            except Exception as db_err:
                logger.warning(
                    "[%s] 更新失败状态失败: file_id=%s, error=%s",
                    self.source_label,
                    snapshot.file_id,
                    db_err,
                )
            return False

    def _route_name(self, snapshot: PendingIngestSnapshot) -> str:
        if not snapshot.kb_found:
            return "missing-kb"
        if snapshot.ingestion_backend == SELF_BACKEND:
            return SELF_BACKEND
        if self.legacy_md_direct and self._is_md_file(snapshot):
            return "legacy-md-direct"
        return "legacy-mineru"

    @staticmethod
    def _is_md_file(snapshot: PendingIngestSnapshot) -> bool:
        if snapshot.file_type in MD_MIME_TYPES or snapshot.mime_type in MD_MIME_TYPES:
            return True
        return bool(snapshot.file_name and snapshot.file_name.lower().endswith(".md"))

    async def _process_md_file(self, snapshot: PendingIngestSnapshot) -> bool:
        cache_file_path = build_pending_file_cache_path(
            self.cache_dir,
            snapshot.file_id,
            snapshot.file_name,
        )
        try:
            await asyncio.to_thread(
                download_minio_object_to_path,
                snapshot.minio_path,
                cache_file_path,
                bucket_name=snapshot.object_bucket,
            )
            doc_id = str(uuid.uuid4())
            max_inline_bytes = self._max_inline_text_bytes()
            if cache_file_path.stat().st_size > max_inline_bytes:
                logger.info(
                    "[%s] MD 超过内联文本上限，改用文件路径入库: %s",
                    self.source_label,
                    snapshot.file_name,
                )
                ingest_result = await self.rag_service.process_multimodal(
                    file_path=str(cache_file_path),
                    kb_id=snapshot.kb_id,
                    doc_id=doc_id,
                    file_name=snapshot.file_name,
                    document_date=snapshot.document_date,
                    category_path=snapshot.category_path,
                )
            else:
                md_content = await asyncio.to_thread(
                    cache_file_path.read_text,
                    encoding="utf-8",
                )
                logger.info(
                    "[%s] MD直接入库: %s, doc_id=%s, md_length=%d chars",
                    self.source_label,
                    snapshot.file_name,
                    doc_id,
                    len(md_content),
                )
                await self._assert_current_fence()
                ingest_result = await self.rag_service.process_md_content(
                    md_content=md_content,
                    kb_id=snapshot.kb_id,
                    doc_id=doc_id,
                    file_name=snapshot.file_name,
                    document_date=snapshot.document_date,
                    category_path=snapshot.category_path,
                )
            await self._assert_current_fence()
            doc_id = ingest_result.get("doc_id", doc_id)
            await self._enrich_document_record(snapshot, doc_id)
            await self._update_pending_file_status(snapshot.file_id, 2, None)
            logger.info(
                "[%s] MD入库成功: %s, doc_id=%s",
                self.source_label,
                snapshot.file_name,
                doc_id,
            )
            return True
        finally:
            await asyncio.to_thread(cache_file_path.unlink, missing_ok=True)

    async def _process_multimodal_file(self, snapshot: PendingIngestSnapshot) -> bool:
        cache_file_path: Path | None = None
        try:
            cache_file_path = build_pending_file_cache_path(
                self.cache_dir,
                snapshot.file_id,
                snapshot.file_name,
            )
            await asyncio.to_thread(
                download_minio_object_to_path,
                snapshot.minio_path,
                cache_file_path,
                bucket_name=snapshot.object_bucket,
            )

            doc_id = str(uuid.uuid4())
            await self._assert_current_fence()
            ingest_result = await self.rag_service.process_multimodal(
                file_path=str(cache_file_path),
                kb_id=snapshot.kb_id,
                doc_id=doc_id,
                file_name=snapshot.file_name,
                document_date=snapshot.document_date,
                category_path=snapshot.category_path,
            )
            await self._assert_current_fence()
            doc_id = ingest_result.get("doc_id", doc_id)
            await self._enrich_document_record(snapshot, doc_id)
            await self._update_pending_file_status(snapshot.file_id, 2, None)
            logger.info(
                "[%s] 文件入库成功: %s, doc_id=%s",
                self.source_label,
                snapshot.file_name,
                doc_id,
            )
            return True
        finally:
            if cache_file_path:
                await asyncio.to_thread(cache_file_path.unlink, missing_ok=True)

    @staticmethod
    def _max_inline_text_bytes() -> int:
        try:
            return max(
                1024 * 1024,
                int(os.getenv("PENDING_INGEST_MAX_INLINE_TEXT_BYTES", "67108864")),
            )
        except (TypeError, ValueError):
            return 64 * 1024 * 1024

    async def _mark_pending_file_started(self, file_id: str, ingestion_backend: str) -> None:
        await self._assert_current_fence()
        async with async_session_context() as session:
            await session.execute(
                update(PendingFile)
                .where(PendingFile.file_id == file_id)
                .values(
                    ingestion_backend=ingestion_backend,
                    last_ingest_started_at=datetime.now(),
                )
            )
            await session.commit()

    async def _mark_pending_file_blocked(self, file_id: str) -> None:
        await self._assert_current_fence()
        async with async_session_context() as session:
            await session.execute(
                update(PendingFile)
                .where(PendingFile.file_id == file_id)
                .values(
                    ingestion_backend=SELF_BACKEND,
                    status=3,
                    error_message=BLOCKED_ERROR_MESSAGE,
                    last_ingest_finished_at=datetime.now(),
                )
            )
            await session.commit()

    async def _mark_pending_file_missing_kb(self, file_id: str, kb_id: str) -> None:
        await self._update_pending_file_status(
            file_id,
            3,
            f"{MISSING_KB_ERROR_MESSAGE}: kb_id={kb_id}",
        )

    async def _enrich_document_record(self, snapshot: PendingIngestSnapshot, doc_id: str) -> None:
        await self._assert_current_fence()
        values = {
            "file_size": snapshot.file_size,
            "source_hash": snapshot.source_hash,
            "object_bucket": snapshot.object_bucket or settings.minio_bucket_name,
            "object_key": snapshot.object_key or snapshot.minio_path,
            "object_storage_uri": snapshot.original_uri
            or f"minio://{settings.minio_bucket_name}/{snapshot.minio_path}",
            "mime_type": snapshot.mime_type or snapshot.file_type,
            "file_ext": snapshot.file_ext or Path(snapshot.file_name or "").suffix.lower(),
            "ingestion_backend": LEGACY_BACKEND,
            "latest_job_id": snapshot.ingest_job_id or snapshot.upload_id,
            "doc_status": "visible",
            "ingest_finished_at": datetime.now(),
            "committed_at": datetime.now(),
            "visible_at": datetime.now(),
            "updated_at": datetime.now(),
        }
        async with async_session_context() as session:
            await session.execute(
                update(KnowledgeBaseDocument)
                .where(KnowledgeBaseDocument.kb_id == snapshot.kb_id)
                .where(KnowledgeBaseDocument.doc_id == doc_id)
                .values(**values)
            )
            await session.commit()

    async def _update_pending_file_status(
        self,
        file_id: str,
        status: int,
        error_message: str | None,
    ) -> None:
        await self._assert_current_fence()
        async with async_session_context() as session:
            await session.execute(
                update(PendingFile)
                .where(PendingFile.file_id == file_id)
                .values(
                    status=status,
                    error_message=error_message,
                    last_ingest_finished_at=datetime.now(),
                )
            )
            await session.commit()
