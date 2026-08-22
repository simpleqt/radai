"""认证路由"""

import asyncio
import json
import re
import secrets
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.auth_security import (
    clear_login_failures,
    getdel,
    login_is_blocked,
    record_login_failure,
)
from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.core.utils.client_ip import resolve_client_ip
from backend.db.connection import get_async_session, mysql_session_read_only_enabled
from backend.db.models import User
from backend.db.redis import get_redis_client


from backend.api.routers.auth_cas import (  # noqa: F401  (兼容 re-export)
    CAS_DISPLAY_NAME_KEYS,
    CAS_EXCHANGE_KEY_PREFIX,
    CAS_EXCHANGE_TTL_SECONDS,
    CAS_TARGET_URL_COOKIE,
    _absolute_public_url,
    _build_cas_service_url,
    _configured_cas_service_urls,
    _get_cas_login_url,
    _get_cas_logout_url,
    _get_cas_validate_url,
    _is_safe_redirect_target,
    _is_trusted_existing_cas_user,
    _issue_cas_exchange,
    _merge_query,
    _parse_cas_validate_response,
    _pick_cas_display_name,
    _request_host_candidates,
    _validate_cas_service_url,
    _verify_cas_ticket,
)
from backend.api.routers.auth_credentials import (  # noqa: F401  (兼容 re-export)
    ChangePasswordRequest,
    ChangePasswordResponse,
    EMAIL_RE,
    LOGIN_BLOCK_SECONDS,
    LOGIN_FAILURE_WINDOW_SECONDS,
    LOGIN_IP_MAX_FAILURES,
    LOGIN_MAX_FAILURES,
    TOKEN_EXPIRE_HOURS,
    _ALLOW_QUERY_TOKEN_AUTH,
    _change_current_user_password,
    _extract_bearer_token,
    _issue_auth_token,
    _load_active_user,
    _normalize_display_name,
    _normalize_profile_email,
    _serialize_user,
    _user_auth_provider,
    _user_display_name,
    generate_token,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["认证"])
logger = get_logger(__name__)

# 登录等时防护用的固定哑哈希：用户不存在/无密码分支也跑一次同成本校验，
# 消除"存在用户跑 ~百毫秒 scrypt、不存在用户立即返回"的枚举计时侧信道。
# 哈希对象本身不是秘密（口令是占位符）。
_DUMMY_PASSWORD_HASH = (
    "scrypt:32768:8:1$a9MNu5fysDVN7GU3$"
    "a992d38e8863826544f37edc126fc09ddd18500f5b50a07c1a2f73ac7d2dd85c"
    "d168382bff6d928a14488ee4472342746983bcc98fc34d29655e1f1508069560"
)


class LoginRequest(BaseModel):
    # 口令上界防哈希 CPU 放大（werkzeug 成本随长度线性增长，匿名面可滥用）。
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    success: bool
    token: Optional[str] = None
    user: Optional[dict] = None
    message: Optional[str] = None


class CasExchangeRequest(BaseModel):
    code: str


class CurrentUserResponse(BaseModel):
    user: dict


class UserProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None


