"""依赖注入模块"""

import logging
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.auth_service import (
    API_KEY_SCOPE_FULL,
    ResolvedAuth,
    api_key_scope_allows_path,
    auth_service,
)
from backend.application.policies.knowledge_base_access import (
    KnowledgeBaseRecord,
    can_read_knowledge_base,
    is_guest_principal,
)
from backend.core.utils.normalization import normalize_unique_strings

logger = logging.getLogger(__name__)


class ApiKeyAuth(BaseModel):
    """API Key 认证"""

    api_key: str
    user_id: str
    role: str = "user"
    access_scope: str = API_KEY_SCOPE_FULL
    mcp_allowed_tools: tuple[str, ...] | None = None


async def _resolve_api_key_auth(api_key: str) -> ApiKeyAuth | None:
    """解析并校验 API Key（含激活状态/过期时间）。"""
    return _to_api_key_auth(await auth_service.resolve_api_key(api_key))


async def _resolve_bearer_token_auth(token: str) -> ApiKeyAuth | None:
    """解析 Bearer Token（登录态 token），用于统一用户鉴权。"""
    return _to_api_key_auth(await auth_service.resolve_bearer_token(token))


def _to_api_key_auth(auth: ResolvedAuth | None) -> ApiKeyAuth | None:
    if auth is None:
        return None
    return ApiKeyAuth(
        api_key=auth.api_key,
        user_id=auth.user_id,
        role=auth.role,
        access_scope=auth.access_scope,
        mcp_allowed_tools=auth.mcp_allowed_tools,
    )


def _enforce_request_scope(request: Request | None, auth: ApiKeyAuth) -> ApiKeyAuth:
    if (
        request is not None
        and auth.api_key
        and not api_key_scope_allows_path(auth.access_scope, request.url.path)
    ):
        raise HTTPException(
            status_code=403,
            detail="此 API Key 仅允许访问 MCP 服务",
        )
    return auth


def _cached_request_auth(
    request: Request | None,
    *,
    api_key: str = "",
    bearer: str = "",
) -> ApiKeyAuth | None:
    if request is None:
        return None
    auth = getattr(request.state, "auth", None)
    if not isinstance(auth, ResolvedAuth) or not auth.user_id:
        return None
    if api_key and auth.api_key != api_key:
        return None
    if bearer and auth.api_key and auth.api_key != bearer:
        return None
    return _to_api_key_auth(auth)


async def verify_api_key(
    request: Request = None,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None, alias="Authorization"),
) -> ApiKeyAuth:
    """统一认证：优先 X-API-Key；其次 Bearer Token；最后兼容 Bearer API Key。"""
    api_key = (x_api_key or "").strip()
    bearer = ""
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization[7:].strip()

    cached_auth = _cached_request_auth(request, api_key=api_key, bearer=bearer)
    if cached_auth:
        return _enforce_request_scope(request, cached_auth)

    if api_key:
        auth = await _resolve_api_key_auth(api_key)
        if auth:
            return _enforce_request_scope(request, auth)
        raise HTTPException(status_code=401, detail="无效、已禁用或已过期的 API Key")

    if bearer:
        token_auth = await _resolve_bearer_token_auth(bearer)
        if token_auth:
            return token_auth

        # 兼容历史客户端：Authorization: Bearer <api_key>
        key_auth = await _resolve_api_key_auth(bearer)
        if key_auth:
            return _enforce_request_scope(request, key_auth)

        raise HTTPException(status_code=401, detail="无效或已过期的认证令牌")

    raise HTTPException(status_code=401, detail="缺少认证凭据")


async def optional_verify_api_key(
    request: Request = None,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None, alias="Authorization"),
) -> ApiKeyAuth:
    """可选认证：有凭据则校验，无凭据则返回 guest 默认（不抛 401）。"""
    api_key = (x_api_key or "").strip()
    bearer = ""
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization[7:].strip()

    cached_auth = _cached_request_auth(request, api_key=api_key, bearer=bearer)
    if cached_auth:
        return _enforce_request_scope(request, cached_auth)

    if api_key:
        auth = await _resolve_api_key_auth(api_key)
        if auth:
            return _enforce_request_scope(request, auth)
        raise HTTPException(status_code=401, detail="无效、已禁用或已过期的 API Key")

    if bearer:
        token_auth = await _resolve_bearer_token_auth(bearer)
        if token_auth:
            return token_auth
        key_auth = await _resolve_api_key_auth(bearer)
        if key_auth:
            return _enforce_request_scope(request, key_auth)
        raise HTTPException(status_code=401, detail="无效或已过期的认证令牌")

    return ApiKeyAuth(api_key="", user_id="guest", role="user")


