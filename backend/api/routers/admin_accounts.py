"""管理员后台:用户与 API Key 管理路由(由 admin.py 主 router include)。"""

from __future__ import annotations

from fastapi import APIRouter
from backend.config.loggers import get_logger

import asyncio
import secrets

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, require_admin
from backend.application.api_key_credentials import (
    api_key_digest,
    api_key_prefix,
    hashed_storage_available,
    mask_api_key,
)
from backend.application.auth_security import revoke_user_sessions
from backend.application.auth_service import API_KEY_SCOPE_FULL
from backend.db.models import ApiKey, User
from backend.db.redis import get_redis_client
from datetime import datetime
from fastapi import Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any
from backend.api.schemas.admin import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyItem,
    ApiKeyListResponse,
    ApiKeyUpdateRequest,
    ResetPasswordRequest,
    ResetPasswordResponse,
    UserAdminItem,
    UserCreateRequest,
    UserCreateResponse,
    UserListResponse,
    UserUpdateRequest,
)
from backend.api.routers.admin_operations import (
    _write_audit,
)


logger = get_logger(__name__)
router = APIRouter()
# student/teacher 仅被基础力学课程智能体（fmca）的教师分析功能消费；
# 其他业务仍只区分 admin/user。注册默认角色保持不变。
_ROLES = {"admin", "user", "student", "teacher"}


def _mask_secret(value: str | None) -> str:
    raw = str(value or "")
    if len(raw) <= 10:
        return "***" if raw else ""
    return f"{raw[:6]}...{raw[-4:]}"


def _generate_api_key() -> str:
    return f"ak_{secrets.token_urlsafe(32)}"


async def _api_key_storage_fields(session: AsyncSession, raw_key: str) -> dict[str, Any]:
    """Return the columns that persist a new key for this database.

    Once the hash column exists the verbatim key is never written, so the raw
    value only ever exists in the response handed to the administrator.
    """

    if await hashed_storage_available(session):
        return {
            "api_key": None,
            "api_key_hash": api_key_digest(raw_key),
            "api_key_prefix": api_key_prefix(raw_key),
        }
    return {"api_key": raw_key}


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
        # Hashed rows have no verbatim key to mask; the stored prefix is the
        # only recognisable, non-credential label.
        "api_key": (
            _mask_secret(api_key.api_key)
            if api_key.api_key
            else mask_api_key(prefix=api_key.api_key_prefix)
        ),
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
        # ``users.api_key`` is an account marker, not a credential: the usable
        # key lives in ``api_keys``, so keep a distinct random value here rather
        # than a second copy of the secret.
        api_key=f"managed_{secrets.token_urlsafe(24)}",
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
                **await _api_key_storage_fields(session, raw_key),
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
                    .where(User.role == "admin", User.is_active == True)  # noqa: E712
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
                .where(User.role == "admin", User.is_active == True)  # noqa: E712
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
        **await _api_key_storage_fields(session, raw_key),
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
    for column, value in (await _api_key_storage_fields(session, raw_key)).items():
        setattr(record, column, value)
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
