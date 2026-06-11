"""依赖注入模块"""

from collections.abc import AsyncIterator
from datetime import datetime
import json

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class ApiKeyAuth(BaseModel):
    """API Key 认证"""

    api_key: str
    user_id: str
    role: str = "user"


async def _resolve_api_key_auth(api_key: str) -> ApiKeyAuth | None:
    """解析并校验 API Key（含激活状态/过期时间）。"""
    from backend.db.connection import get_async_session
    from backend.db.models import ApiKey, User

    async for session in get_async_session():
        result = await session.execute(
            select(ApiKey, User.role, User.is_active)
            .outerjoin(User, User.user_id == ApiKey.user_id)
            .where(ApiKey.api_key == api_key)
        )
        row = result.first()
        if not row:
            return None

        key_obj: ApiKey = row[0]
        user_role = row[1]
        user_is_active = row[2]

        if not key_obj.is_active:
            return None

        if key_obj.expires_at and key_obj.expires_at <= datetime.now():
            return None

        if key_obj.user_id and user_is_active is False:
            return None

        return ApiKeyAuth(
            api_key=api_key,
            user_id=key_obj.user_id or "",
            role=user_role or "user",
        )

    return None


async def _resolve_bearer_token_auth(token: str) -> ApiKeyAuth | None:
    """解析 Bearer Token（登录态 token），用于统一用户鉴权。"""
    from backend.db.connection import get_async_session
    from backend.db.models import User
    from backend.db.redis import get_redis

    redis_client = get_redis()
    try:
        token_data_str = await redis_client.get(f"auth:token:{token}")
    finally:
        await redis_client.aclose()

    if not token_data_str:
        return None

    try:
        token_info = json.loads(token_data_str)
    except Exception:
        return None

    user_id = str(token_info.get("user_id") or "").strip()
    if not user_id:
        return None

    async for session in get_async_session():
        result = await session.execute(
            select(User.user_id, User.role, User.is_active).where(User.user_id == user_id)
        )
        row = result.first()
        if not row:
            return None

        if row[2] is False:
            return None

        return ApiKeyAuth(
            api_key="",
            user_id=str(row[0]),
            role=str(row[1] or "user"),
        )

    return None


async def verify_api_key(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None, alias="Authorization"),
) -> ApiKeyAuth:
    """统一认证：优先 X-API-Key；其次 Bearer Token；最后兼容 Bearer API Key。"""
    api_key = (x_api_key or "").strip()
    bearer = ""
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization[7:].strip()

    if api_key:
        auth = await _resolve_api_key_auth(api_key)
        if auth:
            return auth
        raise HTTPException(status_code=401, detail="无效、已禁用或已过期的 API Key")

    if bearer:
        token_auth = await _resolve_bearer_token_auth(bearer)
        if token_auth:
            return token_auth

        # 兼容历史客户端：Authorization: Bearer <api_key>
        key_auth = await _resolve_api_key_auth(bearer)
        if key_auth:
            return key_auth

        raise HTTPException(status_code=401, detail="无效或已过期的认证令牌")

    raise HTTPException(status_code=401, detail="缺少认证凭据")


async def require_admin(auth: ApiKeyAuth = Depends(verify_api_key)) -> ApiKeyAuth:
    """要求管理员权限"""
    if (auth.role or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return auth


class TaskContext(BaseModel):
    """任务上下文"""

    task_id: str
    user_id: str | None = None
    priority: int = 0


async def get_task_context(
    x_task_id: str | None = Header(None, alias="X-Task-ID"),
    x_priority: int = Header(0, alias="X-Priority"),
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> TaskContext:
    """获取任务上下文"""
    if not x_task_id:
        raise HTTPException(status_code=400, detail="缺少 Task ID")

    return TaskContext(
        task_id=x_task_id,
        user_id=x_user_id,
        priority=x_priority,
    )


class PaginationParams(BaseModel):
    """分页参数"""

    page: int = 1
    page_size: int = 20

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


async def get_pagination(
    page: int = 1,
    page_size: int = 20,
) -> PaginationParams:
    """获取分页参数"""
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20

    return PaginationParams(page=page, page_size=page_size)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """获取数据库会话的依赖注入"""
    from backend.db.connection import get_async_session

    async for session in get_async_session():
        yield session
