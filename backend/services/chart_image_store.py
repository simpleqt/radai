"""Storage boundary for generated chart images."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from backend.config.loggers import get_logger
from backend.config.settings import settings

logger = get_logger(__name__)

LOCAL_CHARTS_DIR = Path(__file__).resolve().parents[1] / "data" / "charts"
LOCAL_CHARTS_DIR.mkdir(parents=True, exist_ok=True)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CHART_URL_TTL_SECONDS = int(os.getenv("CHART_URL_TTL_SECONDS", str(7 * 24 * 60 * 60)))


def _safe_id(value: str | None, fallback: str) -> str:
    cleaned = str(value or fallback).strip()
    return cleaned if _SAFE_ID_RE.fullmatch(cleaned) else fallback


def _signature_secret() -> bytes:
    secret = settings.internal_api_secret or settings.minio_secret_key or ""
    return secret.encode("utf-8")


def build_chart_signature(user_id: str, chart_id: str, expires_at: int) -> str:
    payload = f"{user_id}\n{chart_id}\n{int(expires_at)}".encode("utf-8")
    return hmac.new(_signature_secret(), payload, hashlib.sha256).hexdigest()


def verify_chart_signature(
    user_id: str,
    chart_id: str,
    expires_at: int | None,
    signature: str | None,
) -> bool:
    if not expires_at or not signature or int(expires_at) < int(time.time()):
        return False
    if not _signature_secret():
        return False
    expected = build_chart_signature(user_id, chart_id, int(expires_at))
    return hmac.compare_digest(expected, signature)


def build_chart_url(user_id: str, chart_id: str) -> str:
    expires_at = int(time.time()) + max(CHART_URL_TTL_SECONDS, 60)
    query = urlencode(
        {
            "exp": str(expires_at),
            "sig": build_chart_signature(user_id, chart_id, expires_at),
        }
    )
    return f"/api/v1/charts/{user_id}/{chart_id}?{query}"


async def store_chart(
    png_bytes: bytes,
    *,
    user_id: str | None = None,
    chart_id: str | None = None,
) -> str:
    """Persist a chart and return its stable API proxy URL."""
    cid = _safe_id(chart_id, uuid.uuid4().hex)
    uid = _safe_id(user_id, "public")
    if await _save_to_minio(png_bytes, uid, cid):
        logger.info("[ChartImages] 图表已存储到 MinIO: user=%s, chart=%s", uid, cid)
    else:
        await asyncio.to_thread(_save_locally, png_bytes, uid, cid)
        logger.info(
            "[ChartImages] 图表已存储到本地（MinIO 降级）: user=%s, chart=%s", uid, cid
        )
    return build_chart_url(uid, cid)


async def fetch_chart(user_id: str, chart_id: str) -> bytes | None:
    """Load a chart from object storage, then from the local fallback."""
    user_id = _safe_id(user_id, "")
    chart_id = _safe_id(chart_id, "")
    if not user_id or not chart_id:
        return None
    remote = await _fetch_from_minio(user_id, chart_id)
    if remote:
        return remote
    local_path = (LOCAL_CHARTS_DIR / user_id / f"{chart_id}.png").resolve()
    try:
        local_path.relative_to(LOCAL_CHARTS_DIR.resolve())
    except ValueError:
        return None
    return await asyncio.to_thread(
        lambda: local_path.read_bytes() if local_path.exists() else None
    )


async def _save_to_minio(png_bytes: bytes, user_id: str, chart_id: str) -> bool:
    try:
        from backend.storage import get_user_minio_bucket_name, upload_bytes_to_minio

        result = await asyncio.wait_for(
            upload_bytes_to_minio(
                png_bytes,
                f"charts/{user_id}/{chart_id}.png",
                content_type="image/png",
                bucket_name=get_user_minio_bucket_name(),
            ),
            timeout=5.0,
        )
        return bool(result)
    except Exception as exc:
        logger.warning(
            "[ChartImages] MinIO 上传失败（降级本地）: %s", type(exc).__name__
        )
        return False


def _save_locally(png_bytes: bytes, user_id: str, chart_id: str) -> None:
    user_dir = LOCAL_CHARTS_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / f"{chart_id}.png").write_bytes(png_bytes)


async def _fetch_from_minio(user_id: str, chart_id: str) -> bytes | None:
    try:
        from backend.storage import _get_minio_client, get_user_minio_bucket_name

        def _sync_fetch() -> bytes:
            client = _get_minio_client()
            response = client.get_object(
                get_user_minio_bucket_name(),
                f"charts/{user_id}/{chart_id}.png",
            )
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.wait_for(asyncio.to_thread(_sync_fetch), timeout=5.0)
    except TimeoutError:
        logger.warning(
            "[ChartImages] MinIO 下载超时: user=%s chart=%s",
            user_id,
            chart_id,
        )
        return None
    except Exception as exc:
        logger.warning(
            "[ChartImages] MinIO 下载失败: user=%s chart=%s error=%s",
            user_id,
            chart_id,
            type(exc).__name__,
        )
        return None
