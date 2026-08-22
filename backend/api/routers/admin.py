"""统一管理员后台 API。

这些接口补齐通用后台管理能力：用户、API Key、审计、总览、任务与系统状态。
"""

from __future__ import annotations


from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, require_admin
from backend.application.conversations.local_catalog import (
    get_local_conversation_catalog,
)
from backend.config.loggers import get_logger
from backend.db.models import (
    AdminAuditLog,
    ApiKey,
    Conversation,
    Document,
    ExtractionResult,
    KnowledgeBase,
    Task,
    TaskStatus,
    User,
)

from backend.api.schemas.admin import (  # noqa: F401  (含测试兼容 re-export)
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyItem,
    ApiKeyListResponse,
    ApiKeyUpdateRequest,
    AuditLogItem,
    AuditLogListResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SystemComponent,
    TaskAdminItem,
    TaskListResponse,
    UserAdminItem,
    UserCreateRequest,
    UserCreateResponse,
    UserListResponse,
    UserUpdateRequest,
)
from backend.api.routers import admin_accounts as _admin_accounts
from backend.api.routers import admin_operations as _admin_operations

router = APIRouter(prefix="/admin", tags=["管理员后台"])
router.include_router(_admin_accounts.router)
router.include_router(_admin_operations.router)
logger = get_logger(__name__)


@router.get("/overview")
async def get_admin_overview(
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    task_stats = {
        "total": 0,
        TaskStatus.PENDING.value: 0,
        TaskStatus.PROCESSING.value: 0,
        TaskStatus.COMPLETED.value: 0,
        TaskStatus.FAILED.value: 0,
        TaskStatus.CANCELLED.value: 0,
    }
    task_rows = await session.execute(
        select(Task.status, func.count(Task.task_id)).group_by(Task.status)
    )
    for status, count in task_rows.all():
        task_stats[str(status)] = int(count or 0)
        task_stats["total"] += int(count or 0)

    recent_audits = await session.execute(
        select(AdminAuditLog, User.username)
        .outerjoin(User, User.user_id == AdminAuditLog.actor_user_id)
        .order_by(AdminAuditLog.created_at.desc())
        .limit(10)
    )

    local_catalog = get_local_conversation_catalog()
    if local_catalog is not None:
        conversation_stats = await local_catalog.stats()
        conversation_overview = {
            "total": conversation_stats.total,
            "active": conversation_stats.active,
        }
    else:
        conversation_overview = {
            "total": await session.scalar(
                select(func.count()).select_from(Conversation)
            )
            or 0,
            "active": await session.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.is_deleted.is_(False))
            )
            or 0,
        }

    return {
        "users": {
            "total": await session.scalar(select(func.count()).select_from(User)) or 0,
            "active": await session.scalar(
                select(func.count()).select_from(User).where(User.is_active == True)  # noqa: E712
            )
            or 0,
            "admins": await session.scalar(
                select(func.count()).select_from(User).where(User.role == "admin")
            )
            or 0,
        },
        "api_keys": {
            "total": await session.scalar(select(func.count()).select_from(ApiKey))
            or 0,
            "active": await session.scalar(
                select(func.count()).select_from(ApiKey).where(ApiKey.is_active == True)  # noqa: E712
            )
            or 0,
        },
        "knowledge_bases": {
            "total": await session.scalar(
                select(func.count()).select_from(KnowledgeBase)
            )
            or 0,
            "documents": await session.scalar(
                select(func.coalesce(func.sum(KnowledgeBase.doc_count), 0))
            )
            or 0,
            "chunks": await session.scalar(
                select(func.coalesce(func.sum(KnowledgeBase.chunk_count), 0))
            )
            or 0,
            "entities": await session.scalar(
                select(func.coalesce(func.sum(KnowledgeBase.entity_count), 0))
            )
            or 0,
            "relations": await session.scalar(
                select(func.coalesce(func.sum(KnowledgeBase.relation_count), 0))
            )
            or 0,
        },
        "documents": {
            "total": await session.scalar(select(func.count()).select_from(Document))
            or 0,
            "extraction_results": await session.scalar(
                select(func.count()).select_from(ExtractionResult)
            )
            or 0,
        },
        "conversations": conversation_overview,
        "tasks": task_stats,
        "recent_audits": [
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
            for log, username in recent_audits.all()
        ],
    }
