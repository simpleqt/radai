"""管理员后台相关表初始化。"""

from __future__ import annotations

from backend.config.loggers import get_logger
from backend.db.connection import get_async_engine
from backend.db.models import AdminAuditLog

logger = get_logger(__name__)


async def init_admin_tables() -> None:
    """创建管理员后台扩展表。"""
    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(AdminAuditLog.__table__.create, checkfirst=True)
    logger.info("[管理员后台] admin_audit_logs 表创建/确认成功")
