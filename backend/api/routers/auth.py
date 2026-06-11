"""认证路由"""

import secrets
import json
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import xml.etree.ElementTree as ET

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.settings import settings
from backend.db.connection import get_async_session
from backend.db.models import User
from backend.db.redis import get_redis_client

from backend.config.loggers import get_logger

router = APIRouter(prefix="/auth", tags=["认证"])
logger = get_logger(__name__)

TOKEN_EXPIRE_HOURS = 24
CAS_TARGET_URL_COOKIE = "cas_target_url"
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


class UserResponse(BaseModel):
    user_id: str
    username: str
    email: Optional[str]
    role: str = "user"
    is_active: bool
    created_at: Optional[datetime]
    last_login: Optional[datetime]


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


def _is_safe_redirect_target(target: str, request: Request) -> bool:
    """只允许站内相对路径或同源绝对地址，避免开放重定向。"""
    if not target:
        return False
    if target.startswith("/") and not target.startswith("//"):
        return True

    parsed = urlparse(target)
    if not parsed.scheme or not parsed.netloc:
        return False

    current = urlparse(str(request.base_url))
    return parsed.scheme == current.scheme and parsed.netloc == current.netloc


def _build_cas_service_url(request: Request) -> str:
    configured = (settings.cas_service_url or "").strip()
    if not configured:
        return str(request.url_for("cas_callback"))

    # 动态根据当前请求的 Host 替换配置中的域名，支持多域名（如 csh-ai.example.com 和 radai2.example.com）
    try:
        # 获取请求的真实 Host 和 Scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if host:
            # 移除非标准端口（如果存在的话）
            host = host.split(":")[0]
            # 获取请求的真实 Scheme，默认为 https
            scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
            
            # 解析配置 of service_url
            parsed = urlparse(configured)
            # 替换 scheme 和 netloc
            new_netloc = host
            if parsed.port:
                new_netloc = f"{host}:{parsed.port}"
            
            # 构建新的 URL
            new_url = urlunparse((
                scheme,
                new_netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment
            ))
            return new_url
    except Exception as e:
        logger.error(f"构建动态 CAS 回调 URL 失败: {e}")

    return configured


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

    async with httpx.AsyncClient(timeout=timeout, verify=settings.cas_verify_ssl) as client:
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
    token_data = {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": (display_name or user.username),
        "role": user.role if hasattr(user, "role") else "user",
    }
    await redis_client.setex(
        f"auth:token:{token}",
        TOKEN_EXPIRE_HOURS * 3600,
        json.dumps(token_data),
    )
    return token


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
) -> User:
    """获取当前用户"""
    auth_header = request.headers.get("Authorization", "")
    token = None

    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.query_params.get("token")

    if not token:
        raise HTTPException(status_code=401, detail="未提供认证令牌")

    token_data_str = await redis_client.get(f"auth:token:{token}")
    if not token_data_str:
        raise HTTPException(status_code=401, detail="无效的认证令牌或已过期")

    token_info = json.loads(token_data_str)
    
    result = await session.execute(
        select(User).where(User.user_id == token_info["user_id"])
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="账户已被禁用")

    return user


@router.post("/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    """用户登录"""
    logger.info("登录尝试: 用户名='%s'", request.username)
    result = await session.execute(
        select(User).where(User.username == request.username)
    )
    user = result.scalar_one_or_none()

    if not user:
        logger.warning(f"登录失败: 用户不存在 - 用户名='{request.username}'")
        return LoginResponse(success=False, message="用户名或密码错误")

    if not user.password:
        logger.warning(f"登录失败: 用户没有设置密码 - 用户名='{request.username}'")
        return LoginResponse(success=False, message="用户名或密码错误")

    if not verify_password(request.password, user.password):
        logger.warning(f"登录失败: 密码不匹配 - 用户名='{request.username}'")
        return LoginResponse(success=False, message="用户名或密码错误")

    if not user.is_active:
        logger.warning(f"登录失败: 账户已被禁用 - 用户名='{request.username}'")
        return LoginResponse(success=False, message="账户已被禁用")

    token = await _issue_auth_token(redis_client, user, user.username)

    await session.execute(
        update(User)
        .where(User.user_id == user.user_id)
        .values(last_login=datetime.now())
    )
    await session.commit()

    return LoginResponse(
        success=True,
        token=token,
        user={
            "user_id": user.user_id,
            "username": user.username,
            "display_name": user.username,
            "email": user.email,
            "role": user.role or "user",
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login": datetime.now().isoformat(),
        },
    )


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
            secure=request.url.scheme == "https",
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
    """CAS 回调：校验 ticket，创建/更新本地用户，并固定为 user 权限。"""
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
        user.role = "user"
        user.is_active = True
        user.last_login = datetime.now()
        user.updated_at = datetime.now()

    await session.commit()

    display_name = (cas_display_name or cas_username).strip() or cas_username
    token = await _issue_auth_token(redis_client, user, display_name)

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
                "token": token,
                "username": user.username,
                "display_name": display_name,
                "user_id": user.user_id,
                "role": "user",
                "targetUrl": target_after_login,
            },
        ),
        status_code=302,
    )
    redirect_response.delete_cookie(CAS_TARGET_URL_COOKIE, path="/")
    return redirect_response


@router.get("/cas/logout")
async def cas_logout_redirect(request: Request, redirect: str | None = None):
    """浏览器跳转用 CAS 注销入口。"""
    if not settings.cas_enabled:
        raise HTTPException(status_code=404, detail="CAS 登录未启用")

    fallback = settings.cas_frontend_logout_redirect or "/login"
    target = redirect if redirect and _is_safe_redirect_target(redirect, request) else fallback
    if target.startswith("/"):
        base_url = str(request.base_url).rstrip("/")
        target = f"{base_url}{target}"
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
    target = redirect if redirect and _is_safe_redirect_target(redirect, request) else fallback
    if target.startswith("/"):
        base_url = str(request.base_url).rstrip("/")
        target = f"{base_url}{target}"
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
    return VerifyResponse(
        valid=True,
        user={
            "user_id": user.user_id,
            "username": user.username,
            "display_name": user.username,
            "email": user.email,
            "role": user.role or "user",
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login": user.last_login.isoformat() if user.last_login else None,
        },
    )


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    """获取当前用户信息"""
    return UserResponse(
        user_id=user.user_id,
        username=user.username,
        email=user.email,
        role=user.role or "user",
        is_active=user.is_active,
        created_at=user.created_at,
        last_login=user.last_login,
    )


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class ChangePasswordResponse(BaseModel):
    success: bool
    message: Optional[str] = None


def hash_password(password: str) -> str:
    """生成密码哈希"""
    from werkzeug.security import generate_password_hash
    return generate_password_hash(password)


@router.post("/change-password", response_model=ChangePasswordResponse)
async def change_password(
    request: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """修改密码"""
    if not user.password or not verify_password(request.old_password, user.password):
        return ChangePasswordResponse(success=False, message="原密码错误")

    if len(request.new_password) < 6:
        return ChangePasswordResponse(success=False, message="新密码长度不能少于6位")

    hashed_password = hash_password(request.new_password)

    await session.execute(
        update(User)
        .where(User.user_id == user.user_id)
        .values(password=hashed_password, updated_at=datetime.now())
    )
    await session.commit()

    return ChangePasswordResponse(success=True, message="密码修改成功")
