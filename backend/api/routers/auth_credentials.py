"""本地凭证域:密码哈希/校验、token 生成与签发、登录限流常量、当前用户解析与序列化。(拆自 api/routers/auth.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger
from typing import Optional

from pydantic import BaseModel, Field

from backend.application.auth_security import clear_login_failures
from backend.application.auth_security import get_session_epoch
from backend.application.auth_security import login_is_blocked
from backend.application.auth_security import record_login_failure
from backend.application.auth_security import revoke_user_sessions
from backend.application.auth_security import session_epoch_key
from backend.application.auth_security import token_session_is_current
from backend.application.auth_service import ResolvedAuth
from backend.db.connection import get_async_session
from backend.db.models import User
from backend.db.redis import get_redis_client
from datetime import datetime
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
import asyncio
import json
import os
import re
import secrets

logger = get_logger(__name__)


TOKEN_EXPIRE_HOURS = 24


LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60


LOGIN_MAX_FAILURES = 5


LOGIN_IP_MAX_FAILURES = 30


LOGIN_BLOCK_SECONDS = 15 * 60


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


_ALLOW_QUERY_TOKEN_AUTH = os.getenv("ALLOW_QUERY_TOKEN_AUTH", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    try:
        from werkzeug.security import check_password_hash

        return check_password_hash(hashed_password, plain_password)
    except Exception:
        logger.warning("密码哈希校验异常", exc_info=True)
        return False


def generate_token() -> str:
    """生成 Token"""
    return secrets.token_urlsafe(32)


async def _issue_auth_token(
    redis_client: Redis,
    user: User,
    display_name: str | None = None,
) -> str:
    token = generate_token()
    session_epoch = await get_session_epoch(redis_client, user.user_id)
    if session_epoch < 0:
        # Recover a corrupted epoch without allowing legacy epoch-0 sessions
        # to become valid again.
        await redis_client.delete(session_epoch_key(user.user_id))
        session_epoch = await revoke_user_sessions(redis_client, user.user_id)
    resolved_display_name = (
        display_name or getattr(user, "display_name", None) or user.username
    )
    token_data = {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": resolved_display_name,
        "role": user.role if hasattr(user, "role") else "user",
        "session_epoch": session_epoch,
    }
    await redis_client.setex(
        f"auth:token:{token}",
        TOKEN_EXPIRE_HOURS * 3600,
        json.dumps(token_data),
    )
    return token


def _user_display_name(user: User) -> str:
    return str(getattr(user, "display_name", None) or user.username or "").strip()


def _user_auth_provider(user: User) -> str:
    return "password" if bool(user.password) else "cas"


def _serialize_user(user: User) -> dict:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": _user_display_name(user),
        "email": user.email,
        "role": user.role or "user",
        "is_active": user.is_active,
        "auth_provider": _user_auth_provider(user),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
    }


def _normalize_display_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 50:
        raise HTTPException(status_code=400, detail="显示名不能超过50个字符")
    return normalized


def _normalize_profile_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 100 or not EMAIL_RE.match(normalized):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    return normalized


def _extract_bearer_token(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    if _ALLOW_QUERY_TOKEN_AUTH:
        return request.query_params.get("token")
    return None


async def _load_active_user(session: AsyncSession, user_id: str) -> User:
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账户已被禁用")
    return user


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
) -> User:
    """获取当前用户"""
    token = _extract_bearer_token(request)
    cached_auth = getattr(request.state, "auth", None)
    if (
        isinstance(cached_auth, ResolvedAuth)
        and cached_auth.source == "session_token"
        and cached_auth.user_id
        and token
    ):
        return await _load_active_user(session, cached_auth.user_id)

    if not token:
        raise HTTPException(status_code=401, detail="未提供认证令牌")

    token_data_str = await redis_client.get(f"auth:token:{token}")
    if not token_data_str:
        raise HTTPException(status_code=401, detail="无效的认证令牌或已过期")

    try:
        token_info = json.loads(token_data_str)
        user_id = str(token_info["user_id"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="认证令牌数据无效") from exc

    if not await token_session_is_current(redis_client, token_info):
        raise HTTPException(status_code=401, detail="认证会话已失效，请重新登录")

    return await _load_active_user(session, user_id)


def hash_password(password: str) -> str:
    """生成密码哈希"""
    from werkzeug.security import generate_password_hash

    return generate_password_hash(password)


class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = Field(default=None, max_length=256)
    old_password: Optional[str] = Field(default=None, max_length=256)
    new_password: str = Field(min_length=6, max_length=256)


class ChangePasswordResponse(BaseModel):
    success: bool
    message: Optional[str] = None


async def _change_current_user_password(
    request: ChangePasswordRequest,
    http_request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    current_password = request.current_password or request.old_password or ""
    if not user.password:
        raise HTTPException(status_code=400, detail="当前账号不支持本地密码修改")

    # 持泄露 token 的攻击者可在此接口在线爆破 current_password；成功后
    # 得到比 token 更持久的凭证。与登录同款 Redis 失败锁定（按键位
    # 隔离，不影响登录节流）。
    if await login_is_blocked(redis_client, "pwchange", user.username):
        raise HTTPException(
            status_code=429, detail="尝试次数过多，请稍后再试"
        )

    if not await asyncio.to_thread(
        verify_password,
        current_password,
        user.password,
    ):
        await record_login_failure(
            redis_client,
            "pwchange",
            user.username,
            window_seconds=900,
            max_attempts=5,
            block_seconds=900,
        )
        raise HTTPException(status_code=400, detail="当前密码错误")

    await clear_login_failures(redis_client, "pwchange", user.username)

    if len(request.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度不能少于6位")

    hashed_password = await asyncio.to_thread(hash_password, request.new_password)

    await session.execute(
        update(User)
        .where(User.user_id == user.user_id)
        .values(password=hashed_password, updated_at=datetime.now())
    )

    await revoke_user_sessions(redis_client, user.user_id)
    token = _extract_bearer_token(http_request)
    if token:
        await redis_client.delete(f"auth:token:{token}")
    await session.commit()

    return ChangePasswordResponse(success=True, message="密码修改成功")
