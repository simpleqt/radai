import os
import random
import re
import json
import secrets
from datetime import datetime, timedelta
import httpx

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import shortuuid

from backend.db.connection import get_async_session
from backend.db.models import User
from backend.db.redis import get_redis_client
from backend.api.routers.auth import _issue_auth_token

# 配置
GET_CHANNELS_API_URL = os.getenv("SMS_GET_CHANNELS_URL", "")
SEND_MESSAGE_API_URL = os.getenv("SMS_SEND_MESSAGE_URL", "")
SMS_KEY = os.getenv("SMS_KEY", "")
SMS_SECRET = os.getenv("SMS_SECRET", "")

sms_headers = {"appId": SMS_KEY, "token": SMS_SECRET}

# 缓存改为使用内存字典
sms_code_store = {}

router = APIRouter(prefix="", tags=["短信认证"])

class SendSmsRequest(BaseModel):
    telephone: str
    type: str = "register"

class RegisterRequest(BaseModel):
    telephone: str
    code: str
    username: str
    password: str
    confirmPassword: str

class ForgotPasswordRequest(BaseModel):
    telephone: str
    code: str
    newPassword: str
    confirmPassword: str

def is_strong_password(password: str) -> bool:
    if len(password) < 6: return False
    return True

def hash_password(password: str) -> str:
    from werkzeug.security import generate_password_hash
    return generate_password_hash(password)

@router.post("/api/send-sms-code")
async def send_sms_code(data: SendSmsRequest):
    telephone = data.telephone.strip()
    sms_type = data.type
    
    if not re.match(r'^1[3-9]\d{9}$', telephone):
        return {"success": False, "error": "请输入正确的手机号"}

    code = ''.join([str(random.randint(0, 9)) for _ in range(6)])

    try:
        async with httpx.AsyncClient() as client:
            # 外部接口 getChannels 报错提示缺少参数，且该步骤通常仅为获取类型。
            # 直接使用 sms 作为默认值以确保核心发送逻辑能够运行。
            channel_encode = "sms"

            payload = {
                "title": "验证码",
                "content": f"【智能体后台管理系统】您的验证码是：{code}，5分钟内有效。",
                "channels": channel_encode,
                "smsReceiver": telephone,
                "cron": "0"
            }
            form_data = {
                "msgData": json.dumps(payload, ensure_ascii=False)
            }
            
            resp_send = await client.post(SEND_MESSAGE_API_URL, data=form_data, headers=sms_headers, timeout=60)
            result = resp_send.json()

            # 兼容处理：有些接口返回 code=200，有些直接返回带 message 的结构
            if result.get("code") == 200 or result.get("message") == "成功" or result.get("code") == 0:
                sms_code_store[f"{sms_type}:{telephone}"] = {
                    "code": code,
                    "expire_time": datetime.now() + timedelta(minutes=5)
                }
                return {"success": True, "message": "验证码已发送"}
            else:
                return {"success": False, "error": result.get("message") or result.get("msg") or "验证码发送失败"}
    except Exception as e:
        print(f"短信发送异常: {e}")
        return {"success": False, "error": f"短信网关异常: {str(e)}"}

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

    if not re.match(r'^1[3-9]\d{9}$', telephone):
        return {"success": False, "error": "请输入正确的手机号"}

    if len(username) < 3:
        return {"success": False, "error": "用户名至少3位"}

    if not is_strong_password(password):
        return {"success": False, "error": "密码强度不符合要求"}

    if password != confirm_password:
        return {"success": False, "error": "两次输入的密码不一致"}

    key = f"register:{telephone}"
    stored = sms_code_store.get(key)
    if not stored or stored["code"] != code:
        return {"success": False, "error": "验证码错误"}
    if datetime.now() > stored["expire_time"]:
        del sms_code_store[key]
        return {"success": False, "error": "验证码已过期"}
    del sms_code_store[key]

    stmt = select(User).where(User.username == username)
    result = await session.execute(stmt)
    existing = result.scalars().first()
    if existing:
        return {"success": False, "error": "用户名已存在"}

    user_id = shortuuid.uuid()[:16]

    new_user = User(
        user_id=user_id,
        username=username,
        password=hash_password(password),
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

@router.post("/api/forgot-password")
async def forgot_password(data: ForgotPasswordRequest, session: AsyncSession = Depends(get_async_session)):
    telephone = data.telephone.strip()
    code = data.code.strip()
    new_password = data.newPassword
    confirm_password = data.confirmPassword

    if not is_strong_password(new_password):
        return {"success": False, "error": "密码强度不符合要求"}

    if new_password != confirm_password:
        return {"success": False, "error": "两次输入的密码不一致"}

    key = f"forgot:{telephone}"
    stored = sms_code_store.get(key)
    if not stored or stored["code"] != code:
        return {"success": False, "error": "验证码错误"}
    if datetime.now() > stored["expire_time"]:
        del sms_code_store[key]
        return {"success": False, "error": "验证码已过期"}
    del sms_code_store[key]

    stmt = select(User).where(User.username == telephone)
    result = await session.execute(stmt)
    user = result.scalars().first()
    if not user:
        return {"success": False, "error": "用户不存在"}
        
    user.password = hash_password(new_password)
    await session.commit()
    
    return {"success": True, "message": "密码重置成功"}

