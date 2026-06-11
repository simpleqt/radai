"""服务层模块。

避免在包初始化阶段预加载依赖存储层的服务，防止
``backend.storage -> backend.services.minio_paths -> backend.services``
触发循环导入。
"""

from backend.services.directory_service import DirectoryService

__all__ = [
    "DirectoryService",
    "StartupTaskProcessor",
    "get_startup_task_processor",
]


def __getattr__(name: str):
    if name in {"StartupTaskProcessor", "get_startup_task_processor"}:
        from backend.services.startup_task_processor import (
            StartupTaskProcessor,
            get_startup_task_processor,
        )

        return {
            "StartupTaskProcessor": StartupTaskProcessor,
            "get_startup_task_processor": get_startup_task_processor,
        }[name]
    raise AttributeError(name)
