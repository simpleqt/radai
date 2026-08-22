import asyncio
import base64
import io
import json
import os
import re
import secrets
from datetime import datetime

import httpx
import shortuuid
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
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
from backend.config.settings_sections import ENV_FILE
from backend.db.connection import get_async_session
from backend.db.models import User
from backend.db.redis import get_redis_client

logger = get_logger(__name__)

# 比较并删除必须在 Redis 端原子执行：网关调用可能挂满锁 TTL，超时后
# 锁被后继请求抢得，前任的迟到 DELETE 会误删后继的锁，绕过重发间隔。
_RELEASE_SMS_LOCK_IF_OWNER_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


async def _release_sms_lock(redis_client, lock_key: str, owner: str) -> None:
    try:
        await redis_client.eval(
            _RELEASE_SMS_LOCK_IF_OWNER_LUA, 1, lock_key, owner
        )
    except Exception:
        pass


try:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=False)
except Exception:
    logger.warning("短信认证环境变量文件加载失败: %s", ENV_FILE, exc_info=True)

# 配置
SCHOOL_SMS_CONFIG_KEYS = (
    "SMS_SEND_MESSAGE_API_URL",
    "SMS_APP_ID",
    "SMS_APP_SECRET",
)


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _school_sms_config() -> tuple[dict[str, str], list[str]]:
    config = {
        "get_channels_url": _env("SMS_GET_CHANNELS_API_URL"),
        "send_message_url": _env("SMS_SEND_MESSAGE_API_URL"),
        "app_id": _env("SMS_APP_ID"),
        "app_secret": _env("SMS_APP_SECRET"),
    }
    missing = [key for key in SCHOOL_SMS_CONFIG_KEYS if not _env(key)]
    return config, missing


CAPTCHA_TTL_SECONDS = 300
CAPTCHA_MAX_ATTEMPTS = 3
SMS_CODE_TTL_SECONDS = 300
SMS_CODE_MAX_ATTEMPTS = 5
SMS_RESEND_SECONDS = 60
SMS_IP_RATE_WINDOW_SECONDS = 10 * 60
SMS_IP_RATE_MAX_REQUESTS = 10
SCHOOL_SMS_FAILURE_MESSAGE = "验证码发送失败，请稍后再试"
PHONE_ALREADY_REGISTERED_MESSAGE = "该手机号已注册，请直接登录"
PHONE_NOT_REGISTERED_MESSAGE = "该手机号未注册"
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

router = APIRouter(prefix="", tags=["短信认证"])


class SmsCaptchaResponse(BaseModel):
    captcha_id: str
    image_data: str
    expires_in: int


class SendSmsRequest(BaseModel):
    telephone: str = Field(min_length=1, max_length=32)
    type: str = Field(default="register", max_length=32)
    captcha_id: str | None = Field(default=None, max_length=64)
    captcha_answer: str | None = Field(default=None, max_length=32)


class RegisterRequest(BaseModel):
    telephone: str = Field(min_length=1, max_length=32)
    code: str = Field(min_length=1, max_length=16)
    # username 与 users.username String(50) 对齐，超长原先是 DB 异常 500。
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=256)
    confirmPassword: str = Field(min_length=6, max_length=256)


class ForgotPasswordRequest(BaseModel):
    telephone: str = Field(min_length=1, max_length=32)
    code: str = Field(min_length=1, max_length=16)
    newPassword: str = Field(min_length=6, max_length=256)
    confirmPassword: str = Field(min_length=6, max_length=256)


class SmsLoginRequest(BaseModel):
    telephone: str = Field(min_length=1, max_length=32)
    code: str = Field(min_length=1, max_length=16)


def is_strong_password(password: str) -> bool:
    if len(password) < 6:
        return False
    return True


def hash_password(password: str) -> str:
    from werkzeug.security import generate_password_hash

    return generate_password_hash(password)


def _captcha_key(captcha_id: str) -> str:
    return f"sms:captcha:{captcha_id}"


def _sms_code_key(sms_type: str, telephone: str) -> str:
    return f"sms:code:{sms_type}:{telephone}"


def _sms_send_lock_key(telephone: str) -> str:
    return f"sms:send_lock:{telephone}"


def _guest_username(telephone: str) -> str:
    return f"guest_{telephone}"


