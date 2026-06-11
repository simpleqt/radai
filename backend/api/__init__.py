"""API 层。

包初始化必须保持轻量，避免导入 ``backend.api.routers.*`` 时连带初始化
``http_server``，否则自定义 v2 路由聚合导入会形成循环依赖。
"""

from __future__ import annotations

from typing import Any

__all__ = ["app", "create_app"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from backend.api import http_server

        return getattr(http_server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
