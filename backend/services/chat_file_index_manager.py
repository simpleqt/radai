"""聊天文件索引管理:线程级 LRU 管理器、存储水合与全局单例。(拆自 chat_file_index.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.db.connection import get_session_factory
from backend.db.models import SessionFile
from backend.db.models import UserFile
from collections import OrderedDict
from sqlalchemy import select
from typing import List
from typing import Optional
import asyncio
import threading
from backend.services.chat_file_search import (
    ChatFileIndex,
)

logger = get_logger(__name__)


class ChatFileIndexManager:
    """全局 LRU 索引池

    控制同时在内存中的索引数量，避免内存膨胀。
    100 个活跃会话 × ~200KB/会话 ≈ 20MB，极轻量。
    """

    def __init__(self, max_threads: int = 100):
        self._indices: OrderedDict[tuple[str, str], ChatFileIndex] = OrderedDict()
        self._max_threads = max_threads
        self._lock = threading.RLock()

    def _evict_if_needed_locked(self) -> None:
        if len(self._indices) >= self._max_threads:
            (evicted_user_id, evicted_thread_id), _ = self._indices.popitem(last=False)
            logger.info(
                "[ChatFileIndexManager] LRU 驱逐: user_id=%s thread_id=%s",
                evicted_user_id,
                evicted_thread_id,
            )

    def _store_index_locked(
        self, user_id: str, thread_id: str, index: ChatFileIndex
    ) -> ChatFileIndex:
        self._evict_if_needed_locked()
        self._indices[(user_id, thread_id)] = index
        return index

    def _load_thread_documents_from_storage(
        self, user_id: str, thread_id: str
    ) -> List[tuple[str, str, str]]:
        session_factory = get_session_factory()
        with session_factory() as session:
            stmt = (
                select(
                    UserFile.file_id,
                    UserFile.display_name,
                    UserFile.metadata_,
                )
                .join(SessionFile, SessionFile.file_id == UserFile.file_id)
                .where(SessionFile.session_id == thread_id)
                .where(SessionFile.user_id == user_id)
                .where(UserFile.owner_user_id == user_id)
                .where(UserFile.status == "active")
                .order_by(SessionFile.created_at.asc(), UserFile.created_at.asc())
            )
            rows = session.execute(stmt).all()

        documents: List[tuple[str, str, str]] = []
        seen_file_ids: set[str] = set()
        for file_id, display_name, metadata in rows:
            if file_id in seen_file_ids:
                continue
            seen_file_ids.add(file_id)
            meta = metadata or {}
            if meta.get("file_category") != "long_doc":
                continue
            content = str(meta.get("content") or "").strip()
            if not content:
                continue
            documents.append((file_id, display_name, content))
        return documents

    def _hydrate_index(self, user_id: str, thread_id: str) -> Optional[ChatFileIndex]:
        documents = self._load_thread_documents_from_storage(user_id, thread_id)
        if not documents:
            return None

        index = ChatFileIndex(thread_id)
        for file_id, file_name, content in documents:
            index.add_document(
                file_id=file_id,
                content=content,
                file_name=file_name,
            )
        logger.info(
            "[ChatFileIndexManager] 已从存储重建索引: user_id=%s thread_id=%s, files=%d, chunks=%d",
            user_id,
            thread_id,
            len(documents),
            index.chunk_count,
        )
        return index

    def get_or_create(self, thread_id: str, *, user_id: str) -> ChatFileIndex:
        key = (user_id, thread_id)
        with self._lock:
            if key in self._indices:
                self._indices.move_to_end(key)
                return self._indices[key]

        # 未命中先按 get 的路径从存储水合:每日发布重启/LRU 驱逐后,直接建
        # 空索引会把它缓存住,该会话早前的长文档从检索中静默消失(后续
        # get_async 一直命中这份空索引,永不再水合)。水合为空才建空索引;
        # 水合是机会性回填,存储故障不应阻断上传链路建索引。
        try:
            hydrated = self._hydrate_index(user_id, thread_id)
        except Exception:
            logger.warning(
                "[ChatFileIndexManager] get_or_create 水合失败,回退空索引: "
                "user_id=%s thread_id=%s",
                user_id,
                thread_id,
                exc_info=True,
            )
            hydrated = None
        with self._lock:
            if key in self._indices:
                self._indices.move_to_end(key)
                return self._indices[key]
            index = hydrated if hydrated is not None else ChatFileIndex(thread_id)
            return self._store_index_locked(user_id, thread_id, index)

    def get(self, thread_id: str, *, user_id: str) -> Optional[ChatFileIndex]:
        key = (user_id, thread_id)
        with self._lock:
            if key in self._indices:
                self._indices.move_to_end(key)
                return self._indices[key]

        hydrated = self._hydrate_index(user_id, thread_id)
        if hydrated is None:
            return None

        with self._lock:
            if key in self._indices:
                self._indices.move_to_end(key)
                return self._indices[key]
            return self._store_index_locked(user_id, thread_id, hydrated)

    async def get_async(
        self,
        thread_id: str,
        *,
        user_id: str,
    ) -> Optional[ChatFileIndex]:
        """Hydrate an index without running synchronous DB/CPU work on the loop."""
        return await asyncio.to_thread(self.get, thread_id, user_id=user_id)

    async def get_or_create_async(
        self,
        thread_id: str,
        *,
        user_id: str,
    ) -> ChatFileIndex:
        """同 get_or_create，但水合（同步 DB 查询 + BM25 重建）走线程。

        缓存未命中时 get_or_create 会在调用线程上做完整的存储水合；
        从 async 路由直接调用会阻塞整个事件循环。
        """
        return await asyncio.to_thread(self.get_or_create, thread_id, user_id=user_id)

    def remove(self, thread_id: str, *, user_id: str) -> None:
        with self._lock:
            self._indices.pop((user_id, thread_id), None)

    def has_files(self, thread_id: str, *, user_id: str) -> bool:
        idx = self.get(thread_id, user_id=user_id)
        return idx is not None and idx.chunk_count > 0


# 全局单例
_index_manager = ChatFileIndexManager(max_threads=100)


def get_index_manager() -> ChatFileIndexManager:
    return _index_manager
