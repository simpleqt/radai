"""文档处理路由模块

该模块包含文档处理相关的所有API路由，按照功能拆分为多个子模块：
- models.py: Pydantic 模型定义
- upload_routes.py: 基本上传接口
- task_routes.py: 任务管理接口
- document_routes.py: 文档信息接口
- chunked_upload_routes.py: 分块上传接口
"""

from fastapi import APIRouter

from backend.api.routers.document_processing.chunked_upload_routes import (
    router as chunked_upload_router,
)
from backend.api.routers.document_processing.document_routes import (
    router as document_router,
)
from backend.api.routers.document_processing.task_routes import router as task_router
from backend.api.routers.document_processing.upload_routes import (
    router as upload_router,
)

router = APIRouter()

router.include_router(upload_router, tags=["上传"])
router.include_router(task_router, tags=["任务"])
router.include_router(document_router, tags=["文档"])
router.include_router(chunked_upload_router, tags=["分块上传"])


__all__ = ["router"]
