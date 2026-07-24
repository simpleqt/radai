"""
启动时任务检查服务
检查数据库中待处理的任务并自动处理
使用独立线程运行，不阻塞主事件循环
"""

import asyncio
import threading
from typing import Optional

from sqlalchemy import select

from backend.application.ingestion import (
    PendingIngestSnapshot,
    PendingIngestUseCase,
)
from backend.config.loggers import get_logger
from backend.db.connection import get_session
from backend.db.models import KnowledgeBase, PendingFile
from backend.rag.ingestion.concurrency import ingest_file_concurrency

logger = get_logger(__name__)


class StartupTaskProcessor:
    """启动时任务处理器 - 在独立线程中运行，不阻塞主事件循环"""

    def __init__(self):
        self._running = False
        self._ingest_use_case = PendingIngestUseCase(
            source_label="StartupTaskProcessor",
            legacy_md_direct=False,
        )
        self._bg_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """初始化服务"""
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

            semaphore = asyncio.Semaphore(ingest_file_concurrency())

            async def process_with_semaphore(pf):
                async with semaphore:
                    try:
                        return await self._ingest_use_case.process_snapshot(pf)
                    except Exception as e:
                        logger.exception(
                            f"[StartupTaskProcessor] 单文件处理任务异常: file_id={pf['file_id']}, file_name={pf['file_name']}, error={e}"
                        )
                        return False

            tasks = [process_with_semaphore(pf) for pf in pending_file_snapshots]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            success_count = sum(1 for r in results if r is True)
            fail_count = sum(1 for r in results if isinstance(r, Exception) or r is False)
            skip_count = sum(1 for r in results if r is None)
            logger.info(f"[StartupTaskProcessor] 批量处理完成: 成功={success_count}, 失败={fail_count}, 跳过={skip_count}")

        except Exception as e:
            logger.exception(f"[StartupTaskProcessor] 检查待处理任务失败: {e}")

    @staticmethod
    def _claim_pending_files_snapshot() -> tuple[list[dict], int]:
        """同步查询并抢占待入库任务，供 to_thread 调用。"""
        with get_session() as session:
            result = session.execute(
                select(PendingFile, KnowledgeBase)
                .outerjoin(KnowledgeBase, PendingFile.kb_id == KnowledgeBase.kb_id)
                .where(PendingFile.status == 1)
                .order_by(
                    PendingFile.retry_count.desc(),
                    PendingFile.updated_at.asc(),
                    PendingFile.created_at.asc(),
                )
                .limit(100)
            )
            rows = result.all()

            if not rows:
                return [], 0

            snapshots = [
                PendingIngestSnapshot.from_models(pf, kb).to_mapping()
                for pf, kb in rows
            ]

            status_1_count = sum(1 for pf, _kb in rows if pf.status == 1)
            return snapshots, status_1_count

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
_startup_task_processor_lock = threading.Lock()


def get_startup_task_processor() -> StartupTaskProcessor:
    """获取启动任务处理器单例"""
    global _startup_task_processor
    if _startup_task_processor is None:
        with _startup_task_processor_lock:
            if _startup_task_processor is None:
                _startup_task_processor = StartupTaskProcessor()
    return _startup_task_processor
