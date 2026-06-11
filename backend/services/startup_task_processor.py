"""
启动时任务检查服务
检查数据库中待处理的任务并自动处理
使用独立线程运行，不阻塞主事件循环
"""

import asyncio
import threading
import uuid
from pathlib import Path
from typing import Optional

from sqlalchemy import select, update

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.connection import get_session
from backend.db.models import PendingFile
from backend.rag.service import get_rag_service
from backend.storage import _get_minio_client, _normalize_minio_object_name

logger = get_logger(__name__)


def build_pending_file_cache_path(cache_dir: Path, file_id: str, file_name: str) -> Path:
    """为待入库文件生成唯一且保留原始扩展名的本地缓存路径。"""
    original_name = Path(file_name or "").name or "pending_file"
    suffix = Path(original_name).suffix
    stem = Path(original_name).stem or "pending_file"
    safe_stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in stem).strip("_")
    safe_stem = safe_stem or "pending_file"
    return cache_dir / f"{file_id}_{safe_stem}{suffix}"


class StartupTaskProcessor:
    """启动时任务处理器 - 在独立线程中运行，不阻塞主事件循环"""

    def __init__(self):
        self._running = False
        self._rag_service = None
        self._bg_task: Optional[asyncio.Task] = None
        self._concurrent_limit = getattr(settings, 'max_concurrent_files', 5)

    async def initialize(self):
        """初始化服务"""
        self._rag_service = get_rag_service()
        logger.info("[StartupTaskProcessor] 初始化完成")

    async def _background_loop(self):
        """后台循环处理任务"""
        while self._running:
            try:
                await self._check_and_process_pending_tasks()
            except asyncio.CancelledError:
                logger.info("[StartupTaskProcessor] 后台处理任务已取消")
                break
            except Exception as e:
                logger.exception(f"[StartupTaskProcessor] 后台处理出错: {e}")

            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break

    async def _check_and_process_pending_tasks(self):
        """检查并处理已启动的入库任务（仅 status=1）。"""
        try:
            pending_file_snapshots, status_1_count = await asyncio.to_thread(
                self._claim_pending_files_snapshot
            )

            if not pending_file_snapshots:
                logger.info("[StartupTaskProcessor] 无待入库文件")
                return

            logger.info(
                f"[StartupTaskProcessor] 发现 {len(pending_file_snapshots)} 个待入库文件 "
                f"(入库中={status_1_count})，开始处理..."
            )

            from backend.db.redis import get_redis
            redis_client = get_redis()
            
            try:
                semaphore = asyncio.Semaphore(self._concurrent_limit)
                
                async def process_with_semaphore(pf):
                    async with semaphore:
                        lock_key = f"kb_ingest_lock:{pf['file_id']}"
                        acquired = await redis_client.set(lock_key, "1", nx=True, ex=7200)
                        if not acquired:
                            logger.info(f"[StartupTaskProcessor] 文件正在由其他 worker 处理，跳过: {pf['file_name']}")
                            return None

                        try:
                            return await self._process_single_file(
                                pf["file_id"],
                                pf["file_name"],
                                pf["minio_path"],
                                pf["kb_id"],
                                pf["category_path"],
                                pf["document_date"],
                            )
                        except Exception as e:
                            logger.exception(
                                f"[StartupTaskProcessor] 单文件处理任务异常: file_id={pf['file_id']}, file_name={pf['file_name']}, error={e}"
                            )
                            return False
                        finally:
                            await redis_client.delete(lock_key)
                
                tasks = [process_with_semaphore(pf) for pf in pending_file_snapshots]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                success_count = sum(1 for r in results if r is True)
                fail_count = sum(1 for r in results if isinstance(r, Exception) or r is False)
                skip_count = sum(1 for r in results if r is None)
                logger.info(f"[StartupTaskProcessor] 批量处理完成: 成功={success_count}, 失败={fail_count}, 跳过={skip_count}")
            finally:
                await redis_client.aclose()

        except Exception as e:
            logger.exception(f"[StartupTaskProcessor] 检查待处理任务失败: {e}")

    async def _process_single_file(
        self,
        file_id: str,
        file_name: str,
        minio_path: str,
        kb_id: str,
        category_path: Optional[str],
        document_date: Optional[str],
    ) -> bool:
        """处理单个文件"""
        cache_file_path = None
        try:
            logger.info(f"[StartupTaskProcessor] 开始处理: {file_name}")

            # MinIO 下载是同步阻塞的，在 asyncio 任务中应使用 to_thread 运行
            content = await asyncio.to_thread(self._download_from_minio, minio_path)
            
            cache_dir = Path(__file__).resolve().parents[2] / "data" / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file_path = build_pending_file_cache_path(cache_dir, file_id, file_name)
            cache_file_path.write_bytes(content)

            doc_id = str(uuid.uuid4())
            ingest_result = await self._rag_service.process_multimodal(
                file_path=str(cache_file_path),
                kb_id=kb_id,
                doc_id=doc_id,
                file_name=file_name,
                document_date=document_date,
                category_path=category_path,
            )
            doc_id = ingest_result.get("doc_id", doc_id)

            await asyncio.to_thread(self._update_pending_file_status, file_id, 2, None)

            logger.info(f"[StartupTaskProcessor] 文件入库成功: {file_name}, doc_id={doc_id}")
            return True

        except Exception as e:
            logger.error(f"[StartupTaskProcessor] 文件入库失败: {file_name}, error={e}")
            try:
                await asyncio.to_thread(self._update_pending_file_status, file_id, 3, str(e))
            except Exception as db_err:
                logger.warning(f"[StartupTaskProcessor] 更新失败状态失败: {db_err}")
            return False

        finally:
            if cache_file_path and cache_file_path.exists():
                cache_file_path.unlink()

    @staticmethod
    def _download_from_minio(minio_path: str) -> bytes:
        """从 MinIO 下载文件"""
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

    @staticmethod
    def _claim_pending_files_snapshot() -> tuple[list[dict], int]:
        """同步查询并抢占待入库任务，供 to_thread 调用。"""
        with get_session() as session:
            result = session.execute(
                select(PendingFile)
                .where(PendingFile.status == 1)
                .order_by(PendingFile.created_at)
                .limit(100)
            )
            pending_files = result.scalars().all()

            if not pending_files:
                return [], 0

            snapshots = [
                {
                    "file_id": pf.file_id,
                    "file_name": pf.file_name,
                    "minio_path": pf.minio_path,
                    "kb_id": pf.kb_id,
                    "category_path": pf.category_path,
                    "document_date": pf.document_date,
                }
                for pf in pending_files
            ]

            status_1_count = sum(1 for pf in pending_files if pf.status == 1)
            return snapshots, status_1_count

    @staticmethod
    def _update_pending_file_status(file_id: str, status: int, error_message: Optional[str]) -> None:
        """同步更新文件状态，供 to_thread 调用。"""
        with get_session() as session:
            session.execute(
                update(PendingFile)
                .where(PendingFile.file_id == file_id)
                .values(status=status, error_message=error_message)
            )
            session.commit()

    def start_background_processor(self):
        """启动后台任务处理器（在主事件循环的任务中运行）"""
        if self._running:
            logger.info("[StartupTaskProcessor] 后台处理器已在运行中")
            return

        self._running = True
        self._bg_task = asyncio.create_task(self._background_loop(), name="StartupTaskProcessor")
        logger.info("[StartupTaskProcessor] 后台任务处理器已在主事件循环启动")

    async def stop_background_processor(self):
        """停止后台任务处理器"""
        self._running = False
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
        logger.info("[StartupTaskProcessor] 后台任务处理器已停止")


_startup_task_processor: Optional[StartupTaskProcessor] = None


def get_startup_task_processor() -> StartupTaskProcessor:
    """获取启动任务处理器单例"""
    global _startup_task_processor
    if _startup_task_processor is None:
        _startup_task_processor = StartupTaskProcessor()
    return _startup_task_processor
