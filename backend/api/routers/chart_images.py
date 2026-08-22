"""图表服务路由

可视化工具生成的图表通过此端点对外提供访问：

- 默认返回 PNG（导出资产：PDF 打印 / DOCX / HTML PPT 按 URL 取图）
- ``?format=option`` 返回同 chart_id 的 ECharts 交互 payload（JSON），
  供聊天前端渲染交互式图表

设计原则：
- 图表存储到 MinIO（user-files bucket），路径：charts/{user_id}/{chart_id}.png|.json
- 对外暴露 API Server 代理 URL，不直接暴露 MinIO 地址
- GET 端点使用短期 HMAC 签名 URL（签名载荷与 format 无关），便于 Markdown/<img> 直接嵌入
- 支持本地降级存储（MinIO 不可用时存本地，通过相同 URL 服务）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from backend.services.chart_image_store import (
    fetch_chart,
    fetch_chart_option,
    verify_chart_signature,
)

router = APIRouter()


# ── API 端点 ──────────────────────────────────────────────────────────────


@router.get("/charts/{user_id}/{chart_id}", summary="获取可视化图表")
async def get_chart_image(
    user_id: str,
    chart_id: str,
    exp: int | None = None,
    sig: str | None = None,
    format: str | None = None,
):
    """代理访问可视化图表。

    默认返回 PNG（支持 <img> 直接嵌入与文件导出）；``format=option``
    返回 ECharts 交互 payload JSON。两种格式共用同一签名。
    """
    # 安全校验：只允许合法字符，防止路径穿越
    if not _safe_id(user_id) or not _safe_id(chart_id):
        raise HTTPException(status_code=400, detail="非法图表 ID")
    if not verify_chart_signature(user_id, chart_id, exp, sig):
        raise HTTPException(status_code=403, detail="图表访问签名无效或已过期")

    if (format or "").strip().lower() == "option":
        payload = await fetch_chart_option(user_id, chart_id)
        if payload:
            return Response(
                content=payload,
                media_type="application/json; charset=utf-8",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        raise HTTPException(status_code=404, detail="图表交互数据不存在")

    png = await fetch_chart(user_id, chart_id)
    if png:
        return Response(
            content=png,
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=86400",
                "Content-Disposition": f'inline; filename="{chart_id}.png"',
            },
        )

    raise HTTPException(status_code=404, detail="图表不存在或已过期")


def _safe_id(s: str) -> bool:
    """只允许字母、数字、下划线、连字符"""
    import re

    return bool(s) and bool(re.match(r"^[\w\-]{1,128}$", s))
