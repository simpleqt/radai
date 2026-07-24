"""认证路由"""

import asyncio
import json
import os
import re
import secrets
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.auth_security import (
    clear_login_failures,
    get_session_epoch,
    getdel,
    login_is_blocked,
    record_login_failure,
    revoke_user_sessions,
    session_epoch_key,
    token_session_is_current,
)
from backend.application.auth_service import ResolvedAuth
from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.connection import get_async_session
from backend.db.models import User
from backend.db.redis import get_redis_client

router = APIRouter(prefix="/auth", tags=["认证"])
logger = get_logger(__name__)

TOKEN_EXPIRE_HOURS = 24
CAS_TARGET_URL_COOKIE = "cas_target_url"
CAS_EXCHANGE_TTL_SECONDS = 60
CAS_EXCHANGE_KEY_PREFIX = "auth:cas_exchange:"
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
CAS_DISPLAY_NAME_KEYS = (
    "name",
    "displayname",
    "realname",
    "cn",
    "truename",
    "nickname",
    "xm",
)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    success: bool
    token: Optional[str] = None
    user: Optional[dict] = None
    message: Optional[str] = None


class CasExchangeRequest(BaseModel):
    code: str


class UserResponse(BaseModel):
    user_id: str
    username: str
    display_name: Optional[str] = None
    email: Optional[str]
    role: str = "user"
    is_active: bool
    auth_provider: str = "password"
    created_at: Optional[datetime]
    updated_at: Optional[datetime] = None
    last_login: Optional[datetime]


class CurrentUserResponse(BaseModel):
    user: dict


class UserProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None


class VerifyResponse(BaseModel):
    valid: bool
    user: Optional[dict] = None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    try:
        from werkzeug.security import check_password_hash

        return check_password_hash(hashed_password, plain_password)
    except Exception:
        return False


def generate_token() -> str:
    """生成 Token"""
    return secrets.token_urlsafe(32)


def _merge_query(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None:
            continue
        query[key] = value
    new_query = urlencode(query)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        )
    )


def _validate_cas_service_url(configured: str) -> str:
    parsed = urlparse(configured)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise HTTPException(status_code=500, detail="CAS 回调地址配置无效")
    return configured


def _configured_cas_service_urls() -> list[str]:
    """Parse CAS_SERVICE_URL as one URL or a comma-separated allowlist."""
    raw = (settings.cas_service_url or "").strip()
    if not raw:
        return []
    urls: list[str] = []
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        urls.append(_validate_cas_service_url(candidate))
    return urls


def _request_host_candidates(request: Request) -> list[str]:
    """Collect Host / X-Forwarded-Host values for allowlist matching only."""
    candidates: list[str] = []
    for header_name in ("host", "x-forwarded-host"):
        raw = (request.headers.get(header_name) or "").strip().lower()
        if not raw:
            continue
        # X-Forwarded-Host may be a chain; only consider the first hop.
        host = raw.split(",", 1)[0].strip()
        if host and host not in candidates:
            candidates.append(host)
    return candidates


def _is_safe_redirect_target(target: str, request: Request) -> bool:
    """只允许站内相对路径或配置中明确声明的公开源。"""
    del request  # Never derive trust from a user-controlled Host header.
    if not target:
        return False
    if target.startswith("/") and not target.startswith("//"):
        return True

    parsed = urlparse(target)
    if not parsed.scheme or not parsed.netloc:
        return False

    target_origin = (parsed.scheme.lower(), parsed.netloc.lower())
    trusted_origins: set[tuple[str, str]] = set()
    for configured_url in (
        *_configured_cas_service_urls(),
        settings.cas_frontend_success_url,
        settings.cas_frontend_logout_redirect,
    ):
        configured_parsed = urlparse(str(configured_url or "").strip())
        if configured_parsed.scheme in {"http", "https"} and configured_parsed.netloc:
            trusted_origins.add(
                (configured_parsed.scheme.lower(), configured_parsed.netloc.lower())
            )
    return target_origin in trusted_origins


def _build_cas_service_url(request: Request) -> str:
    """Return an allowlisted CAS callback URL for this request.

    ``CAS_SERVICE_URL`` may list multiple public callbacks (comma-separated) for
    shared backends serving several frontends. The request Host /
    X-Forwarded-Host is only used to *select* among that allowlist; unknown
    hosts fall back to the first configured URL and never invent a new callback.
    """
    configured_urls = _configured_cas_service_urls()
    if not configured_urls:
        raise HTTPException(status_code=500, detail="CAS 回调地址未配置")

    host_candidates = _request_host_candidates(request)
    if host_candidates:
        for service_url in configured_urls:
            netloc = urlparse(service_url).netloc.lower()
            if netloc in host_candidates:
                return service_url

    return configured_urls[0]


def _absolute_public_url(target: str, request: Request) -> str:
    """Resolve a relative configured target against the trusted CAS origin."""
    if not target.startswith("/") or target.startswith("//"):
        return target
    service = urlparse(_build_cas_service_url(request))
    origin = urlunparse((service.scheme, service.netloc, "/", "", "", ""))
    return urljoin(origin, target.lstrip("/"))


def _get_cas_login_url() -> str:
    explicit = (settings.cas_login_url or "").strip()
    if explicit:
        return explicit

    base = (settings.cas_base_url or "").strip().rstrip("/")
    if base:
        return f"{base}/login"
    raise HTTPException(status_code=500, detail="CAS 登录地址未配置")


def _get_cas_logout_url() -> str:
    explicit = (settings.cas_logout_url or "").strip()
    if explicit:
        return explicit

    base = (settings.cas_base_url or "").strip().rstrip("/")
    if base:
        return f"{base}/logout"
    raise HTTPException(status_code=500, detail="CAS 注销地址未配置")


