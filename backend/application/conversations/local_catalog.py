"""SQLite conversation catalog owned by one application runtime.

Test and production may each select a separate catalog so conversation
list/rename/delete/memory operations remain durable without writing the shared
MySQL ``conversations`` table.  Other business metadata continues to use MySQL.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from backend.application.conversations.errors import (
    ConversationAgentMismatchError,
    ConversationDeletedError,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '新对话',
    metadata TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, thread_id)
);
CREATE INDEX IF NOT EXISTS idx_local_conversation_user_history
ON conversations(user_id, is_deleted, updated_at DESC, id DESC);
"""


@dataclass(slots=True)
class ConversationRecord:
    id: str
    user_id: str
    thread_id: str
    title: str
    metadata_: dict[str, Any] | None
    is_deleted: bool
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class ConversationCatalogStats:
    total: int
    active: int
    deleted: int
    unique_users: int
    latest_updated_at: datetime | None


def conversation_store_backend() -> str:
    return os.getenv("AGENT_CONVERSATION_STORE_BACKEND", "mysql").strip().lower()


def local_catalog_enabled() -> bool:
    return conversation_store_backend() in {"sqlite", "sqlite3", "local"}


def shared_object_storage_read_only() -> bool:
    return os.getenv(
        "AGENT_SHARED_OBJECT_STORAGE_READ_ONLY", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _configured_catalog_path() -> Path:
    raw = os.getenv("AGENT_CONVERSATION_SQLITE_PATH", "").strip()
    if not raw:
        raise RuntimeError(
            "AGENT_CONVERSATION_SQLITE_PATH is required for the SQLite conversation catalog"
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise RuntimeError("AGENT_CONVERSATION_SQLITE_PATH must be absolute")
    resolved = path.resolve(strict=False)
    runtime_root = (
        os.getenv("DOC_AGENT_RUNTIME_ROOT", "").strip()
        or os.getenv("DOC_AGENT_TEST_RUNTIME_ROOT", "").strip()
        or os.getenv("DOC_AGENT_PRODUCTION_RUNTIME_ROOT", "").strip()
    )
    if runtime_root:
        allowed_root = Path(runtime_root).expanduser().resolve(strict=False)
        try:
            resolved.relative_to(allowed_root)
        except ValueError as exc:
            raise RuntimeError(
                "SQLite conversation catalog must stay inside the configured runtime root"
            ) from exc
    return resolved


def get_local_conversation_catalog() -> SQLiteConversationCatalog | None:
    if not local_catalog_enabled():
        return None
    return SQLiteConversationCatalog(_configured_catalog_path())


def _now() -> datetime:
    return datetime.now()


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return _now()


def _decode_metadata(value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(decoded) if isinstance(decoded, dict) else None


def encode_metadata(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


# Paths whose directory/permissions/schema/journal mode were already ensured
# by this process. Every catalog operation used to re-run mkdir + two chmods +
# the DDL script; all of that is idempotent one-time setup, so later
# connections only need the per-connection pragmas.
_INITIALIZED_PATHS: set[Path] = set()
_INITIALIZED_PATHS_LOCK = threading.Lock()


def reset_local_catalog_initialization() -> None:
    """Forget initialized paths (tests and controlled restarts only)."""

    with _INITIALIZED_PATHS_LOCK:
        _INITIALIZED_PATHS.clear()


def _initialize_catalog_path(path: Path) -> None:
    """Run the one-time directory/journal/schema setup, serialized per process.

    并发首连不能各自执行 WAL 切换与建表脚本:journal_mode 切换需要独占访问,
    与并发的 executescript 相互抢锁时 busy_timeout 并不总能挽救,实测五个并发
    首连稳定抛 ``database is locked``。因此初始化在线程锁内用同步连接完成,
    第一个调用方执行,其余等待后直接走已初始化快路径;线程锁同时覆盖多事件
    循环场景。初始化失败不记入已初始化集合,下一次调用会重试。
    """

    with _INITIALIZED_PATHS_LOCK:
        if path in _INITIALIZED_PATHS:
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        connection = sqlite3.connect(str(path), timeout=15)
        try:
            connection.execute("PRAGMA busy_timeout=15000")
            # journal_mode=WAL persists in the database file; the schema
            # script is IF NOT EXISTS. Both only need one successful run
            # per process lifetime.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA_SQL)
            connection.commit()
        finally:
            connection.close()
        os.chmod(path, 0o600)
        _INITIALIZED_PATHS.add(path)


class SQLiteConversationCatalog:
    """Small async repository that owns one runtime-local SQLite file."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser().resolve(strict=False)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        with _INITIALIZED_PATHS_LOCK:
            initialized = self.path in _INITIALIZED_PATHS
        if not initialized:
            # 串行化的一次性初始化在工作线程内持锁完成(锁不可跨 await 持有)。
            await asyncio.to_thread(_initialize_catalog_path, self.path)
        connection = await aiosqlite.connect(str(self.path), timeout=15)
        try:
            connection.row_factory = aiosqlite.Row
            # busy_timeout and synchronous are per-connection settings.
            await connection.execute("PRAGMA busy_timeout=15000")
            await connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            await connection.close()

    @staticmethod
    def _record(row: aiosqlite.Row) -> ConversationRecord:
        return ConversationRecord(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            thread_id=str(row["thread_id"]),
            title=str(row["title"] or "新对话"),
            metadata_=_decode_metadata(row["metadata"]),
            is_deleted=bool(row["is_deleted"]),
            created_at=_as_datetime(row["created_at"]),
            updated_at=_as_datetime(row["updated_at"]),
        )

    async def get(
        self,
        *,
        user_id: str,
        thread_id: str,
        include_deleted: bool = True,
    ) -> ConversationRecord | None:
        deleted_clause = "" if include_deleted else " AND is_deleted = 0"
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM conversations WHERE user_id = ? AND thread_id = ?"
                + deleted_clause,
                (user_id, thread_id),
            )
            row = await cursor.fetchone()
        return self._record(row) if row else None

    async def list_for_user(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> list[ConversationRecord]:
        # 置顶排序基于 metadata JSON 的 pinned_at 键：无需 schema 迁移即可让
        # 既有目录文件直接生效。json_valid 守卫历史坏数据(malformed JSON 会让
        # json_extract 直接抛错终止查询)。
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM conversations
                WHERE user_id = ? AND is_deleted = 0
                ORDER BY
                    CASE
                        WHEN metadata IS NOT NULL
                             AND json_valid(metadata)
                             AND json_extract(metadata, '$.pinned_at') IS NOT NULL
                        THEN 0 ELSE 1
                    END,
                    updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, int(limit), int(offset)),
            )
            rows = await cursor.fetchall()
        return [self._record(row) for row in rows]

    async def list_all(
        self,
        *,
        limit: int,
        offset: int,
        include_deleted: bool = False,
        user_ids: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[list[ConversationRecord], int]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if not include_deleted:
            clauses.append("is_deleted = 0")
        if user_ids is not None:
            normalized = sorted({str(user_id) for user_id in user_ids if user_id})
            if not normalized:
                return [], 0
            clauses.append("user_id IN (" + ",".join("?" for _ in normalized) + ")")
            parameters.extend(normalized)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        async with self._connect() as connection:
            total_cursor = await connection.execute(
                "SELECT COUNT(*) FROM conversations" + where,
                tuple(parameters),
            )
            total_row = await total_cursor.fetchone()
            cursor = await connection.execute(
                "SELECT * FROM conversations"
                + where
                + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (*parameters, int(limit), int(offset)),
            )
            rows = await cursor.fetchall()
        return [self._record(row) for row in rows], int(
            total_row[0] if total_row else 0
        )

    async def stats(self) -> ConversationCatalogStats:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN is_deleted = 0 THEN 1 ELSE 0 END),
                       COUNT(DISTINCT user_id),
                       MAX(updated_at)
                FROM conversations
                """
            )
            row = await cursor.fetchone()
        total = int(row[0] or 0) if row else 0
        active = int(row[1] or 0) if row else 0
        latest = _as_datetime(row[3]) if row and row[3] else None
        return ConversationCatalogStats(
            total=total,
            active=active,
            deleted=total - active,
            unique_users=int(row[2] or 0) if row else 0,
            latest_updated_at=latest,
        )

    async def ensure_runtime_conversation(
        self,
        *,
        user_id: str,
        thread_id: str,
        title: str,
        agent_code: str,
        workflow_name: str,
        question: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        now = _now()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT * FROM conversations WHERE user_id = ? AND thread_id = ?",
                (user_id, thread_id),
            )
            row = await cursor.fetchone()
            if row is None:
                runtime_metadata = {
                    "agent_runtime": {
                        "agent_code": agent_code,
                        "workflow_name": workflow_name,
                        "last_question": str(question or "")[:500],
                        "request_metadata": metadata or {},
                        "created_at": now.isoformat(),
                    }
                }
                await connection.execute(
                    """
                    INSERT INTO conversations(
                        id, user_id, thread_id, title, metadata,
                        is_deleted, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        user_id,
                        thread_id,
                        title,
                        encode_metadata(runtime_metadata),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                await connection.commit()
                return

            record = self._record(row)
            if record.is_deleted:
                await connection.rollback()
                raise ConversationDeletedError(
                    f"Conversation thread has been deleted: {thread_id}"
                )
            base_meta = dict(record.metadata_ or {})
            runtime_meta = dict(base_meta.get("agent_runtime") or {})
            owner = str(runtime_meta.get("agent_code") or "").strip().lower()
            requested = str(agent_code or "").strip().lower()
            if owner and owner != requested:
                await connection.rollback()
                raise ConversationAgentMismatchError(
                    "Conversation thread belongs to a different agent: "
                    f"thread_id={thread_id} owner={owner} requested={requested}"
                )
            runtime_meta.update(
                {
                    "agent_code": agent_code,
                    "workflow_name": workflow_name,
                    "last_question": str(question or "")[:500],
                    "updated_at": now.isoformat(),
                }
            )
            if metadata:
                runtime_meta["request_metadata"] = metadata
            base_meta["agent_runtime"] = runtime_meta
            resolved_title = (
                title if not record.title or record.title == "新对话" else record.title
            )
            await connection.execute(
                """
                UPDATE conversations
                SET title = ?, metadata = ?, updated_at = ?
                WHERE user_id = ? AND thread_id = ?
                """,
                (
                    resolved_title,
                    encode_metadata(base_meta),
                    now.isoformat(),
                    user_id,
                    thread_id,
                ),
            )
            await connection.commit()

    async def rename(self, *, user_id: str, thread_id: str, title: str) -> bool:
        """返回是否有活跃行被改名；软删行静默冲突时返回 False（不误报成功）。"""
        now = _now().isoformat()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT id FROM conversations WHERE user_id = ? AND thread_id = ? AND is_deleted = 0",
                (user_id, thread_id),
            )
            if await cursor.fetchone():
                await connection.execute(
                    "UPDATE conversations SET title = ?, updated_at = ? WHERE user_id = ? AND thread_id = ?",
                    (title, now, user_id, thread_id),
                )
                updated = True
            else:
                await connection.execute(
                    """
                    INSERT INTO conversations(
                        id, user_id, thread_id, title, metadata,
                        is_deleted, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, 0, ?, ?)
                    ON CONFLICT(user_id, thread_id) DO NOTHING
                    """,
                    (str(uuid.uuid4()), user_id, thread_id, title, now, now),
                )
                updated = connection.total_changes > 0
            await connection.commit()
            return updated

    async def set_deleted(
        self,
        *,
        user_id: str,
        thread_id: str,
        deleted: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._connect() as connection:
            if metadata is None:
                await connection.execute(
                    """
                    UPDATE conversations SET is_deleted = ?, updated_at = ?
                    WHERE user_id = ? AND thread_id = ?
                    """,
                    (int(deleted), _now().isoformat(), user_id, thread_id),
                )
            else:
                await connection.execute(
                    """
                    UPDATE conversations SET is_deleted = ?, metadata = ?, updated_at = ?
                    WHERE user_id = ? AND thread_id = ?
                    """,
                    (
                        int(deleted),
                        encode_metadata(metadata),
                        _now().isoformat(),
                        user_id,
                        thread_id,
                    ),
                )
            await connection.commit()

    async def merge_metadata_value(
        self,
        *,
        user_id: str,
        thread_id: str,
        key: str,
        value: Any,
    ) -> bool:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT metadata FROM conversations WHERE user_id = ? AND thread_id = ? AND is_deleted = 0",
                (user_id, thread_id),
            )
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                return False
            metadata = _decode_metadata(row["metadata"]) or {}
            metadata[key] = value
            await connection.execute(
                """
                UPDATE conversations SET metadata = ?, updated_at = ?
                WHERE user_id = ? AND thread_id = ?
                """,
                (
                    encode_metadata(metadata),
                    _now().isoformat(),
                    user_id,
                    thread_id,
                ),
            )
            await connection.commit()
            return True

    async def merge_metadata_value_with(
        self,
        *,
        user_id: str,
        thread_id: str,
        key: str,
        merge: Any,
    ) -> bool:
        """Replace one metadata key with ``merge(previous_value)``.

        Unlike :meth:`merge_metadata_value`, the merge callback runs inside
        the row transaction with the current stored value, so two concurrent
        writers cannot both base their merge on the same stale snapshot and
        silently drop each other's contribution (load-merge-save races on
        ``agent_memory``). The callback must be synchronous and fast.
        """

        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT metadata FROM conversations WHERE user_id = ? AND thread_id = ? AND is_deleted = 0",
                (user_id, thread_id),
            )
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                return False
            metadata = _decode_metadata(row["metadata"]) or {}
            try:
                metadata[key] = merge(metadata.get(key))
            except Exception:
                await connection.rollback()
                raise
            await connection.execute(
                """
                UPDATE conversations SET metadata = ?, updated_at = ?
                WHERE user_id = ? AND thread_id = ?
                """,
                (
                    encode_metadata(metadata),
                    _now().isoformat(),
                    user_id,
                    thread_id,
                ),
            )
            await connection.commit()
            return True

    async def discard_metadata_value(
        self,
        *,
        user_id: str,
        thread_id: str,
        key: str,
    ) -> bool:
        """Remove one metadata key without rewriting the rest of the document.

        The read-modify-write happens inside the row transaction, so a
        concurrent writer's unrelated keys survive; passing a snapshot taken
        seconds earlier to ``set_deleted`` would drop them.
        """

        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT metadata FROM conversations WHERE user_id = ? AND thread_id = ?",
                (user_id, thread_id),
            )
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                return False
            metadata = _decode_metadata(row["metadata"]) or {}
            if key not in metadata:
                await connection.rollback()
                return True
            metadata.pop(key, None)
            await connection.execute(
                """
                UPDATE conversations SET metadata = ?, updated_at = ?
                WHERE user_id = ? AND thread_id = ?
                """,
                (
                    encode_metadata(metadata),
                    _now().isoformat(),
                    user_id,
                    thread_id,
                ),
            )
            await connection.commit()
            return True

    async def merge_registered_images(
        self,
        *,
        user_id: str,
        thread_id: str,
        registered_images: dict[str, Any],
    ) -> bool:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT metadata FROM conversations WHERE user_id = ? AND thread_id = ? AND is_deleted = 0",
                (user_id, thread_id),
            )
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                return False
            metadata = _decode_metadata(row["metadata"]) or {}
            existing = dict(metadata.get("registered_images") or {})
            existing.update(registered_images)
            metadata["registered_images"] = existing
            await connection.execute(
                """
                UPDATE conversations SET metadata = ?, updated_at = ?
                WHERE user_id = ? AND thread_id = ?
                """,
                (
                    encode_metadata(metadata),
                    _now().isoformat(),
                    user_id,
                    thread_id,
                ),
            )
            await connection.commit()
            return True

    async def set_pinned(
        self,
        *,
        user_id: str,
        thread_id: str,
        pinned: bool,
    ) -> bool:
        """置顶/取消置顶。不改 updated_at：取消置顶不应把旧会话顶到最近列表首位。"""
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT metadata FROM conversations WHERE user_id = ? AND thread_id = ? AND is_deleted = 0",
                (user_id, thread_id),
            )
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                return False
            metadata = _decode_metadata(row["metadata"]) or {}
            if pinned:
                metadata["pinned_at"] = _now().isoformat()
            else:
                metadata.pop("pinned_at", None)
            await connection.execute(
                """
                UPDATE conversations SET metadata = ?
                WHERE user_id = ? AND thread_id = ?
                """,
                (encode_metadata(metadata or None), user_id, thread_id),
            )
            await connection.commit()
            return True

    async def touch(self, *, user_id: str, thread_id: str) -> None:
        async with self._connect() as connection:
            await connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE user_id = ? AND thread_id = ?",
                (_now().isoformat(), user_id, thread_id),
            )
            await connection.commit()
