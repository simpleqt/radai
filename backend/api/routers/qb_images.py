"""题库图片签名代理路由。

对外只暴露本代理 URL（``/api/v2/files/qb-image?name=...&exp=...&sig=...``）：
HMAC 签名与 kb-image/chart 同源纪律（密钥为空 fail-closed），object_name 白名单
校验防路径穿越，不暴露 MinIO 凭据或裸对象路径。签名是短期展示态，稳定
object_name 才是持久身份。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from backend.application.question_bank.images import (
    content_type_for,
    fetch_qb_image,
    is_valid_object_name,
    verify_qb_image_signature,
)

router = APIRouter(prefix="/files/qb-image", tags=["题库图片"])


@router.get("", summary="代理访问题库图片")
async def get_qb_image(
    name: str = Query(..., max_length=512),
    exp: int | None = Query(default=None),
    sig: str | None = Query(default=None, max_length=128),
):
    object_name = str(name or "").strip()
    if not is_valid_object_name(object_name):
        raise HTTPException(status_code=400, detail="非法图片标识")
    if not verify_qb_image_signature(object_name, exp, sig):
        raise HTTPException(status_code=403, detail="图片访问签名无效或已过期")
    data = await fetch_qb_image(object_name)
    if not data:
        raise HTTPException(status_code=404, detail="图片不存在")
    return Response(
        content=data,
        media_type=content_type_for(object_name),
        headers={
            "Cache-Control": "private, max-age=3600",
            # 教师上传的图片仅靠扩展名白名单决定 content-type，防内容嗅探
            # （与 minio-artifact 下载路由同款纪律）。
            "X-Content-Type-Options": "nosniff",
        },
    )
