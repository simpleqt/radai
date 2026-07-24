"""数据库连接管理"""

import asyncio
import os
import random
import threading
import weakref
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

import anyio
from sqlalchemy import create_engine, event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool
from starlette.requests import Request

from backend.config.settings import settings
from backend.db.models import Base
from backend.db.unit_of_work import mark_outer_transaction_managed

_engine = None
_async_engine = None
_session_factory = None
_async_session_factory = None
_async_engines_by_loop: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_async_factories_by_loop: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_async_registry_lock = threading.RLock()
_sync_registry_lock = threading.RLock()
_T = TypeVar("_T")
MYSQL_RETRYABLE_ERROR_CODES = {1205, 1213}


def _mysql_pool_timeout_seconds() -> float:
    try:
        return max(
            0.1,
            float(os.getenv("MYSQL_POOL_TIMEOUT_SECONDS", "5") or "5"),
        )
    except (TypeError, ValueError):
        return 5.0


@asynccontextmanager
async def _cancellation_safe_context(context_manager):
    """Keep async resource teardown running after an AnyIO request cancellation.

    Starlette's ``BaseHTTPMiddleware`` and streaming responses use AnyIO cancel
    scopes when the client disconnects.  SQLAlchemy's async context-manager
    exit performs database I/O, so an unshielded exit can be cancelled halfway
    through and make ``AsyncAdaptedQueuePool`` log an ``ERROR`` while terminating
    an otherwise expected request connection.
    """
    resource = await context_manager.__aenter__()
    try:
        yield resource
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            suppress = await context_manager.__aexit__(
                type(exc), exc, exc.__traceback__
            )
        if suppress:
            return
        raise
    else:
        with anyio.CancelScope(shield=True):
            await context_manager.__aexit__(None, None, None)


async def _shielded_db_operation(operation: Awaitable[_T]) -> _T:
    """Run rollback/commit while allowing the original cancellation to re-raise."""
    with anyio.CancelScope(shield=True):
        return await operation


def _current_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def is_retryable_mysql_error(exc: BaseException) -> bool:
    """Return True for transient MySQL lock errors worth retrying."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        orig = getattr(current, "orig", None)
        args = getattr(orig or current, "args", ())
        if args:
            try:
                if int(args[0]) in MYSQL_RETRYABLE_ERROR_CODES:
                    return True
            except (TypeError, ValueError):
                pass
        if isinstance(current, DBAPIError) and getattr(current, "connection_invalidated", False):
            return True
        current = current.__cause__ or current.__context__
    return False


async def retry_async_db_operation(
    operation: Callable[[], Awaitable[_T]],
    *,
    attempts: int = 4,
    base_delay: float = 0.2,
    label: str = "db operation",
    logger=None,
) -> _T:
    """Retry a fresh async DB operation after MySQL deadlock/lock-timeout rollback."""
    attempts = max(int(attempts or 1), 1)
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt >= attempts or not is_retryable_mysql_error(exc):
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)
            if logger is not None:
                logger.warning(
                    "%s failed with retryable MySQL error, retrying %d/%d in %.2fs: %s",
                    label,
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable retry loop state")


def _create_pooled_async_engine():
    return create_async_engine(
        settings.async_mysql_url,
        pool_size=settings.mysql_pool_size,
        max_overflow=settings.mysql_max_overflow,
        pool_timeout=_mysql_pool_timeout_seconds(),
        pool_pre_ping=True,
        pool_recycle=3600,
        echo=False,
    )


def get_engine():
    """获取同步引擎"""
    global _engine

    if _engine is None:
        with _sync_registry_lock:
            if _engine is None:
                engine = create_engine(
                    settings.mysql_url,
                    poolclass=QueuePool,
                    pool_size=settings.mysql_pool_size,
                    max_overflow=settings.mysql_max_overflow,
                    pool_timeout=_mysql_pool_timeout_seconds(),
                    pool_pre_ping=True,
                    pool_recycle=3600,
                    echo=False,
                )

                @event.listens_for(engine, "connect")
                def set_charset(dbapi_connection, _connection_record):
                    cursor = dbapi_connection.cursor()
                    cursor.execute("SET NAMES utf8mb4")
                    cursor.close()

                _engine = engine

    return _engine


def get_session_factory():
    """获取同步会话工厂"""
    global _session_factory

    if _session_factory is None:
        with _sync_registry_lock:
            if _session_factory is None:
                engine = get_engine()
                _session_factory = sessionmaker(
                    bind=engine,
                    autocommit=False,
                    autoflush=False,
                )

    return _session_factory


def get_session() -> Session:
    """获取同步会话"""
    factory = get_session_factory()
    return factory()


def get_async_engine():
    """获取异步引擎"""
    global _async_engine

    loop = _current_loop()
    if loop is None:
        # CLI/bootstrap code may construct a factory before an event loop exists.
        # Keep that exceptional path unpooled so it is never shared across loops.
        if _async_engine is None:
            _async_engine = create_async_engine(
                settings.async_mysql_url,
                poolclass=NullPool,
                pool_pre_ping=False,
                echo=False,
            )
        return _async_engine

    with _async_registry_lock:
        engine = _async_engines_by_loop.get(loop)
        if engine is None:
            engine = _create_pooled_async_engine()
            _async_engines_by_loop[loop] = engine
        return engine


def get_async_session_factory():
    """获取异步会话工厂"""
    global _async_session_factory

    loop = _current_loop()
    if loop is None:
        if _async_session_factory is None:
            _async_session_factory = async_sessionmaker(
                bind=get_async_engine(),
                class_=AsyncSession,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            )
        return _async_session_factory

    with _async_registry_lock:
        factory = _async_factories_by_loop.get(loop)
        if factory is None:
            factory = async_sessionmaker(
                bind=get_async_engine(),
                class_=AsyncSession,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            )
            _async_factories_by_loop[loop] = factory
        return factory


async def get_async_session(
    request: Request = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Provide a request-scoped unit of work.

    Safe HTTP methods always roll back their read transaction; mutating methods
    commit once after the endpoint succeeds. Programmatic callers without a
    request retain the legacy commit-on-success behavior.
    """
    factory = get_async_session_factory()
    async with _cancellation_safe_context(factory()) as session:
        mark_outer_transaction_managed(session)
        try:
            yield session
            if request is not None and request.method.upper() in {
                "GET",
                "HEAD",
                "OPTIONS",
            }:
                await session.rollback()
            else:
                await session.commit()
        except BaseException:
            await _shielded_db_operation(session.rollback())
            raise


