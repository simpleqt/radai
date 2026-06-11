"""
内容过滤管理器

管理多个知识库的内容过滤器实例
"""

import asyncio
import threading
import weakref
from collections import OrderedDict
from typing import Callable, Optional

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.rag.content_filter.models import FilterConfig, TimeAwareConfig
from backend.rag.content_filter.per_kb_filter import PerKBContentFilter

logger = get_logger(__name__)


class ContentFilterManager:
    """
    内容过滤管理器
    
    管理多个知识库的内容过滤器实例，支持：
    - 按知识库隔离过滤逻辑
    - LRU 缓存和自动清理
    - 统一的过滤接口
    """
    
    def __init__(self, max_cached_kb: int = 5):
        self._filters: OrderedDict[str, PerKBContentFilter] = OrderedDict()
        self._max_cached = max_cached_kb
        self._lock = asyncio.Lock()
    
    async def get_filter(
        self,
        kb_code: str,
        kb_id: str,
        config: Optional[FilterConfig] = None,
        embedding_func: Optional[Callable] = None,
    ) -> PerKBContentFilter:
        """
        获取指定知识库的内容过滤器（带缓存）
        
        Args:
            kb_code: 知识库代码
            kb_id: 知识库 ID
            config: 过滤配置
            embedding_func: 嵌入函数
            
        Returns:
            内容过滤器实例
        """
        if config is None:
            config = FilterConfig(
                time_aware=TimeAwareConfig(
                    enabled=bool(settings.time_aware_enabled),
                    sigma=float(settings.time_aware_sigma),
                    alpha_base=float(settings.time_aware_alpha_base),
                    time_high_boost=float(settings.time_aware_high_boost),
                    time_low_penalty=float(settings.time_aware_low_penalty),
                    undated_doc_beta=float(settings.time_aware_undated_doc_beta),
                    created_at_fallback_beta=float(settings.time_aware_created_at_fallback_beta),
                )
            )

        cache_key = kb_code
        
        async with self._lock:
            if cache_key in self._filters:
                self._filters.move_to_end(cache_key)
                # 若缓存实例创建时未注入 embedding_func，后续补齐以启用语义反向边。
                if embedding_func is not None:
                    cached = self._filters[cache_key]
                    if getattr(cached, "_embedding_func", None) is None:
                        cached._embedding_func = embedding_func
                        if hasattr(cached, "_ppr_filter") and cached._ppr_filter is not None:
                            cached._ppr_filter.embedding_func = embedding_func
                        logger.info(f"[内容过滤管理器] 已为缓存过滤器补齐 embedding_func: {kb_code}")
                    else:
                        logger.debug(f"[内容过滤管理器] 缓存过滤器已具备 embedding_func: {kb_code}")
                logger.debug(f"[内容过滤管理器] 命中缓存: {kb_code}")
                return self._filters[cache_key]
            
            if len(self._filters) >= self._max_cached:
                oldest_key = next(iter(self._filters))
                del self._filters[oldest_key]
                logger.info(f"[内容过滤管理器] LRU 清理: {oldest_key}")
            
            content_filter = PerKBContentFilter(
                kb_code=kb_code,
                kb_id=kb_id,
                config=config,
                embedding_func=embedding_func,
            )
            
            self._filters[cache_key] = content_filter
            logger.info(f"[内容过滤管理器] 创建新过滤器: {kb_code}, 当前缓存数: {len(self._filters)}")
            
            return content_filter
    
    async def invalidate_filter(self, kb_code: str) -> bool:
        """
        使指定知识库的过滤器失效
        
        Args:
            kb_code: 知识库代码
            
        Returns:
            是否成功失效
        """
        async with self._lock:
            if kb_code in self._filters:
                del self._filters[kb_code]
                logger.info(f"[内容过滤管理器] 过滤器失效: {kb_code}")
                return True
            return False
    
    def clear_cache(self) -> int:
        """
        清空所有缓存
        
        Returns:
            清理的过滤器数量
        """
        count = len(self._filters)
        self._filters.clear()
        logger.info(f"[内容过滤管理器] 清空缓存: {count} 个")
        return count
    
    def get_cached_kb_codes(self) -> list:
        """获取已缓存的知识库代码列表"""
        return list(self._filters.keys())


_content_filter_manager_local = threading.local()


def get_content_filter_manager() -> ContentFilterManager:
    """获取内容过滤管理器（事件循环本地实例）"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        manager = getattr(_content_filter_manager_local, "sync_manager", None)
        if manager is None:
            manager = ContentFilterManager()
            _content_filter_manager_local.sync_manager = manager
        return manager

    loop_managers = getattr(_content_filter_manager_local, "loop_managers", None)
    if loop_managers is None:
        loop_managers = weakref.WeakKeyDictionary()
        _content_filter_manager_local.loop_managers = loop_managers

    manager = loop_managers.get(loop)
    if manager is None:
        manager = ContentFilterManager()
        loop_managers[loop] = manager
    return manager
