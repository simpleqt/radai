"""统一管理员后台 API。

这些接口补齐通用后台管理能力：用户、API Key、审计、总览、任务与系统状态。
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, require_admin
from backend.application.auth_security import revoke_user_sessions
from backend.application.auth_service import API_KEY_SCOPE_FULL
from backend.config.loggers import get_logger
from backend.config.settings import settings
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
from backend.db.redis import get_redis_client
from backend.services.admin_audit import write_admin_audit
from backend.services.task_dispatch_outbox import (
    TASK_DISPATCH_OUTBOX_KIND,
    OutboxBacklogFullError,
    build_task_dispatch_event,
    ensure_outbox_capacity,
    task_dispatch_outbox,
    task_dispatch_result,
)

router = APIRouter(prefix="/admin", tags=["管理员后台"])
logger = get_logger(__name__)

_ROLES = {"admin", "user"}


class UserAdminItem(BaseModel):
    user_id: str
    username: str
    email: str | None = None
    role: str
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_login: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UserCreateResponse(UserAdminItem):
    generated_password: str | None = None
    raw_api_key: str | None = None


class UserListResponse(BaseModel):
    items: list[UserAdminItem]
    total: int
    limit: int
    offset: int


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str | None = Field(default=None, min_length=6, max_length=256)
    email: str | None = Field(default=None, max_length=100)
    role: str = "user"
    is_active: bool = True
    create_api_key: bool = True


class UserUpdateRequest(BaseModel):
    email: str | None = Field(default=None, max_length=100)
    role: str | None = Field(default=None, max_length=20)
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str | None = Field(default=None, min_length=6, max_length=256)


class ResetPasswordResponse(BaseModel):
    success: bool
    generated_password: str | None = None


class ApiKeyItem(BaseModel):
    id: int
    api_key: str
    user_id: str | None = None
    username: str | None = None
    key_name: str | None = None
    access_scope: Literal["full", "mcp_only"]
    mcp_allowed_tools: list[str] | None = None
    is_active: bool
    rate_limit: int
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    usage_count: int


class ApiKeyListResponse(BaseModel):
    items: list[ApiKeyItem]
    total: int
    limit: int
    offset: int


class ApiKeyCreateRequest(BaseModel):
    key_name: str | None = Field(default=None, max_length=50)
    access_scope: Literal["full", "mcp_only"] = API_KEY_SCOPE_FULL
    mcp_allowed_tools: list[str] | None = None
    rate_limit: int = Field(default=100, ge=1, le=1000000)
    expires_at: datetime | None = None


class ApiKeyCreateResponse(ApiKeyItem):
    raw_api_key: str


class ApiKeyUpdateRequest(BaseModel):
    key_name: str | None = Field(default=None, max_length=50)
    access_scope: Literal["full", "mcp_only"] | None = None
    mcp_allowed_tools: list[str] | None = None
    is_active: bool | None = None
    rate_limit: int | None = Field(default=None, ge=1, le=1000000)
    expires_at: datetime | None = None


class AuditLogItem(BaseModel):
    audit_id: int
    actor_user_id: str | None = None
    actor_username: str | None = None
    action: str
    target_type: str
    target_id: str | None = None
    status: str
    method: str | None = None
    path: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    detail_json: dict[str, Any] | None = None
    created_at: datetime | None = None


class AuditLogListResponse(BaseModel):
    items: list[AuditLogItem]
    total: int
    limit: int
    offset: int


class TaskAdminItem(BaseModel):
    task_id: str
    doc_id: str
    filename: str | None = None
    user_id: str | None = None
    username: str | None = None
    batch_id: str | None = None
    status: str
    progress: int
    priority: int
    retry_count: int
    max_retries: int
    error: str | None = None
    error_detail: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TaskListResponse(BaseModel):
    items: list[TaskAdminItem]
    total: int
    limit: int
    offset: int


class SystemComponent(BaseModel):
    status: str
    detail: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


def _mask_secret(value: str | None) -> str:
    raw = str(value or "")
    if len(raw) <= 10:
        return "***" if raw else ""
    return f"{raw[:6]}...{raw[-4:]}"


def _generate_api_key() -> str:
    return f"ak_{secrets.token_urlsafe(32)}"


def _normalize_role(role: str | None) -> str:
    normalized = str(role or "user").lower().strip()
    if normalized not in _ROLES:
        raise HTTPException(status_code=400, detail=f"不支持的角色: {role}")
    return normalized


def _normalize_email(email: str | None) -> str | None:
    value = str(email or "").strip()
    return value or None


def _user_item(user: User) -> UserAdminItem:
    return UserAdminItem(
        user_id=user.user_id,
        username=user.username,
        email=user.email,
        role=user.role or "user",
        is_active=bool(user.is_active),
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login=user.last_login,
    )


def _api_key_item(row: Any, *, raw_api_key: str | None = None):
    if isinstance(row, ApiKey):
        api_key = row
        username = None
    else:
        api_key = row[0]
        username = row[1] if len(row) > 1 else None
    payload = {
        "id": api_key.id,
        "api_key": _mask_secret(api_key.api_key),
        "user_id": api_key.user_id,
        "username": username,
        "key_name": api_key.key_name,
        "access_scope": str(api_key.access_scope or API_KEY_SCOPE_FULL),
        "mcp_allowed_tools": api_key.mcp_allowed_tools,
        "is_active": bool(api_key.is_active),
        "rate_limit": int(api_key.rate_limit or 0),
        "created_at": api_key.created_at,
        "expires_at": api_key.expires_at,
        "last_used_at": api_key.last_used_at,
        "usage_count": int(api_key.usage_count or 0),
    }
    if raw_api_key is not None:
        return ApiKeyCreateResponse(**payload, raw_api_key=raw_api_key)
    return ApiKeyItem(**payload)


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

    return {
        "users": {
            "total": await session.scalar(select(func.count()).select_from(User)) or 0,
            "active": await session.scalar(
                select(func.count()).select_from(User).where(User.is_active == True)
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
                select(func.count()).select_from(ApiKey).where(ApiKey.is_active == True)
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
        "conversations": {
            "total": await session.scalar(
                select(func.count()).select_from(Conversation)
            )
            or 0,
            "active": await session.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.is_deleted == False)
            )
            or 0,
        },
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


@router.get("/users", response_model=UserListResponse)
async def list_admin_users(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, description="用户名/邮箱模糊搜索"),
    role: str | None = Query(None),
    is_active: bool | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    stmt = select(User)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.username.ilike(like), User.email.ilike(like)))
    if role:
        stmt = stmt.where(User.role == _normalize_role(role))
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(User.created_at.desc()).offset(offset).limit(limit)
    )
    return UserListResponse(
        items=[_user_item(row) for row in rows.scalars().all()],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/users", response_model=UserCreateResponse, status_code=201)
async def create_admin_user(
    payload: UserCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    from werkzeug.security import generate_password_hash

    username = payload.username.strip()
    email = _normalize_email(payload.email)
    role = _normalize_role(payload.role)

    existing = await session.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    if email:
        existing_email = await session.execute(select(User).where(User.email == email))
        if existing_email.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="邮箱已存在")

    generated_password = None
    raw_password = payload.password
    if not raw_password:
        generated_password = secrets.token_urlsafe(12)
        raw_password = generated_password

    raw_key = _generate_api_key()
    password_hash = await asyncio.to_thread(generate_password_hash, raw_password)
    user = User(
        username=username,
        password=password_hash,
        email=email,
        api_key=raw_key,
        role=role,
        is_active=payload.is_active,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    session.add(user)
    await session.flush()

    if payload.create_api_key:
        session.add(
            ApiKey(
                api_key=raw_key,
                user_id=user.user_id,
                key_name="默认 API Key",
                access_scope=API_KEY_SCOPE_FULL,
                is_active=True,
                rate_limit=1000 if role == "admin" else 100,
            )
        )

    await _write_audit(
        session,
        request,
        admin,
        action="user.create",
        target_type="user",
        target_id=user.user_id,
        detail={
            "username": username,
            "role": role,
            "create_api_key": payload.create_api_key,
        },
    )
    item = _user_item(user)
    return UserCreateResponse(
        **item.model_dump(),
        generated_password=generated_password,
        raw_api_key=raw_key if payload.create_api_key else None,
    )


@router.get("/users/{user_id}", response_model=UserAdminItem)
async def get_admin_user(
    user_id: str,
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return _user_item(user)


@router.patch("/users/{user_id}", response_model=UserAdminItem)
async def update_admin_user(
    user_id: str,
    payload: UserUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
    redis_client: Redis = Depends(get_redis_client),
):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    updates = payload.model_dump(exclude_unset=True)
    if "role" in updates and updates["role"] is not None:
        updates["role"] = _normalize_role(updates["role"])
        if user.user_id == admin.user_id and updates["role"] != "admin":
            raise HTTPException(
                status_code=400, detail="不能移除当前登录管理员的管理员角色"
            )
        if user.role == "admin" and updates["role"] != "admin":
            admin_count = (
                await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.role == "admin", User.is_active == True)
                )
                or 0
            )
            if admin_count <= 1:
                raise HTTPException(
                    status_code=400, detail="系统必须至少保留一个启用的管理员"
                )
    if "email" in updates:
        updates["email"] = _normalize_email(updates["email"])
        if updates["email"]:
            existing_email = await session.execute(
                select(User).where(
                    User.email == updates["email"], User.user_id != user_id
                )
            )
            if existing_email.scalar_one_or_none() is not None:
                raise HTTPException(status_code=409, detail="邮箱已存在")
    if updates.get("is_active") is False and user.user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="不能禁用当前登录管理员")

    for key, value in updates.items():
        setattr(user, key, value)
    user.updated_at = datetime.now()
    if "role" in updates or "is_active" in updates:
        await revoke_user_sessions(redis_client, user.user_id)

    await _write_audit(
        session,
        request,
        admin,
        action="user.update",
        target_type="user",
        target_id=user_id,
        detail=updates,
    )
    return _user_item(user)


@router.post("/users/{user_id}/reset-password", response_model=ResetPasswordResponse)
async def reset_admin_user_password(
    user_id: str,
    payload: ResetPasswordRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
    redis_client: Redis = Depends(get_redis_client),
):
    from werkzeug.security import generate_password_hash

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    generated_password = None
    new_password = payload.new_password
    if not new_password:
        generated_password = secrets.token_urlsafe(12)
        new_password = generated_password

    user.password = await asyncio.to_thread(generate_password_hash, new_password)
    user.updated_at = datetime.now()
    await revoke_user_sessions(redis_client, user.user_id)
    await _write_audit(
        session,
        request,
        admin,
        action="user.reset_password",
        target_type="user",
        target_id=user_id,
    )
    return ResetPasswordResponse(success=True, generated_password=generated_password)


@router.post("/users/{user_id}/enable", response_model=UserAdminItem)
async def enable_admin_user(
    user_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.is_active = True
    user.updated_at = datetime.now()
    await _write_audit(
        session,
        request,
        admin,
        action="user.enable",
        target_type="user",
        target_id=user_id,
    )
    return _user_item(user)


@router.post("/users/{user_id}/disable", response_model=UserAdminItem)
async def disable_admin_user(
    user_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
    redis_client: Redis = Depends(get_redis_client),
):
    if user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="不能禁用当前登录管理员")
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.role == "admin":
        admin_count = (
            await session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.role == "admin", User.is_active == True)
            )
            or 0
        )
        if admin_count <= 1:
            raise HTTPException(
                status_code=400, detail="系统必须至少保留一个启用的管理员"
            )
    user.is_active = False
    user.updated_at = datetime.now()
    await revoke_user_sessions(redis_client, user.user_id)
    await _write_audit(
        session,
        request,
        admin,
        action="user.disable",
        target_type="user",
        target_id=user_id,
    )
    return _user_item(user)


@router.get("/api-keys", response_model=ApiKeyListResponse)
async def list_admin_api_keys(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: str | None = None,
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    stmt = select(ApiKey, User.username).outerjoin(User, User.user_id == ApiKey.user_id)
    if user_id:
        stmt = stmt.where(ApiKey.user_id == user_id)
    if is_active is not None:
        stmt = stmt.where(ApiKey.is_active == is_active)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(ApiKey.created_at.desc()).offset(offset).limit(limit)
    )
    return ApiKeyListResponse(
        items=[_api_key_item(row) for row in rows.all()],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/users/{user_id}/api-keys", response_model=ApiKeyCreateResponse, status_code=201
)
async def create_admin_api_key(
    user_id: str,
    payload: ApiKeyCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    raw_key = _generate_api_key()
    record = ApiKey(
        api_key=raw_key,
        user_id=user_id,
        key_name=payload.key_name,
        access_scope=payload.access_scope,
        mcp_allowed_tools=payload.mcp_allowed_tools,
        is_active=True,
        rate_limit=payload.rate_limit,
        expires_at=payload.expires_at,
    )
    session.add(record)
    await session.flush()
    await _write_audit(
        session,
        request,
        admin,
        action="api_key.create",
        target_type="api_key",
        target_id=str(record.id),
        detail={
            "user_id": user_id,
            "key_name": payload.key_name,
            "access_scope": payload.access_scope,
            "mcp_allowed_tools": payload.mcp_allowed_tools,
        },
    )
    return _api_key_item((record, user.username), raw_api_key=raw_key)


@router.patch("/api-keys/{key_id}", response_model=ApiKeyItem)
async def update_admin_api_key(
    key_id: int,
    payload: ApiKeyUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    record = await session.get(ApiKey, key_id)
    if not record:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(record, key, value)
    await _write_audit(
        session,
        request,
        admin,
        action="api_key.update",
        target_type="api_key",
        target_id=str(key_id),
        detail=updates,
    )
    username = None
    if record.user_id:
        username = await session.scalar(
            select(User.username).where(User.user_id == record.user_id)
        )
    return _api_key_item((record, username))


@router.delete("/api-keys/{key_id}")
async def delete_admin_api_key(
    key_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    record = await session.get(ApiKey, key_id)
    if not record:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    detail = {"user_id": record.user_id, "key_name": record.key_name}
    await session.delete(record)
    await _write_audit(
        session,
        request,
        admin,
        action="api_key.delete",
        target_type="api_key",
        target_id=str(key_id),
        detail=detail,
    )
    return {"success": True}


@router.post("/api-keys/{key_id}/rotate", response_model=ApiKeyCreateResponse)
async def rotate_admin_api_key(
    key_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    record = await session.get(ApiKey, key_id)
    if not record:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    raw_key = _generate_api_key()
    record.api_key = raw_key
    record.usage_count = 0
    record.last_used_at = None
    await _write_audit(
        session,
        request,
        admin,
        action="api_key.rotate",
        target_type="api_key",
        target_id=str(key_id),
    )
    username = None
    if record.user_id:
        username = await session.scalar(
            select(User.username).where(User.user_id == record.user_id)
        )
    return _api_key_item((record, username), raw_api_key=raw_key)


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


@router.get("/tasks", response_model=TaskListResponse)
async def list_admin_tasks(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    user_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    stmt = (
        select(Task, Document.filename, User.username)
        .outerjoin(Document, Document.doc_id == Task.doc_id)
        .outerjoin(User, User.user_id == Task.user_id)
    )
    if status:
        stmt = stmt.where(Task.status == status)
    if user_id:
        stmt = stmt.where(Task.user_id == user_id)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(Task.created_at.desc()).offset(offset).limit(limit)
    )
    return TaskListResponse(
        items=[_task_item(row) for row in rows.all()],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/tasks/{task_id}", response_model=TaskAdminItem)
async def get_admin_task(
    task_id: str,
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    result = await session.execute(
        select(Task, Document.filename, User.username)
        .outerjoin(Document, Document.doc_id == Task.doc_id)
        .outerjoin(User, User.user_id == Task.user_id)
        .where(Task.task_id == task_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _task_item(row)


@router.post("/tasks/{task_id}/cancel", response_model=TaskAdminItem)
async def cancel_admin_task(
    task_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    now = datetime.now()
    cancelled = await session.execute(
        update(Task)
        .where(
            Task.task_id == task_id,
            Task.status.in_((TaskStatus.PENDING.value, TaskStatus.PROCESSING.value)),
        )
        .values(
            status=TaskStatus.CANCELLED.value,
            progress=case(
                (Task.progress > 99, 99),
                else_=func.coalesce(Task.progress, 0),
            ),
            error="管理员已取消任务",
            completed_at=now,
            updated_at=now,
        )
    )
    if cancelled.rowcount != 1:
        task = await session.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        raise HTTPException(status_code=400, detail="已结束任务不能取消")

    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    await _write_audit(
        session,
        request,
        admin,
        action="task.cancel",
        target_type="task",
        target_id=task_id,
    )
    filename = await session.scalar(
        select(Document.filename).where(Document.doc_id == task.doc_id)
    )
    username = (
        await session.scalar(select(User.username).where(User.user_id == task.user_id))
        if task.user_id
        else None
    )
    return _task_item((task, filename, username))


@router.post("/tasks/{task_id}/retry", response_model=TaskAdminItem)
async def retry_admin_task(
    task_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
        raise HTTPException(status_code=400, detail="只有失败或已取消任务可重试")

    document = await session.get(Document, task.doc_id)
    if not document or not document.file_path:
        raise HTTPException(status_code=400, detail="任务原始文件不存在，无法重新投递")

    from backend.kafka.schemas import (
        MessageHeader,
        MessageType,
        ProcessingOptions,
        TaskMessage,
        TaskMessageBody,
        TaskPriority,
    )

    options = dict(task.options or {})
    processing_fields = set(ProcessingOptions.model_fields)
    processing_options = ProcessingOptions(
        **{key: value for key, value in options.items() if key in processing_fields}
    )
    priority = TaskPriority.HIGH if int(task.priority or 0) > 0 else TaskPriority.NORMAL
    current_retry_count = int(task.retry_count or 0)
    next_retry_count = current_retry_count + 1
    message = TaskMessage(
        header=MessageHeader(
            message_type=MessageType.TASK_SUBMITTED,
            source="admin-retry",
            user_id=task.user_id or admin.user_id,
            correlation_id=task_id,
        ),
        body=TaskMessageBody(
            task_id=task.task_id,
            document_id=document.doc_id,
            file_path=document.file_path,
            file_name=document.filename,
            file_size=int(document.file_size or 0),
            file_type=str(document.document_type or "unknown"),
            processing_options=processing_options,
            priority=priority,
            retry_count=next_retry_count,
            max_retries=int(task.max_retries if task.max_retries is not None else 3),
        ),
    )
    dispatch_event = build_task_dispatch_event(message, next_retry_count)
    try:
        await ensure_outbox_capacity(
            session,
            kind=TASK_DISPATCH_OUTBOX_KIND,
            excluding_task_id=task_id,
        )
    except OutboxBacklogFullError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    retried = await session.execute(
        update(Task)
        .where(
            Task.task_id == task_id,
            Task.status.in_((TaskStatus.FAILED.value, TaskStatus.CANCELLED.value)),
            func.coalesce(Task.retry_count, 0) == current_retry_count,
        )
        .values(
            status=TaskStatus.PENDING.value,
            progress=0,
            result=task_dispatch_result(dispatch_event),
            outbox_kind=TASK_DISPATCH_OUTBOX_KIND,
            output_path=None,
            error=None,
            error_detail=None,
            retry_count=next_retry_count,
            started_at=None,
            completed_at=None,
            updated_at=datetime.now(),
        )
    )
    if retried.rowcount != 1:
        await session.rollback()
        raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
    await _write_audit(
        session,
        request,
        admin,
        action="task.retry",
        target_type="task",
        target_id=task_id,
        detail={
            "retry_count": next_retry_count,
            "queue": settings.kafka_task_topic,
            "dispatch_event_id": dispatch_event["event_id"],
            "outbox_enqueued": True,
        },
    )
    # The state change, durable outbox payload and audit entry are one atomic
    # unit. Publishing must happen only after that unit is committed.
    await session.commit()
    await session.refresh(task)
    try:
        await task_dispatch_outbox.publish_task(
            task_id,
            str(dispatch_event["event_id"]),
        )
    except Exception as exc:
        # The task state and payload are already durable in one DB commit. The
        # background reconciler will retry even if this immediate attempt or
        # this API process fails now.
        logger.warning(
            "管理员任务重试已写入 outbox，立即投递失败: task=%s error=%s",
            task_id,
            exc,
        )
    filename = await session.scalar(
        select(Document.filename).where(Document.doc_id == task.doc_id)
    )
    username = (
        await session.scalar(select(User.username).where(User.user_id == task.user_id))
        if task.user_id
        else None
    )
    return _task_item((task, filename, username))


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
        components["database"] = SystemComponent(status="error", detail=str(exc))

    try:
        from backend.db.redis import get_redis

        redis_client = get_redis()
        try:
            pong = await redis_client.ping()
        finally:
            await redis_client.aclose()
        components["redis"] = SystemComponent(status="ok" if pong else "error")
    except Exception as exc:
        components["redis"] = SystemComponent(status="error", detail=str(exc))

    try:
        from backend.storage import storage_manager

        stats = storage_manager.get_storage_stats()
        components["local_storage"] = SystemComponent(status="ok", metadata=stats)
    except Exception as exc:
        components["local_storage"] = SystemComponent(status="error", detail=str(exc))

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