def _has_usable_password(user: User) -> bool:
    """Whether the account can authenticate by password (non-empty hash)."""
    return bool(str(getattr(user, "password", "") or "").strip())


async def _find_user_by_telephone(
    session: AsyncSession,
    telephone: str,
) -> User | None:
    """按绑定手机号查找用户，兼容历史「用户名=手机号」及访客短信账户。

    安全约束(R4-RT-1):`username == 手机号` 的历史回退**只对无口令的短信
    来源账户**生效。口令账户(尤其管理员)绝不经此命中——否则任何控制该号码
    短信的人,只要某账户的用户名恰好等于该号码(但并未把该号码绑定到
    telephone 字段),即可免口令登入或重置其口令,构成接管/提权。telephone
    字段精确绑定是权威匹配(绑定本身证明拥有该号码),不受此限制。
    """
    result = await session.execute(select(User).where(User.telephone == telephone))
    user = result.scalars().first()
    if user is not None:
        return user

    result = await session.execute(select(User).where(User.username == telephone))
    user = result.scalars().first()
    if user is not None:
        if not _has_usable_password(user):
            return user
        logger.warning(
            "[SMS] 拒绝短信通道命中口令账户(用户名恰为手机号但未绑定 telephone): "
            "user_id=%s",
            user.user_id,
        )

    result = await session.execute(
        select(User).where(User.username == _guest_username(telephone))
    )
    return result.scalars().first()


def _user_auth_payload(user: User) -> dict:
    display_name = (
        str(getattr(user, "display_name", "") or "").strip()
        or str(user.username or "")
    )
    return {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": display_name,
        "role": user.role or "user",
    }


