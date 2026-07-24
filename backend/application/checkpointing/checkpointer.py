import logging
import os
from urllib.parse import quote
from pathlib import Path
from typing import Any

from fastapi import Request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

logger = logging.getLogger(__name__)


async def init_sqlite_checkpointer(
    db_name: str,
    data_dir: str | Path | None = None,
) -> tuple[AsyncSqliteSaver, Any, str]:
    """创建 SQLite checkpointer，并返回 saver、连接和数据库路径。"""
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
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    return saver, conn, str(db_path)


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