async def require_admin(auth: ApiKeyAuth = Depends(verify_api_key)) -> ApiKeyAuth:
    """要求管理员权限"""
    if (auth.role or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return auth


async def require_teacher(auth: ApiKeyAuth = Depends(verify_api_key)) -> ApiKeyAuth:
    """要求教师（或管理员）权限，用于基础力学课程智能体的教学分析功能。"""
    if (auth.role or "").lower() not in {"teacher", "admin"}:
        raise HTTPException(status_code=403, detail="需要教师权限")
    return auth


def _status_value(value) -> str:
    return str(getattr(value, "value", value) or "").lower()


def _normalize_ids(values: list[str]) -> list[str]:
    return normalize_unique_strings(values)


async def _explicit_kb_grants(
    session: AsyncSession,
    kb_ids: list[str],
    auth: ApiKeyAuth,
) -> set[str]:
    """Return explicit ACL grants; missing legacy ACL tables fail closed."""
    if not kb_ids or is_guest_principal(auth.user_id):
        return set()

    from backend.db.models.knowledge_base import KnowledgeBaseAcl

    subject_filters = [
        (
            (KnowledgeBaseAcl.subject_type == "user")
            & (KnowledgeBaseAcl.subject_id == str(auth.user_id))
        )
    ]
    role = str(auth.role or "user").strip().lower()
    if role:
        subject_filters.append(
            (KnowledgeBaseAcl.subject_type == "role")
            & (KnowledgeBaseAcl.subject_id == role)
        )

    stmt = select(KnowledgeBaseAcl.kb_id, KnowledgeBaseAcl.effect).where(
        KnowledgeBaseAcl.kb_id.in_(kb_ids),
        or_(*subject_filters),
        KnowledgeBaseAcl.permission.in_(("read", "use")),
        or_(
            KnowledgeBaseAcl.expires_at.is_(None),
            KnowledgeBaseAcl.expires_at > datetime.now(),
        ),
    )
    try:
        # A savepoint prevents a missing ACL table on an older deployment from
        # poisoning the request transaction.
        async with session.begin_nested():
            rows = (await session.execute(stmt)).all()
    except (SQLAlchemyError, AttributeError) as exc:
        logger.warning("知识库 ACL 查询失败，按无授权处理: %s", exc)
        return set()

    allowed = {
        str(row.kb_id)
        for row in rows
        if str(getattr(row, "effect", "allow") or "allow").lower() == "allow"
    }
    denied = {
        str(row.kb_id)
        for row in rows
        if str(getattr(row, "effect", "") or "").lower() == "deny"
    }
    return allowed - denied


async def filter_accessible_kb_ids(
    session: AsyncSession,
    kb_ids: list[str],
    auth: ApiKeyAuth,
) -> list[str]:
    """Filter KB IDs using owner/public/default/explicit-ACL semantics."""
    normalized_ids = _normalize_ids(kb_ids)
    if not normalized_ids:
        return []

    from backend.db.models.knowledge_base import KnowledgeBase

    result = await session.execute(
        select(
            KnowledgeBase.kb_id,
            KnowledgeBase.owner_id,
            KnowledgeBase.is_default,
            KnowledgeBase.status,
        ).where(KnowledgeBase.kb_id.in_(normalized_ids))
    )
    records = {
        str(row.kb_id): KnowledgeBaseRecord(
            kb_id=str(row.kb_id),
            owner_id=str(row.owner_id) if row.owner_id else None,
            is_default=bool(row.is_default),
            status=_status_value(row.status),
        )
        for row in result.all()
    }
    private_candidates = [
        kb_id
        for kb_id, record in records.items()
        if record.status == "active"
        and record.owner_id
        and not record.is_default
        and record.owner_id != str(auth.user_id or "")
    ]
    is_admin = str(auth.role or "user").lower() == "admin"
    granted_ids = (
        set()
        if is_admin
        else await _explicit_kb_grants(session, private_candidates, auth)
    )
    return [
        kb_id
        for kb_id in normalized_ids
        if can_read_knowledge_base(
            records.get(kb_id),
            user_id=auth.user_id,
            explicitly_granted=kb_id in granted_ids,
            is_admin=is_admin,
        )
    ]


async def require_kb_access(
    session: AsyncSession,
    kb_id: str | None,
    auth: ApiKeyAuth,
    *,
    access: str = "read",
):
    """Validate route-level knowledge-base access and return the KB row.

    Read access is limited to admins, owners, public/default KBs, and explicit
    ACL grants.  Agent bindings never grant access.  Write access is narrower:
    only admins and owners may mutate a KB.
    """
    normalized_kb_id = str(kb_id or "").strip()
    if not normalized_kb_id:
        raise HTTPException(status_code=400, detail="缺少知识库 ID")

    from backend.db.models.knowledge_base import KnowledgeBase

    kb = await session.get(KnowledgeBase, normalized_kb_id)
    if not kb or _status_value(getattr(kb, "status", "")) != "active":
        raise HTTPException(status_code=404, detail="知识库不存在或不可用")

    role = str(auth.role or "user").lower()
    user_id = str(auth.user_id or "").strip()
    owner_id = str(getattr(kb, "owner_id", "") or "").strip()
    is_admin = role == "admin"
    is_owner = bool(owner_id and user_id and owner_id == user_id)

    if is_admin or is_owner:
        return kb

    requested_access = str(access or "read").lower()
    if requested_access != "read":
        raise HTTPException(status_code=403, detail="无权修改该知识库")

    if normalized_kb_id in await filter_accessible_kb_ids(
        session,
        [normalized_kb_id],
        auth,
    ):
        return kb

    raise HTTPException(status_code=403, detail="无权访问该知识库")


# 注意：不要重新引入"信任 X-User-ID 头"的任务上下文依赖——那是把
# 客户端可伪造的头当作身份原语；身份一律经 request.state.auth 解析。


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


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """获取数据库会话的依赖注入"""
    from backend.db.connection import get_async_session

    async for session in get_async_session(request):
        yield session
