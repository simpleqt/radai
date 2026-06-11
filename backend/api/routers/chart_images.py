"""图表图片服务路由

可视化工具生成的 PNG 图表通过此端点对外提供访问。

设计原则：
- 图表存储到 MinIO（user-files bucket），路径：charts/{user_id}/{chart_id}.png
- 对外暴露 API Server 代理 URL，不直接暴露 MinIO 地址
- GET 端点无需 auth（UUID 本身作为 capability token，不含敏感业务数据）
- 支持本地降级存储（MinIO 不可用时存本地，通过相同 URL 服务）
"""

from __future__ import annotations

import io
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

from backend.config.loggers import get_logger
from backend.config.settings import settings

logger = get_logger(__name__)
router = APIRouter()

# 本地降级存储目录
_LOCAL_CHARTS_DIR = Path(__file__).resolve().parents[2] / "data" / "charts"
_LOCAL_CHARTS_DIR.mkdir(parents=True, exist_ok=True)


# ── 内部工具函数（供 excel_chart_generator 调用）────────────────────────

async def store_chart(
    png_bytes: bytes,
    *,
    user_id: str | None = None,
    chart_id: str | None = None,
) -> str:
    """保存图表 PNG 并返回可访问的 API URL。

    存储策略（按优先级）：
    1. MinIO  → charts/{user_id or "public"}/{chart_id}.png
    2. 本地降级 → backend/data/charts/{user_id}_{chart_id}.png

    返回 URL 统一格式：/api/v1/charts/{user_id}/{chart_id}
    （均由 API Server 代理，前端不需要直接访问 MinIO）
    """
    cid = chart_id or uuid.uuid4().hex
    uid = user_id or "public"

    # 先尝试 MinIO
    minio_ok = await _save_to_minio(png_bytes, uid, cid)
    if minio_ok:
        logger.info("[ChartImages] 图表已存储到 MinIO: user=%s, chart=%s", uid, cid)
    else:
        # 降级：保存到本地
        _save_locally(png_bytes, uid, cid)
        logger.info("[ChartImages] 图表已存储到本地（MinIO 降级）: user=%s, chart=%s", uid, cid)

    return f"/api/v1/charts/{uid}/{cid}"


async def _save_to_minio(png_bytes: bytes, user_id: str, chart_id: str) -> bool:
    try:
        from backend.storage import get_user_minio_bucket_name, upload_bytes_to_minio
        import asyncio

        object_name = f"charts/{user_id}/{chart_id}.png"
        bucket = get_user_minio_bucket_name()
        # 超时 5s，避免 MinIO 不可用时长时间阻塞
        result = await asyncio.wait_for(
            upload_bytes_to_minio(png_bytes, object_name, content_type="image/png", bucket_name=bucket),
            timeout=5.0,
        )
        return bool(result)
    except Exception as e:
        logger.warning("[ChartImages] MinIO 上传失败（降级本地）: %s", type(e).__name__)
        return False


def _save_locally(png_bytes: bytes, user_id: str, chart_id: str) -> None:
    user_dir = _LOCAL_CHARTS_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    dest = user_dir / f"{chart_id}.png"
    dest.write_bytes(png_bytes)


# ── API 端点 ──────────────────────────────────────────────────────────────

@router.get("/charts/{user_id}/{chart_id}", summary="获取可视化图表图片")
async def get_chart_image(user_id: str, chart_id: str):
    """代理访问可视化图表 PNG。

    无需 Token（UUID 作为 capability token），支持 <img> 标签直接嵌入。
    先尝试从 MinIO 取图片，MinIO 不可用则回落本地缓存。
    """
    # 安全校验：只允许合法字符，防止路径穿越
    if not _safe_id(user_id) or not _safe_id(chart_id):
        raise HTTPException(status_code=400, detail="非法图表 ID")

    # 尝试 MinIO
    png = await _fetch_from_minio(user_id, chart_id)
    if png:
        return Response(content=png, media_type="image/png", headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Disposition": f'inline; filename="{chart_id}.png"',
        })

    # 本地降级
    local_path = _LOCAL_CHARTS_DIR / user_id / f"{chart_id}.png"
    if local_path.exists():
        return Response(content=local_path.read_bytes(), media_type="image/png", headers={
            "Cache-Control": "public, max-age=86400",
        })

    raise HTTPException(status_code=404, detail="图表不存在或已过期")


def _safe_id(s: str) -> bool:
    """只允许字母、数字、下划线、连字符"""
    import re
    return bool(s) and bool(re.match(r"^[\w\-]{1,128}$", s))


async def _fetch_from_minio(user_id: str, chart_id: str) -> bytes | None:
    try:
        from backend.storage import _get_minio_client, get_user_minio_bucket_name

        client = _get_minio_client()
        bucket = get_user_minio_bucket_name()
        object_name = f"charts/{user_id}/{chart_id}.png"
        response = client.get_object(bucket, object_name)
        try:
            data = response.read()
        finally:
            response.close()
            response.release_conn()
        return data
    except Exception:
        return None