class VerifyResponse(BaseModel):
    valid: bool
    user: Optional[dict] = None


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    http_request: Request,
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    """用户登录"""
    username = payload.username.strip()
    client_ip = resolve_client_ip(http_request)
    if await login_is_blocked(
        redis_client, "username", username
    ) or await login_is_blocked(redis_client, "ip", client_ip):
        raise HTTPException(
            status_code=429,
            detail="登录失败次数过多，请稍后再试",
            headers={"Retry-After": str(LOGIN_BLOCK_SECONDS)},
        )

    async def record_failure() -> None:
        for kind, value in (("username", username), ("ip", client_ip)):
            await record_login_failure(
                redis_client,
                kind,
                value,
                window_seconds=LOGIN_FAILURE_WINDOW_SECONDS,
                max_attempts=(
                    LOGIN_IP_MAX_FAILURES if kind == "ip" else LOGIN_MAX_FAILURES
                ),
                block_seconds=LOGIN_BLOCK_SECONDS,
            )

    def _log_username() -> str:
        """日志安全形态：手机号形态的输入打码，非打印字符剔除（防日志注入）。"""
        value = username
        if re.fullmatch(r"1[3-9]\d{9}", value):
            return f"{value[:3]}****{value[-4:]}"
        return "".join(ch for ch in value if ch.isprintable())[:64]

    logger.info("登录尝试: 用户名='%s'", _log_username())
    login_filter = User.username == username
    if re.fullmatch(r"1[3-9]\d{9}", username):
        login_filter = or_(User.username == username, User.telephone == username)
    result = await session.execute(select(User).where(login_filter))
    user = result.scalar_one_or_none()

    if not user:
        await asyncio.to_thread(
            verify_password, payload.password, _DUMMY_PASSWORD_HASH
        )
        await record_failure()
        logger.warning("登录失败: 用户不存在 - 用户名='%s'", _log_username())
        return LoginResponse(success=False, message="用户名或密码错误")

    if not user.password:
        await asyncio.to_thread(
            verify_password, payload.password, _DUMMY_PASSWORD_HASH
        )
        await record_failure()
        logger.warning("登录失败: 用户没有设置密码 - 用户名='%s'", _log_username())
        return LoginResponse(success=False, message="用户名或密码错误")

    if not await asyncio.to_thread(
        verify_password,
        payload.password,
        user.password,
    ):
        await record_failure()
        logger.warning("登录失败: 密码不匹配 - 用户名='%s'", _log_username())
        return LoginResponse(success=False, message="用户名或密码错误")

    if not user.is_active:
        await record_failure()
        logger.warning("登录失败: 账户已被禁用 - 用户名='%s'", _log_username())
        return LoginResponse(success=False, message="账户已被禁用")

    await clear_login_failures(redis_client, "username", username)

    if not mysql_session_read_only_enabled():
        last_login = datetime.now()
        await session.execute(
            update(User)
            .where(User.user_id == user.user_id)
            .values(last_login=last_login)
        )
        await session.commit()
        user.last_login = last_login

    token = await _issue_auth_token(redis_client, user)

    return LoginResponse(success=True, token=token, user=_serialize_user(user))


@router.get("/cas/login")
async def cas_login(request: Request, targetUrl: str | None = None):
    """发起 CAS 登录（新增入口，不影响原账号密码登录）。"""
    if not settings.cas_enabled:
        raise HTTPException(status_code=404, detail="CAS 登录未启用")

    service_url = _build_cas_service_url(request)
    cas_login_url = _merge_query(_get_cas_login_url(), {"service": service_url})
    response = RedirectResponse(url=cas_login_url, status_code=302)

    if targetUrl and _is_safe_redirect_target(targetUrl, request):
        response.set_cookie(
            key=CAS_TARGET_URL_COOKIE,
            value=targetUrl,
            max_age=10 * 60,
            path="/",
            secure=urlparse(service_url).scheme.lower() == "https",
            httponly=True,
            samesite="lax",
        )

    return response


@router.get("/cas/callback", name="cas_callback")
async def cas_callback(
    request: Request,
    ticket: str | None = None,
    targetUrl: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    """CAS 回调：校验 ticket，并创建或更新本地用户登录信息。"""
    if not settings.cas_enabled:
        raise HTTPException(status_code=404, detail="CAS 登录未启用")

    if not ticket:
        raise HTTPException(status_code=400, detail="缺少 CAS ticket")

    service_url = _build_cas_service_url(request)
    cas_username, cas_display_name = await _verify_cas_ticket(ticket, service_url)

    result = await session.execute(select(User).where(User.username == cas_username))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            username=cas_username,
            display_name=(cas_display_name or cas_username).strip() or cas_username,
            password=None,
            email=None,
            api_key=f"cas_{secrets.token_urlsafe(24)}",
            role="user",
            is_active=True,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            last_login=datetime.now(),
        )
        session.add(user)
        await session.flush()
    else:
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账户已被禁用")
        if not _is_trusted_existing_cas_user(user):
            logger.warning(
                "拒绝 CAS 自动合并非 CAS 或特权账户: username=%s user_id=%s",
                cas_username,
                user.user_id,
            )
            raise HTTPException(
                status_code=409,
                detail="该账号已由其他认证方式创建，请联系管理员处理账号绑定",
            )
        if cas_display_name and not getattr(user, "display_name", None):
            user.display_name = cas_display_name.strip() or None
        user.last_login = datetime.now()
        user.updated_at = datetime.now()

    await session.commit()

    display_name = _user_display_name(user)
    token = await _issue_auth_token(redis_client, user, display_name)
    exchange_code = await _issue_cas_exchange(redis_client, token, user)

    redirect_base = settings.cas_frontend_success_url or "/login"
    target_after_login = "/app"
    cookie_target = request.cookies.get(CAS_TARGET_URL_COOKIE)
    if cookie_target and _is_safe_redirect_target(cookie_target, request):
        target_after_login = cookie_target
    elif targetUrl and _is_safe_redirect_target(targetUrl, request):
        target_after_login = targetUrl

    redirect_response = RedirectResponse(
        url=_merge_query(
            redirect_base,
            {
                "code": exchange_code,
                "targetUrl": target_after_login,
            },
        ),
        status_code=302,
    )
    redirect_response.delete_cookie(CAS_TARGET_URL_COOKIE, path="/")
    return redirect_response