def _get_cas_validate_url() -> str:
    explicit = (settings.cas_validate_url or "").strip()
    if explicit:
        return explicit

    base = (settings.cas_base_url or "").strip().rstrip("/")
    if base:
        return f"{base}/serviceValidate"
    raise HTTPException(status_code=500, detail="CAS 票据校验地址未配置")


def _pick_cas_display_name(attributes: dict[str, str]) -> str | None:
    if not attributes:
        return None

    normalized = {
        str(key or "").strip().lower(): str(value or "").strip()
        for key, value in attributes.items()
        if str(value or "").strip()
    }
    for key in CAS_DISPLAY_NAME_KEYS:
        value = normalized.get(key)
        if value:
            return value
    return None


def _parse_cas_validate_response(
    xml_text: str,
) -> tuple[bool, str | None, str | None, dict[str, str]]:
    """返回: (是否成功, 统一账号, 失败原因, 属性字典)。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False, None, "CAS 返回了无效 XML", {}

    username: str | None = None
    attributes: dict[str, str] = {}

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "user":
            value = (elem.text or "").strip()
            if value and not username:
                username = value
            continue

        if tag in {"serviceResponse", "authenticationSuccess", "attributes"}:
            continue

        value = (elem.text or "").strip()
        if value:
            attributes[tag] = value

        if tag == "authenticationFailure":
            return False, None, (elem.text or "CAS 认证失败").strip(), {}

    if username:
        return True, username, None, attributes

    return False, None, "CAS 未返回用户信息", {}


async def _verify_cas_ticket(ticket: str, service_url: str) -> tuple[str, str | None]:
    validate_url = _get_cas_validate_url()
    timeout = httpx.Timeout(10.0, connect=5.0)

    async with httpx.AsyncClient(
        trust_env=False, timeout=timeout, verify=settings.cas_verify_ssl
    ) as client:
        response = await client.get(
            validate_url,
            params={
                "service": service_url,
                "ticket": ticket,
            },
        )

    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="CAS 票据校验失败")

    ok, username, reason, attributes = _parse_cas_validate_response(response.text)
    if not ok or not username:
        raise HTTPException(status_code=401, detail=reason or "CAS 认证失败")

    return username, _pick_cas_display_name(attributes)


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


async def _issue_cas_exchange(
    redis_client: Redis,
    token: str,
    user: User,
) -> str:
    code = secrets.token_urlsafe(32)
    payload = {
        "token": token,
        "user": _serialize_user(user),
    }
    await redis_client.setex(
        f"{CAS_EXCHANGE_KEY_PREFIX}{code}",
        CAS_EXCHANGE_TTL_SECONDS,
        json.dumps(payload),
    )
    return code


def _user_display_name(user: User) -> str:
    return str(getattr(user, "display_name", None) or user.username or "").strip()


def _user_auth_provider(user: User) -> str:
    return "password" if bool(user.password) else "cas"


def _is_trusted_existing_cas_user(user: User) -> bool:
    """Recognize only accounts that this CAS flow previously provisioned.

    The legacy users table has no provider/subject columns.  The generated
    ``cas_`` API-key prefix is therefore the only persisted provider marker
    available without a schema migration.  Fail closed for password accounts,
    privileged accounts, and accounts created by another authentication flow.
    """
    return (
        not bool(user.password)
        and str(getattr(user, "role", "user") or "user").lower() == "user"
        and str(getattr(user, "api_key", "") or "").startswith("cas_")
    )


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


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    http_request: Request,
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    """用户登录"""
    username = payload.username.strip()
    client_ip = http_request.client.host if http_request.client else "unknown"
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

    logger.info("登录尝试: 用户名='%s'", username)
    login_filter = User.username == username
    if re.fullmatch(r"1[3-9]\d{9}", username):
        login_filter = or_(User.username == username, User.telephone == username)
    result = await session.execute(select(User).where(login_filter))
    user = result.scalar_one_or_none()

    if not user:
        await record_failure()
        logger.warning("登录失败: 用户不存在 - 用户名='%s'", username)
        return LoginResponse(success=False, message="用户名或密码错误")

    if not user.password:
        await record_failure()
        logger.warning("登录失败: 用户没有设置密码 - 用户名='%s'", username)
        return LoginResponse(success=False, message="用户名或密码错误")

    if not await asyncio.to_thread(
        verify_password,
        payload.password,
        user.password,
    ):
        await record_failure()
        logger.warning("登录失败: 密码不匹配 - 用户名='%s'", username)
        return LoginResponse(success=False, message="用户名或密码错误")

    if not user.is_active:
        await record_failure()
        logger.warning("登录失败: 账户已被禁用 - 用户名='%s'", username)
        return LoginResponse(success=False, message="账户已被禁用")

    await clear_login_failures(redis_client, "username", username)
    token = await _issue_auth_token(redis_client, user)

    await session.execute(
        update(User)
        .where(User.user_id == user.user_id)
        .values(last_login=datetime.now())
    )
    await session.commit()

    user.last_login = datetime.now()

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


class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = None
    old_password: Optional[str] = None
    new_password: str


class ChangePasswordResponse(BaseModel):
    success: bool
    message: Optional[str] = None


def hash_password(password: str) -> str:
    """生成密码哈希"""
    from werkzeug.security import generate_password_hash

    return generate_password_hash(password)


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

    if not await asyncio.to_thread(
        verify_password,
        current_password,
        user.password,
    ):
        raise HTTPException(status_code=400, detail="当前密码错误")

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
