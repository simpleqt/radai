"""管理员后台:审计日志、任务管理与系统健康路由(由 admin.py 主 router include)。"""

from __future__ import annotations

from fastapi import APIRouter
from backend.config.loggers import get_logger

import asyncio

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, require_admin
from backend.config.settings import settings
from backend.db.models import AdminAuditLog, User
from backend.services.admin_audit import write_admin_audit
from datetime import datetime
from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy import case, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from typing import Any
from urllib.parse import urlparse
from backend.api.schemas.admin import (
    AuditLogItem,
    AuditLogListResponse,
    SystemComponent,
)


logger = get_logger(__name__)
router = APIRouter()


def _task_item(row: Any) -> TaskAdminItem:
    task, filename, username = row
    return TaskAdminItem(
        task_id=task.task_id,
        doc_id=task.doc_id,
        filename=filename,
        user_id=task.user_id,
        username=username,
        batch_id=task.batch_id,
        status=task.status,
        progress=int(task.progress or 0),
        priority=int(task.priority or 0),
        retry_count=int(task.retry_count or 0),
        max_retries=int(task.max_retries or 0),
        error=task.error,
        error_detail=task.error_detail,
        created_at=task.created_at,
        updated_at=task.updated_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
    )


async def _write_audit(
    session: AsyncSession,
    request: Request,
    actor: ApiKeyAuth,
    *,
    action: str,
    target_type: str,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    status: str = "success",
) -> None:
    await write_admin_audit(
        session,
        request,
        actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
        status=status,
    )


@router.get("/audit-logs", response_model=AuditLogListResponse)
async def list_admin_audit_logs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    actor_user_id: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    status: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    stmt = select(AdminAuditLog, User.username).outerjoin(
        User, User.user_id == AdminAuditLog.actor_user_id
    )
    if actor_user_id:
        stmt = stmt.where(AdminAuditLog.actor_user_id == actor_user_id)
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
    if target_type:
        stmt = stmt.where(AdminAuditLog.target_type == target_type)
    if target_id:
        stmt = stmt.where(AdminAuditLog.target_id == target_id)
    if status:
        stmt = stmt.where(AdminAuditLog.status == status)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(AdminAuditLog.created_at.desc()).offset(offset).limit(limit)
    )
    items = [
        AuditLogItem(
            audit_id=log.audit_id,
            actor_user_id=log.actor_user_id,
            actor_username=username,
            action=log.action,
            target_type=log.target_type,
            target_id=log.target_id,
            status=log.status,
            method=log.method,
            path=log.path,
            ip_address=log.ip_address,
            user_agent=log.user_agent,
            detail_json=log.detail_json,
            created_at=log.created_at,
        )
        for log, username in rows.all()
    ]
    return AuditLogListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/audit-logs/{audit_id}", response_model=AuditLogItem)
async def get_admin_audit_log(
    audit_id: int,
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    result = await session.execute(
        select(AdminAuditLog, User.username)
        .outerjoin(User, User.user_id == AdminAuditLog.actor_user_id)
        .where(AdminAuditLog.audit_id == audit_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="审计记录不存在")
    log, username = row
    return AuditLogItem(
        audit_id=log.audit_id,
        actor_user_id=log.actor_user_id,
        actor_username=username,
        action=log.action,
        target_type=log.target_type,
        target_id=log.target_id,
        status=log.status,
        method=log.method,
        path=log.path,
        ip_address=log.ip_address,
        user_agent=log.user_agent,
        detail_json=log.detail_json,
        created_at=log.created_at,
    )


@router.get("/system/health", response_model=dict[str, SystemComponent])
async def get_admin_system_health(
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    components: dict[str, SystemComponent] = {}

    async def tcp_check(endpoint: str, default_port: int) -> tuple[bool, str]:
        parsed = urlparse(endpoint if "://" in endpoint else f"tcp://{endpoint}")
        host = parsed.hostname or endpoint.split(":", 1)[0]
        port = parsed.port or default_port
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1.5
            )
            writer.close()
            await writer.wait_closed()
            return True, f"{host}:{port}"
        except Exception as exc:
            return False, f"{host}:{port} - {type(exc).__name__}"

    try:
        await session.execute(text("SELECT 1"))
        components["database"] = SystemComponent(status="ok")
    except Exception as exc:
        # Report the failure class only: the raw driver message can carry
        # hosts, ports and connection-string fragments.
        logger.warning("管理端健康检查数据库探测失败", exc_info=True)
        components["database"] = SystemComponent(
            status="error", detail=type(exc).__name__
        )

    try:
        from backend.db.redis import get_redis

        redis_client = get_redis()
        try:
            pong = await redis_client.ping()
        finally:
            await redis_client.aclose()
        components["redis"] = SystemComponent(status="ok" if pong else "error")
    except Exception as exc:
        logger.warning("管理端健康检查 Redis 探测失败", exc_info=True)
        components["redis"] = SystemComponent(
            status="error", detail=type(exc).__name__
        )

    try:
        from backend.storage import storage_manager

        stats = await asyncio.to_thread(storage_manager.get_storage_stats)
        components["local_storage"] = SystemComponent(status="ok", metadata=stats)
    except Exception as exc:
        logger.warning("管理端健康检查本地存储探测失败", exc_info=True)
        components["local_storage"] = SystemComponent(
            status="error", detail=type(exc).__name__
        )

    kafka_endpoint = str(settings.kafka_bootstrap_servers).split(",", 1)[0].strip()
    nebula_endpoint = f"{settings.nebula_host}:{settings.nebula_graph_port}"
    (
        (kafka_ok, kafka_detail),
        (minio_ok, minio_detail),
        (nebula_ok, nebula_detail),
    ) = await asyncio.gather(
        tcp_check(kafka_endpoint, 9092),
        tcp_check(str(settings.minio_endpoint), 9000),
        tcp_check(nebula_endpoint, 9669),
    )
    components["kafka"] = SystemComponent(
        status="ok" if kafka_ok else "error", detail=kafka_detail
    )

    components["minio"] = SystemComponent(
        status="ok" if minio_ok else "error",
        detail=minio_detail,
        metadata={"bucket": settings.minio_bucket_name},
    )

    components["nebula_graph"] = SystemComponent(
        status="ok" if nebula_ok else "error", detail=nebula_detail
    )

    task_counts = dict(
        (
            await session.execute(
                select(Task.status, func.count()).group_by(Task.status)
            )
        ).all()
    )
    components["task_queue"] = SystemComponent(
        status="ok"
        if int(task_counts.get(TaskStatus.FAILED.value, 0)) == 0
        else "degraded",
        metadata={str(key): int(value) for key, value in task_counts.items()},
    )

    components["configuration"] = SystemComponent(
        status="ok",
        metadata={
            "mysql_host": settings.mysql_host,
            "mysql_database": settings.mysql_database,
            "redis_host": settings.redis_host,
            "minio_endpoint": settings.minio_endpoint,
            "log_dir": settings.log_dir,
        },
    )
    return components