@router.post("/cas/exchange", response_model=LoginResponse)
async def cas_exchange(
    payload: CasExchangeRequest,
    redis_client: Redis = Depends(get_redis_client),
):
    """Atomically exchange a short-lived, one-time CAS code for a bearer token."""
    code = payload.code.strip()
    if not code or len(code) > 256:
        raise HTTPException(status_code=400, detail="CAS 交换码无效")

    raw = await getdel(redis_client, f"{CAS_EXCHANGE_KEY_PREFIX}{code}")
    if not raw:
        raise HTTPException(status_code=401, detail="CAS 交换码无效或已过期")
    try:
        item = json.loads(raw)
        token = str(item["token"])
        user = item["user"]
        if not token or not isinstance(user, dict):
            raise ValueError("invalid exchange payload")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="CAS 交换码数据无效") from exc
    return LoginResponse(success=True, token=token, user=user)


@router.get("/cas/logout")
async def cas_logout_redirect(request: Request, redirect: str | None = None):
    """浏览器跳转用 CAS 注销入口。"""
    if not settings.cas_enabled:
        raise HTTPException(status_code=404, detail="CAS 登录未启用")

    fallback = settings.cas_frontend_logout_redirect or "/login"
    target = (
        redirect
        if redirect and _is_safe_redirect_target(redirect, request)
        else fallback
    )
    if target.startswith("/"):
        target = _absolute_public_url(target, request)
    logout_url = _merge_query(_get_cas_logout_url(), {"service": target})
    return RedirectResponse(url=logout_url, status_code=302)


@router.post("/cas/logout")
async def cas_logout(
    request: Request,
    redis_client: Redis = Depends(get_redis_client),
    redirect: str | None = None,
):
    """API 注销：清理本地 token，并返回 CAS 注销地址给前端跳转。"""
    if not settings.cas_enabled:
        raise HTTPException(status_code=404, detail="CAS 登录未启用")

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        await redis_client.delete(f"auth:token:{token}")

    fallback = settings.cas_frontend_logout_redirect or "/login"
    target = (
        redirect
        if redirect and _is_safe_redirect_target(redirect, request)
        else fallback
    )
    if target.startswith("/"):
        target = _absolute_public_url(target, request)
    logout_url = _merge_query(_get_cas_logout_url(), {"service": target})

    return {"success": True, "logout_url": logout_url}


@router.post("/logout")
async def logout(request: Request, redis_client: Redis = Depends(get_redis_client)):
    """用户登出"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        await redis_client.delete(f"auth:token:{token}")

    return {"success": True, "message": "已成功登出"}


@router.get("/verify", response_model=VerifyResponse)
async def verify_token(user: User = Depends(get_current_user)):
    """验证 Token"""
    return VerifyResponse(valid=True, user=_serialize_user(user))


@router.get("/me", response_model=CurrentUserResponse)
async def get_me(user: User = Depends(get_current_user)):
    """获取当前用户信息"""
    return CurrentUserResponse(user=_serialize_user(user))


@router.patch("/me", response_model=CurrentUserResponse)
async def update_me(
    payload: UserProfileUpdateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """更新当前用户基础资料"""
    updates = payload.model_dump(exclude_unset=True)

    if "display_name" in updates:
        user.display_name = _normalize_display_name(updates["display_name"])

    if "email" in updates:
        next_email = _normalize_profile_email(updates["email"])
        if next_email:
            existing = await session.execute(
                select(User).where(
                    User.email == next_email, User.user_id != user.user_id
                )
            )
            if existing.scalar_one_or_none() is not None:
                raise HTTPException(status_code=409, detail="邮箱已存在")
        user.email = next_email

    user.updated_at = datetime.now()
    await session.commit()
    await session.refresh(user)
    return CurrentUserResponse(user=_serialize_user(user))


@router.post("/me/change-password", response_model=ChangePasswordResponse)
async def change_my_password(
    request: ChangePasswordRequest,
    http_request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    """修改当前用户密码，成功后当前 Token 失效"""
    return await _change_current_user_password(
        request,
        http_request,
        user=user,
        session=session,
        redis_client=redis_client,
    )


@router.post("/change-password", response_model=ChangePasswordResponse)
async def change_password(
    request: ChangePasswordRequest,
    http_request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    """兼容旧路径的修改密码接口"""
    return await _change_current_user_password(
        request,
        http_request,
        user=user,
        session=session,
        redis_client=redis_client,
    )
