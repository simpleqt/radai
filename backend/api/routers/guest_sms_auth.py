"""Guest-only SMS verification login backed by Tencent Cloud SMS."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache

import httpx
from dotenv import dotenv_values
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import _issue_auth_token
from backend.api.sms_utils import SMS_RATE_LIMIT_MESSAGE as SMS_RATE_LIMIT_MESSAGE
from backend.api.sms_utils import (
    client_ip as _client_ip,
)
from backend.api.sms_utils import (
    sms_rate_limited_response as _sms_rate_limited_response,
)
from backend.application.auth_security import (
    consume_captcha,
    consume_sms_ip_quota,
    consume_verification_code,
    revoke_user_sessions,
)
from backend.application.captcha import build_captcha_text as _build_captcha_text
from backend.config.loggers import get_logger
from backend.db.connection import get_async_session
from backend.db.models import User
from backend.db.redis import get_redis_client

router = APIRouter(prefix="/guest-sms", tags=["guest-短信登录"])
logger = get_logger(__name__)

CAPTCHA_TTL_SECONDS = 300
CAPTCHA_MAX_ATTEMPTS = 5
SMS_CODE_TTL_SECONDS = 300
SMS_CODE_MAX_ATTEMPTS = 5
SMS_RESEND_SECONDS = 60
SMS_IP_RATE_WINDOW_SECONDS = 10 * 60
SMS_IP_RATE_MAX_REQUESTS = 10
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,20}$")

TENCENT_SMS_HOST = "sms.tencentcloudapi.com"
TENCENT_SMS_SERVICE = "sms"
TENCENT_SMS_ACTION = "SendSms"
TENCENT_SMS_VERSION = "2021-01-11"


class GuestSmsCaptchaResponse(BaseModel):
    captcha_id: str
    image_data: str
    expires_in: int


class GuestSmsSendCodeRequest(BaseModel):
    telephone: str
    captcha_id: str
    captcha_answer: str


class GuestSmsLoginRequest(BaseModel):
    telephone: str
    code: str


class GuestSmsRegisterRequest(BaseModel):
    telephone: str
    username: str | None = None
    code: str
    password: str
    confirm_password: str
    display_name: str | None = None


def _mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) == 11 else phone


def _guest_username(phone: str) -> str:
    return f"guest_{phone}"


def _is_trusted_guest_sms_user(user: User, phone: str) -> bool:
    """Return whether an existing row was provisioned by this SMS flow."""
    return (
        str(getattr(user, "username", "") or "") == _guest_username(phone)
        and str(getattr(user, "role", "user") or "user").lower() == "user"
        and str(getattr(user, "api_key", "") or "").startswith("guest_sms_")
    )


def _hash_password(password: str) -> str:
    from werkzeug.security import generate_password_hash

    return generate_password_hash(password)


def _normalize_display_name(value: str | None, phone: str) -> str:
    display_name = (value or "").strip() or _mask_phone(phone)
    return display_name[:50]


@lru_cache(maxsize=1)
def _backend_env() -> dict[str, str]:
    env_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    )
    return {
        key: str(value)
        for key, value in dotenv_values(env_path).items()
        if value is not None
    }


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None:
        return value.strip()
    return (_backend_env().get(name) or default).strip()


def _sms_template_params(code: str) -> list[str]:
    mode = _env("TENCENT_SMS_TEMPLATE_PARAM_MODE", "code_only").lower()
    expire_minutes = str(max(1, SMS_CODE_TTL_SECONDS // 60))
    if mode in {"code", "code_only", "1"}:
        return [code]
    return [code, expire_minutes]


def _friendly_tencent_sms_error(code: str, message: str | None) -> str:
    if code == "LimitExceeded.PhoneNumberDailyLimit":
        return "该手机号今日验证码发送次数已达到腾讯云频控上限，请在腾讯云短信控制台调整单手机号日发送上限"
    if code == "LimitExceeded.PhoneNumberOneHourLimit":
        return "该手机号1小时内验证码发送次数已达到腾讯云频控上限，请稍后再试或调整腾讯云频控"
    if code == "LimitExceeded.PhoneNumberThirtySecondLimit":
        return "验证码发送过于频繁，请稍后再试"
    if code == "LimitExceeded.PhoneNumberSameContentDailyLimit":
        return (
            "该手机号今日相同内容短信发送次数已达到腾讯云频控上限，请调整腾讯云频控策略"
        )
    if code.startswith("LimitExceeded."):
        return "短信发送命中腾讯云频控限制，请在腾讯云短信控制台调整发送频率限制"
    return message or "验证码发送失败"


def _required_sms_config() -> tuple[dict[str, str], str | None]:
    config = {
        "secret_id": _env("TENCENT_SMS_SECRET_ID") or _env("TENCENTCLOUD_SECRET_ID"),
        "secret_key": _env("TENCENT_SMS_SECRET_KEY") or _env("TENCENTCLOUD_SECRET_KEY"),
        "sdk_app_id": _env("TENCENT_SMS_SDK_APP_ID"),
        "sign_name": _env("TENCENT_SMS_SIGN_NAME"),
        "template_id": _env("TENCENT_SMS_TEMPLATE_ID"),
        "region": _env("TENCENT_SMS_REGION", "ap-guangzhou"),
    }
    missing = [
        name for name, value in config.items() if name not in {"region"} and not value
    ]
    if missing:
        return config, "访客短信服务未配置完整，请补充腾讯云短信 AppId、签名和模板 ID"
    return config, None


def _render_captcha_image(text: str) -> str:
    from PIL import Image, ImageDraw, ImageFont

    width, height = 180, 56
    image = Image.new("RGB", (width, height), (248, 250, 252))
    draw = ImageDraw.Draw(image)

    for _ in range(10):
        draw.line(
            (
                secrets.randbelow(width),
                secrets.randbelow(height),
                secrets.randbelow(width),
                secrets.randbelow(height),
            ),
            fill=(203, 213, 225),
            width=1,
        )

    for _ in range(80):
        draw.point(
            (secrets.randbelow(width), secrets.randbelow(height)),
            fill=(
                120 + secrets.randbelow(80),
                130 + secrets.randbelow(80),
                145 + secrets.randbelow(80),
            ),
        )

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28
        )
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    x = max(10, (width - (bbox[2] - bbox[0])) // 2)
    y = max(6, (height - (bbox[3] - bbox[1])) // 2 - 2)
    draw.text((x + 1, y + 1), text, font=font, fill=(148, 163, 184))
    draw.text((x, y), text, font=font, fill=(30, 41, 59))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


async def _verify_captcha(
    redis_client: Redis,
    captcha_id: str,
    answer: str,
) -> tuple[bool, str]:
    normalized_id = (captcha_id or "").strip()
    normalized_answer = (answer or "").strip()
    if not normalized_id or not normalized_answer:
        return False, "请先完成人机验证"

    result = await consume_captcha(
        redis_client,
        f"guest_sms:captcha:{normalized_id}",
        normalized_answer,
        max_attempts=CAPTCHA_MAX_ATTEMPTS,
        fallback_ttl_seconds=CAPTCHA_TTL_SECONDS,
    )
    if result == "ok":
        return True, ""
    if result == "exhausted":
        return False, "人机验证错误次数过多，请刷新后重试"
    if result == "invalid":
        return False, "人机验证答案错误"
    if result == "expired":
        return False, "人机验证已过期，请刷新后重试"
    return False, "人机验证失败，请刷新后重试"


async def _consume_guest_sms_code(
    redis_client: Redis,
    telephone: str,
    code: str,
) -> tuple[bool, str]:
    result = await consume_verification_code(
        redis_client,
        f"guest_sms:code:{telephone}",
        code,
        max_attempts=SMS_CODE_MAX_ATTEMPTS,
        fallback_ttl_seconds=SMS_CODE_TTL_SECONDS,
    )
    if result == "ok":
        return True, ""
    if result == "exhausted":
        return False, "验证码错误次数过多，请重新获取"
    if result == "invalid":
        return False, "验证码错误"
    return False, "验证码已过期"


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _tencent_auth_header(
    *,
    secret_id: str,
    secret_key: str,
    timestamp: int,
    payload_json: str,
) -> str:
    algorithm = "TC3-HMAC-SHA256"
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
    canonical_headers = (
        f"content-type:application/json; charset=utf-8\nhost:{TENCENT_SMS_HOST}\n"
    )
    signed_headers = "content-type;host"
    canonical_request = "\n".join(
        [
            "POST",
            "/",
            "",
            canonical_headers,
            signed_headers,
            _sha256_hex(payload_json),
        ]
    )
    credential_scope = f"{date}/{TENCENT_SMS_SERVICE}/tc3_request"
    string_to_sign = "\n".join(
        [
            algorithm,
            str(timestamp),
            credential_scope,
            _sha256_hex(canonical_request),
        ]
    )
    secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _sign(secret_date, TENCENT_SMS_SERVICE)
    secret_signing = _sign(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return (
        f"{algorithm} Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


async def _send_tencent_sms(phone: str, code: str) -> tuple[bool, str]:
    config, error = _required_sms_config()
    if error:
        return False, error

    payload = {
        "PhoneNumberSet": [f"+86{phone}"],
        "SmsSdkAppId": config["sdk_app_id"],
        "SignName": config["sign_name"],
        "TemplateId": config["template_id"],
        "TemplateParamSet": _sms_template_params(code),
    }
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    timestamp = int(time.time())
    headers = {
        "Authorization": _tencent_auth_header(
            secret_id=config["secret_id"],
            secret_key=config["secret_key"],
            timestamp=timestamp,
            payload_json=payload_json,
        ),
        "Content-Type": "application/json; charset=utf-8",
        "Host": TENCENT_SMS_HOST,
        "X-TC-Action": TENCENT_SMS_ACTION,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": TENCENT_SMS_VERSION,
        "X-TC-Region": config["region"],
    }

    try:
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            response = await client.post(
                f"https://{TENCENT_SMS_HOST}", content=payload_json, headers=headers
            )
        body = response.json()
    except Exception as exc:
        logger.warning("[Guest SMS] 腾讯云短信调用异常: %s", exc)
        return False, "短信网关异常，请稍后再试"

    if response.status_code >= 400:
        logger.warning(
            "[Guest SMS] 腾讯云短信 HTTP 失败: status=%s body=%s",
            response.status_code,
            body,
        )
        return False, "验证码发送失败，请稍后再试"

    send_status = (body.get("Response") or {}).get("SendStatusSet") or []
    first_status = send_status[0] if send_status else {}
    if first_status.get("Code") == "Ok":
        return True, "验证码已发送"

    logger.warning("[Guest SMS] 腾讯云短信返回失败: %s", body)
    response_error = (body.get("Response") or {}).get("Error") or {}
    code = str(first_status.get("Code") or response_error.get("Code") or "")
    message = first_status.get("Message") or response_error.get("Message")
    return False, _friendly_tencent_sms_error(code, message)


@router.get("/captcha", response_model=GuestSmsCaptchaResponse)
async def issue_guest_sms_captcha(redis_client: Redis = Depends(get_redis_client)):
    text, answer = _build_captcha_text()
    captcha_id = secrets.token_urlsafe(24)
    await redis_client.setex(
        f"guest_sms:captcha:{captcha_id}",
        CAPTCHA_TTL_SECONDS,
        json.dumps({"answer": answer, "attempts": 0}),
    )
    image_data = await asyncio.to_thread(_render_captcha_image, text)
    return GuestSmsCaptchaResponse(
        captcha_id=captcha_id,
        image_data=image_data,
        expires_in=CAPTCHA_TTL_SECONDS,
    )


@router.post("/send-code")
async def send_guest_sms_code(
    data: GuestSmsSendCodeRequest,
    request: Request,
    redis_client: Redis = Depends(get_redis_client),
):
    telephone = data.telephone.strip()
    if not PHONE_RE.match(telephone):
        return {"success": False, "error": "请输入正确的手机号"}

    captcha_ok, captcha_error = await _verify_captcha(
        redis_client, data.captcha_id, data.captcha_answer
    )
    if not captcha_ok:
        return {"success": False, "error": captcha_error, "refresh_captcha": True}

    lock_key = f"guest_sms:send_lock:{telephone}"
    acquired = await redis_client.set(
        lock_key,
        "1",
        ex=SMS_RESEND_SECONDS,
        nx=True,
    )
    if not acquired:
        return _sms_rate_limited_response(SMS_RESEND_SECONDS)

    allowed, retry_after = await consume_sms_ip_quota(
        redis_client,
        _client_ip(request),
        window_seconds=SMS_IP_RATE_WINDOW_SECONDS,
        max_requests=SMS_IP_RATE_MAX_REQUESTS,
    )
    if not allowed:
        await redis_client.delete(lock_key)
        return _sms_rate_limited_response(retry_after)

    code = "".join(str(secrets.randbelow(10)) for _ in range(6))
    sent, message = await _send_tencent_sms(telephone, code)
    if not sent:
        await redis_client.delete(lock_key)
        return {"success": False, "error": message}

    await redis_client.setex(
        f"guest_sms:code:{telephone}",
        SMS_CODE_TTL_SECONDS,
        json.dumps({"code": code, "attempts": 0}),
    )
    return {
        "success": True,
        "message": message,
        "expires_in": SMS_CODE_TTL_SECONDS,
        "retry_after": SMS_RESEND_SECONDS,
    }


@router.post("/register")
async def guest_sms_register(
    data: GuestSmsRegisterRequest,
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    telephone = data.telephone.strip()
    requested_username = (data.username or "").strip()
    code = data.code.strip()
    password = data.password
    confirm_password = data.confirm_password
    if not PHONE_RE.match(telephone):
        return {"success": False, "error": "请输入正确的手机号"}
    if requested_username and not USERNAME_RE.fullmatch(requested_username):
        return {"success": False, "error": "用户名需为3-20位中文、字母、数字或下划线"}
    if not code:
        return {"success": False, "error": "请输入验证码"}
    if len(password) < 6:
        return {"success": False, "error": "密码至少6位"}
    if password != confirm_password:
        return {"success": False, "error": "两次输入的密码不一致"}

    code_ok, code_error = await _consume_guest_sms_code(
        redis_client,
        telephone,
        code,
    )
    if not code_ok:
        return {"success": False, "error": code_error}

    username = requested_username or _guest_username(telephone)
    result = await session.execute(select(User).where(User.telephone == telephone))
    user = result.scalar_one_or_none()
    # Keep the lookup on ``execute`` so lightweight/test sessions and the
    # production AsyncSession share the same SQLAlchemy contract.
    username_result = await session.execute(select(User).where(User.username == username))
    username_owner = username_result.scalar_one_or_none()
    if username_owner is not None and username_owner is not user:
        return {"success": False, "error": "该用户名已被使用，请更换用户名"}
    now = datetime.now()
    display_name = _normalize_display_name(data.display_name, telephone)
    password_hash = await asyncio.to_thread(_hash_password, password)

    if user is None:
        user = User(
            user_id=str(uuid.uuid4()),
            username=username,
            telephone=telephone,
            display_name=display_name,
            password=password_hash,
            email=None,
            api_key=f"guest_sms_{secrets.token_urlsafe(24)}",
            role="user",
            is_active=True,
            created_at=now,
            updated_at=now,
            last_login=now,
        )
        session.add(user)
        await session.flush()
    else:
        if not user.is_active:
            return {"success": False, "error": "账户已被禁用"}
        if not str(user.api_key or "").startswith("guest_sms_"):
            logger.warning(
                "[Guest SMS] 拒绝注册流程合并其他认证来源账户: user_id=%s",
                user.user_id,
            )
            return {"success": False, "error": "该手机号账户无法使用短信注册"}
        if user.password:
            return {"success": False, "error": "该手机号已注册，请直接登录"}
        await session.execute(
            update(User)
            .where(User.user_id == user.user_id)
            .values(
                password=password_hash,
                username=username,
                telephone=telephone,
                display_name=display_name,
                last_login=now,
                updated_at=now,
            )
        )
        user.password = password_hash
        user.username = username
        user.telephone = telephone
        user.display_name = display_name
        user.last_login = now
        user.updated_at = now
        await session.flush()

    await revoke_user_sessions(redis_client, user.user_id)
    await session.commit()
    token = await _issue_auth_token(redis_client, user, display_name)
    return {
        "success": True,
        "token": token,
        "user": {
            "user_id": user.user_id,
            "username": username,
            "display_name": display_name,
            "role": user.role or "user",
            "is_active": user.is_active,
            "last_login": now.isoformat(),
        },
    }


@router.post("/login")
async def guest_sms_login(
    data: GuestSmsLoginRequest,
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    telephone = data.telephone.strip()
    code = data.code.strip()
    if not PHONE_RE.match(telephone):
        return {"success": False, "error": "请输入正确的手机号"}
    if not code:
        return {"success": False, "error": "请输入验证码"}

    code_ok, code_error = await _consume_guest_sms_code(
        redis_client,
        telephone,
        code,
    )
    if not code_ok:
        return {"success": False, "error": code_error}

    username = _guest_username(telephone)
    result = await session.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    now = datetime.now()

    if user is None:
        user = User(
            user_id=str(uuid.uuid4()),
            username=username,
            password=None,
            email=None,
            api_key=f"guest_sms_{secrets.token_urlsafe(24)}",
            role="user",
            is_active=True,
            created_at=now,
            updated_at=now,
            last_login=now,
        )
        session.add(user)
        await session.flush()
    else:
        if not user.is_active:
            return {"success": False, "error": "账户已被禁用"}
        if not _is_trusted_guest_sms_user(user, telephone):
            logger.warning(
                "[Guest SMS] 拒绝登录流程合并其他认证来源账户: user_id=%s",
                user.user_id,
            )
            return {"success": False, "error": "该手机号账户无法使用短信登录"}
        await session.execute(
            update(User)
            .where(User.user_id == user.user_id)
            .values(last_login=now, updated_at=now)
        )
        await session.flush()

    await session.commit()
    display_name = _mask_phone(telephone)
    token = await _issue_auth_token(redis_client, user, display_name)
    return {
        "success": True,
        "token": token,
        "user": {
            "user_id": user.user_id,
            "username": username,
            "display_name": display_name,
            "role": user.role or "user",
            "is_active": user.is_active,
            "last_login": now.isoformat(),
        },
    }
