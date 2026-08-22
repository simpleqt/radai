import logging
import os
from collections.abc import AsyncIterator, Sequence
from urllib.parse import quote
from pathlib import Path
from typing import Any

from fastapi import Request
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

logger = logging.getLogger(__name__)

_READ_CONNECTIONS_ENV = "AGENT_CHECKPOINT_SQLITE_READ_CONNECTIONS"


def _sqlite_read_connections() -> int:
    """Reader-connection count for the WAL read/write split (0 = legacy)."""
    try:
        value = int(os.getenv(_READ_CONNECTIONS_ENV, "2") or "2")
    except (TypeError, ValueError):
        return 2
    return max(0, min(8, value))


class ReadRoutedAsyncSqliteSaver(AsyncSqliteSaver):
    """WAL 读写连接分离的 SQLite checkpointer。

    整个应用此前共享一条 aiosqlite 连接(其内部单线程串行执行所有语句),
    长会话的历史读取会把所有并发流的 checkpoint 写入堵在同一条队列后面。
    WAL 模式下读不阻塞写:写/删/裁剪仍走本类自身的写连接,`aget_tuple`
    与 `alist` 轮转到独立读连接上执行。本类仍是 AsyncSqliteSaver
    (isinstance/setup/GC 工具语义不变),读 saver 首次使用时的幂等
    setup() 只是重复执行 CREATE TABLE IF NOT EXISTS,无害。
    """

    def __init__(
        self,
        conn: Any,
        readers: Sequence[AsyncSqliteSaver],
        *,
        serde: Any | None = None,
    ) -> None:
        super().__init__(conn, serde=serde)
        if not readers:
            raise ValueError("ReadRoutedAsyncSqliteSaver requires at least one reader")
        self._readers = list(readers)
        self._reader_cursor = 0

    def _next_reader(self) -> AsyncSqliteSaver:
        # 只在事件循环线程上调用;偶发交错最多导致轮转不均,无正确性影响。
        self._reader_cursor = (self._reader_cursor + 1) % len(self._readers)
        return self._readers[self._reader_cursor]

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await self._next_reader().aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        reader = self._next_reader()
        async for item in reader.alist(
            config,
            filter=filter,
            before=before,
            limit=limit,
        ):
            yield item


class CheckpointerConnections:
    """Close-handle covering the writer plus all reader connections."""

    def __init__(self, connections: Sequence[Any]) -> None:
        self._connections = list(connections)

    async def close(self) -> None:
        for connection in self._connections:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001 -- shutdown must close every conn
                logger.warning("Checkpointer 连接关闭失败", exc_info=True)


async def init_sqlite_checkpointer(
    db_name: str,
    data_dir: str | Path | None = None,
) -> tuple[AsyncSqliteSaver, Any, str]:
    """创建 SQLite checkpointer，并返回 saver、连接句柄和数据库路径。"""
    import aiosqlite

    checkpoint_dir = (
        Path(data_dir)
        if data_dir is not None
        else Path(__file__).resolve().parents[2] / "data"
    )
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    db_path = checkpoint_dir / db_name

    conn = await aiosqlite.connect(str(db_path))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=5000")

    readers_count = _sqlite_read_connections()
    if readers_count <= 0:
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        return saver, conn, str(db_path)

    reader_conns = []
    for _ in range(readers_count):
        reader_conn = await aiosqlite.connect(str(db_path))
        await reader_conn.execute("PRAGMA busy_timeout=5000")
        reader_conns.append(reader_conn)
    pooled_saver = ReadRoutedAsyncSqliteSaver(
        conn,
        [AsyncSqliteSaver(reader_conn) for reader_conn in reader_conns],
    )
    await pooled_saver.setup()
    return pooled_saver, CheckpointerConnections([conn, *reader_conns]), str(db_path)


async def init_app_sqlite_checkpointer(
    app: Any,
    db_name: str,
    data_dir: str | Path | None = None,
) -> str:
    """初始化 SQLite checkpointer 并挂载到 FastAPI app.state。"""
    saver, conn, db_path = await init_sqlite_checkpointer(db_name, data_dir)
    app.state.checkpointer = saver
    app.state.checkpointer_conn = conn
    app.state.checkpoint_db = db_path
    app.state.checkpointer_backend = "sqlite"
    return db_path


def _redis_checkpoint_url() -> str:
    from backend.config.settings import settings

    password = quote(str(settings.redis_password or ""), safe="")
    auth = f":{password}@" if password else ""
    return (
        f"redis://{auth}{settings.redis_host}:{settings.redis_port}/"
        f"{settings.redis_db}"
    )


async def init_app_redis_checkpointer(app: Any) -> str:
    """Initialize the shared Redis checkpointer for multi-worker API runs."""

    from langgraph.checkpoint.redis.aio import AsyncRedisSaver
    from backend.config.settings import settings

    redis_url = _redis_checkpoint_url()
    saver = AsyncRedisSaver(
        redis_url=redis_url,
        checkpoint_prefix=os.getenv(
            "AGENT_CHECKPOINT_REDIS_PREFIX",
            "agent-checkpoint",
        ),
    )
    await saver.setup()
    app.state.checkpointer = saver
    app.state.checkpointer_conn = None
    app.state.checkpoint_db = None
    app.state.checkpointer_backend = "redis"
    # Keep the password out of app state and logs while exposing a useful
    # readiness diagnostic.
    return f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"


async def init_app_checkpointer(
    app: Any,
    db_name: str,
    data_dir: str | Path | None = None,
) -> str:
    """Select the checkpoint backend without changing the request API.

    SQLite remains the default for single-worker/local deployments. Redis is
    the shared backend for multiple API workers; an unknown value fails at
    startup instead of silently falling back to local state.
    """

    backend = os.getenv("AGENT_CHECKPOINTER_BACKEND", "sqlite").strip().lower()
    if backend in {"sqlite", "sqlite3", "local"}:
        return await init_app_sqlite_checkpointer(app, db_name, data_dir)
    if backend in {"redis", "shared-redis"}:
        return await init_app_redis_checkpointer(app)
    raise ValueError(
        "AGENT_CHECKPOINTER_BACKEND must be sqlite or redis"
    )


async def cleanup_app_sqlite_checkpointer(app: Any) -> bool:
    """关闭挂载在 app.state 上的 SQLite checkpointer 连接。"""
    conn = getattr(app.state, "checkpointer_conn", None)
    if conn is None:
        return False
    await conn.close()
    app.state.checkpointer_conn = None
    return True


async def cleanup_app_checkpointer(app: Any) -> bool:
    """Close whichever configured checkpointer backend owns app state."""

    backend = str(getattr(app.state, "checkpointer_backend", "sqlite") or "sqlite")
    if backend != "redis":
        return await cleanup_app_sqlite_checkpointer(app)
    saver = getattr(app.state, "checkpointer", None)
    if saver is None:
        return False
    exit_method = getattr(saver, "__aexit__", None)
    if callable(exit_method):
        await exit_method(None, None, None)
    app.state.checkpointer = None
    app.state.checkpointer_backend = None
    return True


def get_checkpointer_from_request(request: Request) -> Any:
    """从 FastAPI Request 中获取全局共享的 checkpointer。"""
    if not hasattr(request.app.state, "checkpointer"):
        logger.error("Checkpointer not found in app.state")
        raise RuntimeError("Checkpointer not initialized")
    return request.app.state.checkpointer