def _render_captcha_image(text: str) -> str:
    from PIL import Image, ImageDraw, ImageFont

    width, height = 180, 56
    image = Image.new("RGB", (width, height), (248, 250, 252))
    draw = ImageDraw.Draw(image)

    for _ in range(10):
        x1 = secrets.randbelow(width)
        y1 = secrets.randbelow(height)
        x2 = secrets.randbelow(width)
        y2 = secrets.randbelow(height)
        draw.line((x1, y1, x2, y2), fill=(203, 213, 225), width=1)

    for _ in range(80):
        x = secrets.randbelow(width)
        y = secrets.randbelow(height)
        color = (
            120 + secrets.randbelow(80),
            130 + secrets.randbelow(80),
            145 + secrets.randbelow(80),
        )
        draw.point((x, y), fill=color)

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28
        )
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = max(10, (width - text_width) // 2)
    y = max(6, (height - text_height) // 2 - 2)
    draw.text((x + 1, y + 1), text, font=font, fill=(148, 163, 184))
    draw.text((x, y), text, font=font, fill=(30, 41, 59))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


async def _verify_sms_captcha(
    redis_client: Redis,
    captcha_id: str | None,
    answer: str | None,
) -> tuple[bool, str]:
    normalized_id = (captcha_id or "").strip()
    normalized_answer = (answer or "").strip()
    if not normalized_id or not normalized_answer:
        return False, "请先完成人机验证"

    result = await consume_captcha(
        redis_client,
        _captcha_key(normalized_id),
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


async def _consume_sms_code(
    redis_client: Redis,
    sms_type: str,
    telephone: str,
    code: str,
) -> tuple[bool, str]:
    key = _sms_code_key(sms_type, telephone)
    result = await consume_verification_code(
        redis_client,
        key,
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
    if result == "expired":
        return False, "验证码已过期"
    return False, "验证码验证失败"


@router.get("/api/sms-captcha", response_model=SmsCaptchaResponse)
async def issue_sms_captcha(redis_client: Redis = Depends(get_redis_client)):
    text, answer = _build_captcha_text()
    captcha_id = secrets.token_urlsafe(24)
    await redis_client.setex(
        _captcha_key(captcha_id),
        CAPTCHA_TTL_SECONDS,
        json.dumps({"answer": answer, "attempts": 0}),
    )
    image_data = await asyncio.to_thread(_render_captcha_image, text)
    return SmsCaptchaResponse(
        captcha_id=captcha_id,
        image_data=image_data,
        expires_in=CAPTCHA_TTL_SECONDS,
    )


@router.post("/api/send-sms-code")
async def send_sms_code(
    data: SendSmsRequest,
    request: Request,
    redis_client: Redis = Depends(get_redis_client),
    session: AsyncSession = Depends(get_async_session),
):
    telephone = data.telephone.strip()
    sms_type = data.type

    if not PHONE_RE.match(telephone):
        return {"success": False, "error": "请输入正确的手机号"}

    if sms_type not in {"register", "forgot", "login"}:
        return {"success": False, "error": "短信类型无效"}

    captcha_ok, captcha_error = await _verify_sms_captcha(
        redis_client, data.captcha_id, data.captcha_answer
    )
    if not captcha_ok:
        return {"success": False, "error": captcha_error, "refresh_captcha": True}

    existing_user = await _find_user_by_telephone(session, telephone)
    if sms_type == "register" and existing_user is not None:
        return {
            "success": False,
            "error": PHONE_ALREADY_REGISTERED_MESSAGE,
            "refresh_captcha": True,
        }
    if sms_type in {"forgot", "login"} and existing_user is None:
        return {
            "success": False,
            "error": PHONE_NOT_REGISTERED_MESSAGE,
            "refresh_captcha": True,
        }

    lock_key = _sms_send_lock_key(telephone)
    lock_owner = secrets.token_urlsafe(16)
    acquired = await redis_client.set(
        lock_key,
        lock_owner,
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
        await _release_sms_lock(redis_client, lock_key, lock_owner)
        return _sms_rate_limited_response(retry_after)

    sms_config, missing_config = _school_sms_config()
    if missing_config:
        await _release_sms_lock(redis_client, lock_key, lock_owner)
        logger.error(
            "学校短信网关配置不完整: missing=%s",
            ",".join(missing_config),
        )
        return {
            "success": False,
            "error": SCHOOL_SMS_FAILURE_MESSAGE,
            "refresh_captcha": True,
        }

    code = "".join(str(secrets.randbelow(10)) for _ in range(6))

    try:
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            # 外部接口 getChannels 报错提示缺少参数，且该步骤通常仅为获取类型。
            # 直接使用 sms 作为默认值以确保核心发送逻辑能够运行。
            channel_encode = "sms"

            payload = {
                "title": "验证码",
                "content": f"【智能体后台管理系统】您的验证码是：{code}，5分钟内有效。",
                "channels": channel_encode,
                "smsReceiver": telephone,
                "cron": "0",
            }
            form_data = {"msgData": json.dumps(payload, ensure_ascii=False)}
            sms_headers = {
                "appId": sms_config["app_id"],
                "token": sms_config["app_secret"],
            }

            resp_send = await client.post(
                sms_config["send_message_url"],
                data=form_data,
                headers=sms_headers,
                timeout=60,
            )
            result = resp_send.json()

            # 兼容处理：有些接口返回 code=200，有些直接返回带 message 的结构
            if (
                result.get("code") == 200
                or result.get("message") == "成功"
                or result.get("code") == 0
            ):
                await redis_client.setex(
                    _sms_code_key(sms_type, telephone),
                    SMS_CODE_TTL_SECONDS,
                    json.dumps({"code": code, "attempts": 0}),
                )
                return {
                    "success": True,
                    "message": "验证码已发送",
                    "expires_in": SMS_CODE_TTL_SECONDS,
                    "retry_after": SMS_RESEND_SECONDS,
                }
            else:
                await _release_sms_lock(redis_client, lock_key, lock_owner)
                logger.warning(
                    "学校短信网关返回失败: phone=%s status=%s body=%r",
                    f"{telephone[:3]}****{telephone[-4:]}",
                    getattr(resp_send, "status_code", None),
                    result,
                )
                return {
                    "success": False,
                    "error": SCHOOL_SMS_FAILURE_MESSAGE,
                    "refresh_captcha": True,
                }
    except Exception as exc:
        await _release_sms_lock(redis_client, lock_key, lock_owner)
        logger.exception(
            "学校短信网关调用异常: phone=%s error=%s",
            f"{telephone[:3]}****{telephone[-4:]}",
            exc,
        )
        return {
            "success": False,
            "error": SCHOOL_SMS_FAILURE_MESSAGE,
            "refresh_captcha": True,
        }


@router.post("/api/register")
async def register(
    data: RegisterRequest,
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    telephone = data.telephone.strip()
    code = data.code.strip()
    username = data.username.strip()
    password = data.password
    confirm_password = data.confirmPassword

    if not PHONE_RE.match(telephone):
        return {"success": False, "error": "请输入正确的手机号"}

    if len(username) < 3:
        return {"success": False, "error": "用户名至少3位"}

    if not is_strong_password(password):
        return {"success": False, "error": "密码强度不符合要求"}

    if password != confirm_password:
        return {"success": False, "error": "两次输入的密码不一致"}

    code_ok, code_error = await _consume_sms_code(
        redis_client, "register", telephone, code
    )
    if not code_ok:
        return {"success": False, "error": code_error}

    phone_user = await _find_user_by_telephone(session, telephone)
    if phone_user is not None:
        return {"success": False, "error": PHONE_ALREADY_REGISTERED_MESSAGE}

    stmt = select(User).where(User.username == username)
    result = await session.execute(stmt)
    existing = result.scalars().first()
    if existing:
        return {"success": False, "error": "用户名已存在"}

    user_id = shortuuid.uuid()[:16]

    password_hash = await asyncio.to_thread(hash_password, password)
    new_user = User(
        user_id=user_id,
        username=username,
        display_name=username,
        telephone=telephone,
        password=password_hash,
        api_key=f"sms_{secrets.token_urlsafe(24)}",
        role="user",
        is_active=True,
    )
    session.add(new_user)
    await session.commit()

    token = await _issue_auth_token(redis_client, new_user, username)
    user_dict = {
        "user_id": user_id,
        "username": username,
        "display_name": username,
        "role": "user",
    }

    return {
        "success": True,
        "token": token,
        "user": user_dict,
    }


@router.post("/api/sms-login")
async def sms_login(
    data: SmsLoginRequest,
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    telephone = data.telephone.strip()
    code = data.code.strip()

    if not PHONE_RE.match(telephone):
        return {"success": False, "error": "请输入正确的手机号"}
    if not code:
        return {"success": False, "error": "请输入验证码"}

    code_ok, code_error = await _consume_sms_code(
        redis_client, "login", telephone, code
    )
    if not code_ok:
        return {"success": False, "error": code_error}

    user = await _find_user_by_telephone(session, telephone)
    if user is None:
        return {"success": False, "error": PHONE_NOT_REGISTERED_MESSAGE}
    if not user.is_active:
        return {"success": False, "error": "账户已被禁用"}

    now = datetime.now()
    user.last_login = now
    if not getattr(user, "telephone", None):
        user.telephone = telephone
    await session.commit()

    display_name = (
        str(getattr(user, "display_name", "") or "").strip() or str(user.username)
    )
    token = await _issue_auth_token(redis_client, user, display_name)
    return {
        "success": True,
        "token": token,
        "user": _user_auth_payload(user),
    }


@router.post("/api/forgot-password")
async def forgot_password(
    data: ForgotPasswordRequest,
    session: AsyncSession = Depends(get_async_session),
    redis_client: Redis = Depends(get_redis_client),
):
    telephone = data.telephone.strip()
    code = data.code.strip()
    new_password = data.newPassword
    confirm_password = data.confirmPassword

    if not PHONE_RE.match(telephone):
        return {"success": False, "error": "请输入正确的手机号"}

    if not is_strong_password(new_password):
        return {"success": False, "error": "密码强度不符合要求"}

    if new_password != confirm_password:
        return {"success": False, "error": "两次输入的密码不一致"}

    code_ok, code_error = await _consume_sms_code(
        redis_client, "forgot", telephone, code
    )
    if not code_ok:
        return {"success": False, "error": code_error}

    user = await _find_user_by_telephone(session, telephone)
    if not user:
        return {"success": False, "error": PHONE_NOT_REGISTERED_MESSAGE}

    if not user.is_active:
        return {"success": False, "error": "账户已被禁用"}

    user.password = await asyncio.to_thread(hash_password, new_password)
    # 回填历史未绑定手机号的账户
    if not getattr(user, "telephone", None):
        user.telephone = telephone
    await revoke_user_sessions(redis_client, user.user_id)
    await session.commit()

    return {"success": True, "message": "密码重置成功"}
