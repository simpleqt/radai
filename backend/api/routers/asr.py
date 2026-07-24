"""语音转写路由（代理 OpenAI 兼容 /v1/audio/transcriptions）。"""

from __future__ import annotations

import asyncio
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from backend.api.routers.auth import get_current_user
from backend.api.upload_utils import UploadTooLargeError, read_upload_with_limit
from backend.config.loggers import get_logger
from backend.core.models.asr_config import load_asr_config_from_db
from backend.db.models import User

logger = get_logger(__name__)

router = APIRouter(prefix="/asr", tags=["语音转写"])

MAX_AUDIO_SIZE = 25 * 1024 * 1024

ALLOWED_AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".m4a", ".ogg", ".webm", ".flac", ".aac", ".mp4",
}


@router.post("/transcribe")
async def transcribe_audio(
    file: UploadFile = File(..., description="音频文件"),
    language: Optional[str] = Form(default=None, description="语言代码，如 zh"),
    model_name: Optional[str] = Form(default=None, description="ASR 模型 name，默认取 DB 默认模型"),
    user: User = Depends(get_current_user),
):
    """上传音频并转写为文本。"""
    config = await asyncio.to_thread(load_asr_config_from_db, name=model_name)
    if not config:
        raise HTTPException(status_code=503, detail="ASR 模型未配置，请在 llm_models 中添加 usage_scope=asr 的记录")

    api_key = (config.get("api_key") or "").strip()
    base_url = (config.get("base_url") or "").strip().rstrip("/")
    if not api_key or not base_url:
        raise HTTPException(status_code=503, detail="ASR 模型 API Key 或 Base URL 未配置")

    try:
        data = await read_upload_with_limit(file, MAX_AUDIO_SIZE)
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail="音频文件过大，上限 25MB") from exc
    if not data:
        raise HTTPException(status_code=400, detail="音频文件为空")

    filename = file.filename or "audio.webm"
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext and ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的音频格式: {ext}")

    lang = (language or config.get("default_language") or "zh").strip()
    model = config["model"]
    url = f"{base_url}/v1/audio/transcriptions"
    timeout = float(config.get("request_timeout") or 120.0)

    try:
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                files={
                    "file": (
                        filename,
                        data,
                        file.content_type or "application/octet-stream",
                    )
                },
                data={"model": model, "language": lang},
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="语音识别服务超时")
    except httpx.RequestError as exc:
        logger.error("[ASR] 请求失败: %s", exc)
        raise HTTPException(status_code=502, detail="无法连接语音识别服务")

    if response.status_code >= 400:
        logger.warning("[ASR] 上游错误 %s: %s", response.status_code, response.text[:500])
        raise HTTPException(status_code=502, detail="语音识别服务返回错误")

    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="语音识别服务返回无效 JSON")

    text = (payload.get("text") or "").strip()
    return {"text": text, "model": model, "language": lang}