@asynccontextmanager
async def read_only_session_context() -> AsyncGenerator[AsyncSession, None]:
    """Provide a session that never commits and always releases its transaction."""
    factory = get_async_session_factory()
    async with _cancellation_safe_context(factory()) as session:
        mark_outer_transaction_managed(session)
        try:
            yield session
        finally:
            await _shielded_db_operation(session.rollback())


@asynccontextmanager
async def transactional_session_context() -> AsyncGenerator[AsyncSession, None]:
    """Provide an explicit transaction boundary for application write services."""
    factory = get_async_session_factory()
    async with _cancellation_safe_context(factory()) as session:
        mark_outer_transaction_managed(session)
        async with _cancellation_safe_context(session.begin()):
            yield session


@asynccontextmanager
async def async_session_context() -> AsyncGenerator[AsyncSession, None]:
    """异步会话上下文管理器"""
    factory = get_async_session_factory()
    async with _cancellation_safe_context(factory()) as session:
        mark_outer_transaction_managed(session)
        try:
            yield session
            await session.commit()
        except BaseException:
            await _shielded_db_operation(session.rollback())
            raise


def init_db():
    """初始化数据库表"""

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    print("数据库表初始化完成")


def close_db():
    """关闭数据库连接"""
    global _engine, _async_engine, _session_factory, _async_session_factory

    if _engine:
        _engine.dispose()
        _engine = None

    if _async_engine:
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_async_engine.dispose())
            else:
                loop.run_until_complete(_async_engine.dispose())
        except Exception:
            pass
        _async_engine = None

    _session_factory = None
    _async_session_factory = None


async def close_db_async():
    """异步关闭数据库连接"""
    global _async_engine, _async_session_factory

    if _async_engine:
        await _async_engine.dispose()
        _async_engine = None
        _async_session_factory = None
    loop = _current_loop()
    if loop is not None:
        with _async_registry_lock:
            engine = _async_engines_by_loop.pop(loop, None)
            _async_factories_by_loop.pop(loop, None)
        if engine is not None:
            await engine.dispose()
