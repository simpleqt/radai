"""数据库连接管理"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from backend.config.settings import settings
from backend.db.models import Base

_engine = None
_async_engine = None
_session_factory = None
_async_session_factory = None


def get_engine():
    """获取同步引擎"""
    global _engine

    if _engine is None:
        _engine = create_engine(
            settings.mysql_url,
            poolclass=QueuePool,
            pool_size=settings.mysql_pool_size,
            max_overflow=settings.mysql_max_overflow,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=False,
        )

        @event.listens_for(_engine, "connect")
        def set_charset(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("SET NAMES utf8mb4")
            cursor.close()

    return _engine


def get_session_factory():
    """获取同步会话工厂"""
    global _session_factory

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

    if _async_engine is None:
        # Use NullPool for async engine to avoid "Future attached to a different loop"
        # errors that occur when connections checked out in one event loop (e.g.,
        # subagent thread's persistent loop) are later used in another loop
        # (e.g., the main ASGI event loop). NullPool creates a new connection
        # for each checkout and closes it on return, preventing cross-loop
        # connection sharing issues.
        _async_engine = create_async_engine(
            settings.async_mysql_url,
            poolclass=NullPool,
            pool_pre_ping=False,
            echo=False,
        )

    return _async_engine


def get_async_session_factory():
    """获取异步会话工厂"""
    global _async_session_factory

    if _async_session_factory is None:
        engine = get_async_engine()
        _async_session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )

    return _async_session_factory


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """获取异步会话"""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def async_session_context() -> AsyncGenerator[AsyncSession, None]:
    """异步会话上下文管理器"""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
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
    global _async_engine

    if _async_engine:
        await _async_engine.dispose()
        _async_engine = None
