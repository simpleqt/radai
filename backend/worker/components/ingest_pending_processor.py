"""待入库文件处理组件（供独立 ingest worker 使用）"""

import uuid
from pathlib import Path

from sqlalchemy import select, update

from backend.config import settings
from backend.config.loggers import get_logger
from backend.db.connection import async_session_context
from backend.db.models import KnowledgeBaseDocument, PendingFile
from backend.rag.service import get_rag_service
from backend.services.startup_task_processor import build_pending_file_cache_path
from backend.storage import _get_minio_client, _normalize_minio_object_name

logger = get_logger(__name__)

MD_MIME_TYPES = {"text/markdown", "text/x-markdown"}


class PendingIngestProcessor:
    """处理 kb_pending_files 中 status=1 的文件。"""

    def __init__(self):
        self.rag_service = get_rag_service()
        self.cache_dir = Path(__file__).resolve().parents[2] / "data" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _is_md_file(self, pf: PendingFile) -> bool:
        if pf.file_type in MD_MIME_TYPES:
            return True
        if pf.file_name and pf.file_name.lower().endswith(".md"):
            return True
        return False

    async def process_file(self, file_id: str) -> bool:

        from backend.db.redis import get_redis
        redis_client = get_redis()
        lock_key = f"kb_ingest_lock:{file_id}"

        try:
            acquired = await redis_client.set(lock_key, "1", nx=True, ex=7200)
            if not acquired:
                logger.debug(f"[ingest-worker] 文件正在由其他 worker 处理，跳过: {file_id}")
                return False

            try:
                pending_file_name = file_id
                kb_id = ""
                is_md = False

                try:
                    async with async_session_context() as session:
                        result = await session.execute(
                            select(PendingFile).where(PendingFile.file_id == file_id)
                        )
                        pending_file = result.scalar_one_or_none()

                        if not pending_file:
                            logger.warning(f"未找到待入库文件: {file_id}")
                            return False

                        if pending_file.status != 1:
                            logger.info(f"跳过非入库中状态文件: {file_id}, status={pending_file.status}")
                            return False

                        pending_file_name = pending_file.file_name
                        kb_id = pending_file.kb_id
                        is_md = self._is_md_file(pending_file)

                    route = "md-direct" if is_md else "mineru"
                    logger.info(
                        f"[ingest-worker] 开始处理: {pending_file_name}, "
                        f"file_id={file_id}, route={route}"
                    )

                    if is_md:
                        return await self._process_md_file(pending_file, file_id)
                    else:
                        return await self._process_pdf_file(pending_file, file_id)

                except Exception as exc:
                    logger.error(f"[ingest-worker] 入库失败: file_id={file_id}, error={exc}", exc_info=True)
                    try:
                        async with async_session_context() as session:
                            await session.execute(
                                update(PendingFile)
                                .where(PendingFile.file_id == file_id)
                                .values(status=3, error_message=str(exc))
                            )
                            await session.commit()
                    except Exception as db_err:
                        logger.warning(f"[ingest-worker] 更新失败状态失败: file_id={file_id}, error={db_err}")
                    return False

            finally:
                await redis_client.delete(lock_key)
        finally:
            await redis_client.aclose()

    async def _process_md_file(self, pending_file: PendingFile, file_id: str) -> bool:
        try:
            content = self._download_from_minio(pending_file.minio_path)
            md_content = content.decode("utf-8")
        except Exception as e:
            logger.error(f"[ingest-worker] MinIO读取MD失败: {e}")
            raise

        doc_id = str(uuid.uuid4())
        logger.info(
            f"[ingest-worker] MD直接入库: {pending_file.file_name}, "
            f"doc_id={doc_id}, md_length={len(md_content)} chars"
        )

        ingest_result = await self.rag_service.process_md_content(
            md_content=md_content,
            kb_id=pending_file.kb_id,
            doc_id=doc_id,
            file_name=pending_file.file_name,
            document_date=pending_file.document_date,
            category_path=pending_file.category_path,
        )

        async with async_session_context() as session:
            await session.execute(
                update(PendingFile)
                .where(PendingFile.file_id == file_id)
                .values(status=2, error_message=None)
            )
            await session.commit()

        logger.info(f"[ingest-worker] MD入库成功: {pending_file.file_name}, doc_id={doc_id}")
        return True

    async def _process_pdf_file(self, pending_file: PendingFile, file_id: str) -> bool:
        cache_file_path: Path | None = None
        try:
            content = self._download_from_minio(pending_file.minio_path)
            cache_file_path = build_pending_file_cache_path(
                self.cache_dir,
                file_id,
                pending_file.file_name,
            )
            cache_file_path.write_bytes(content)

            doc_id = str(uuid.uuid4())
            ingest_result = await self.rag_service.process_multimodal(
                file_path=str(cache_file_path),
                kb_id=pending_file.kb_id,
                doc_id=doc_id,
                file_name=pending_file.file_name,
                document_date=pending_file.document_date,
                category_path=pending_file.category_path,
            )
            doc_id = ingest_result.get("doc_id", doc_id)

            async with async_session_context() as session:
                await session.execute(
                    update(PendingFile)
                    .where(PendingFile.file_id == file_id)
                    .values(status=2, error_message=None)
                )
                await session.commit()

            logger.info(f"[ingest-worker] PDF入库成功: {pending_file.file_name}, doc_id={doc_id}")
            return True

        finally:
            if cache_file_path and cache_file_path.exists():
                cache_file_path.unlink()

    @staticmethod
    def _download_from_minio(minio_path: str) -> bytes:
        client = _get_minio_client()
        candidates = [minio_path]
        normalized = _normalize_minio_object_name(minio_path)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

        last_error: Exception | None = None
        for candidate in candidates:
            try:
                response = client.get_object(settings.minio_bucket_name, candidate)
                try:
                    return response.read()
                finally:
                    response.close()
                    response.release_conn()
            except Exception as exc:
                last_error = exc

        assert last_error is not None
        raise last_error
