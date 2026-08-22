"""URL 签名密钥的单一来源。

六处 HMAC 签名（qb-image / kb-image / chart / my-files 图片 / chat-files
图片 / 访客设备 scope）此前全部以 ``minio_secret_key``（或默认为空的
``internal_api_secret``）为密钥——签名密钥与存储凭据未分层，存储凭据
的暴露面（运维侧/备份/跨环境共享）即签名伪造面。

设置 ``URL_SIGNING_SECRET``（建议 ≥32 字节随机值）后全部签名点切换到
独立密钥；未设置时回退既有链路（已签发的 URL 在 TTL 内继续有效），
属于零迁移的可选强化。轮换密钥会使在途签名 URL 失效（最长 TTL 后
自愈），应在维护窗口执行。
"""

from __future__ import annotations

import os

from backend.config import settings


def url_signing_secret() -> str:
    """返回 URL 签名密钥（URL_SIGNING_SECRET 优先，回退既有链路）。"""
    explicit = os.getenv("URL_SIGNING_SECRET", "").strip()
    if explicit:
        return explicit
    return (
        getattr(settings, "internal_api_secret", "")
        or settings.minio_secret_key
        or ""
    )
