"""路由模块。

保持包初始化轻量，避免导入单个子路由时触发其它重型路由的依赖链。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "document_processing_router",
]


def __getattr__(name: str) -> Any:
    if name == "document_processing_router":
        from backend.api.routers.document_processing import router

        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
