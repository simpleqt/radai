import asyncio
from datetime import datetime, time, timedelta
from typing import List, Optional

from backend.config.loggers import get_logger

logger = get_logger(__name__)


class RAGIndexSyncScheduler:
    """
    RAG 索引同步调度器
    
    职责：
    1. 定时触发全量同步
    2. 调度时间管理
    3. 资源使用控制
    """
    
    _instance: Optional["RAGIndexSyncScheduler"] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._sync_time = time(2, 0)
        self._sync_kb_codes: List[str] = []
        self._sync_kb_ids: dict = {}
        
        logger.info("[RAGIndexSyncScheduler] 初始化完成")
    
    def set_sync_time(self, sync_time: time) -> None:
        """设置同步时间"""
        self._sync_time = sync_time
        logger.info(f"[RAGIndexSyncScheduler] 同步时间设置为: {sync_time}")
    
    async def start(self, kb_codes: List[str], kb_ids: dict = None) -> None:
        """
        启动调度器
        
        Args:
            kb_codes: 知识库编码列表
            kb_ids: 知识库编码到 ID 的映射
        """
        if self._running:
            logger.warning("[RAGIndexSyncScheduler] 调度器已启动")
            return
        
        self._running = True
        self._sync_kb_codes = kb_codes
        self._sync_kb_ids = kb_ids or {}
        self._task = asyncio.create_task(self._run_loop())
        
        logger.info(
            f"[RAGIndexSyncScheduler] 调度器已启动, "
            f"同步时间: {self._sync_time}, 知识库: {kb_codes}"
        )
    
    async def stop(self) -> None:
        """停止调度器"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[RAGIndexSyncScheduler] 调度器已停止")
    
    async def _run_loop(self) -> None:
        """调度循环"""
        while self._running:
            try:
                now = datetime.now()
                target = datetime.combine(now.date(), self._sync_time)

                if now >= target:
                    # 使用 timedelta 计算次日，避免月底 day+1 导致非法日期
                    target = datetime.combine(now.date() + timedelta(days=1), self._sync_time)

                wait_seconds = (target - now).total_seconds()
                
                logger.debug(
                    f"[RAGIndexSyncScheduler] 下次同步时间: {target}, "
                    f"等待: {wait_seconds:.0f}秒"
                )
                
                await asyncio.sleep(min(wait_seconds, 3600))
                
                if not self._running:
                    break
                
                now = datetime.now()
                if now.time() >= self._sync_time:
                    datetime.combine(now.date(), self._sync_time)
                    if now.time() < time(3, 0):
                        await self._do_scheduled_sync()
                        await asyncio.sleep(3600)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RAGIndexSyncScheduler] 调度错误: {e}")
                await asyncio.sleep(60)
    
    async def _do_scheduled_sync(self) -> None:
        """执行定时同步"""
        logger.info("[RAGIndexSyncScheduler] 开始定时同步")
        
        from backend.rag.sync.rag_index_sync_manager import get_rag_index_sync_manager
        
        sync_manager = get_rag_index_sync_manager()
        await sync_manager.initialize()
        
        for kb_code in self._sync_kb_codes:
            try:
                kb_id = self._sync_kb_ids.get(kb_code, kb_code)
                await sync_manager.force_sync(kb_code, kb_id)
                
            except Exception as e:
                logger.error(
                    f"[RAGIndexSyncScheduler] 同步失败: kb={kb_code}, error={e}"
                )
        
        logger.info("[RAGIndexSyncScheduler] 定时同步完成")
    
    async def trigger_manual_sync(self, kb_code: str, kb_id: str = None) -> bool:
        """手动触发同步"""
        from backend.rag.sync.rag_index_sync_manager import get_rag_index_sync_manager
        
        sync_manager = get_rag_index_sync_manager()
        await sync_manager.initialize()
        return await sync_manager.force_sync(kb_code, kb_id or kb_code)
    
    def is_running(self) -> bool:
        """检查调度器是否运行中"""
        return self._running


def get_rag_index_sync_scheduler() -> RAGIndexSyncScheduler:
    """获取 RAG 索引同步调度器单例"""
    return RAGIndexSyncScheduler()
