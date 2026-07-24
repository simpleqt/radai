"""OCR 提供者管理 - 工厂模式

管理多个 OCR 服务提供者，支持动态注册、创建和切换。
使用单例模式确保全局唯一实例。
"""

import logging
import threading
from typing import Any, TypedDict

from .base_ocr import BaseOCR, OCRConfig, OCRResult
from .mineru_ocr import MinerUOCR

logger = logging.getLogger(__name__)


class ProviderInfo(TypedDict):
    cls: type[BaseOCR]
    config: OCRConfig | None
    kwargs: dict[str, Any]


class OCRProvider:
    """OCR 提供者工厂类

    负责管理所有 OCR 提供者的注册、创建和生命周期管理。
    支持动态注册新的 OCR 提供者类型，并统一管理 OCR 实例。

    Attributes:
        _providers: 已注册的提供者类型字典
        _instances: 已创建的提供者实例字典
        _default_provider: 默认提供者名称
        _lock: 线程锁，保证线程安全
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """初始化 OCR 提供者工厂"""
        if self._initialized:
            return

        self._providers: dict[str, ProviderInfo] = {}
        self._instances: dict[str, BaseOCR] = {}
        self._default_provider: str | None = None
        self._lock = threading.Lock()
        self._initialized = True

        self._register_builtin_providers()
        logger.info("OCR Provider factory initialized")

    def _register_builtin_providers(self):
        """注册内置提供者"""
        from backend.config.settings import settings

        self.register_provider("mineru", MinerUOCR, api_url=settings.mineru_api_url)
        self._default_provider = "mineru"
        logger.info(
            f"Registered built-in providers: {', '.join(self.list_providers())}"
        )

    def register_provider(
        self,
        provider_name: str,
        provider_class: type[BaseOCR],
        default_config: OCRConfig | None = None,
        **kwargs: Any,
    ) -> None:
        """注册 OCR 提供者类型

        Args:
            provider_name: 提供者名称
            provider_class: 提供者类（必须继承自 BaseOCR）
            default_config: 默认配置
            **kwargs: 其他默认参数
        """
        if not issubclass(provider_class, BaseOCR):
            raise TypeError(
                f"Provider class must inherit from BaseOCR: {provider_class}"
            )

        with self._lock:
            self._providers[provider_name] = ProviderInfo(
                cls=provider_class, config=default_config, kwargs=dict(kwargs)
            )
            logger.info(f"Registered OCR provider: {provider_name}")

    def unregister_provider(self, provider_name: str) -> bool:
        """注销 OCR 提供者

        Args:
            provider_name: 提供者名称

        Returns:
            是否成功注销
        """
        instance_to_close = None
        with self._lock:
            if provider_name not in self._providers:
                return False

            instance_to_close = self._instances.pop(provider_name, None)
            del self._providers[provider_name]

            if self._default_provider == provider_name:
                self._default_provider = None

        if instance_to_close is not None and hasattr(instance_to_close, "close"):
            try:
                import asyncio

                loop = asyncio.new_event_loop()
                loop.run_until_complete(instance_to_close.close())
                loop.close()
            except Exception as e:
                logger.warning(f"Failed to close provider instance: {e}")

        logger.info(f"Unregistered OCR provider: {provider_name}")
        return True

    def create_provider(
        self, provider_name: str, config: OCRConfig | None = None, **kwargs: Any
    ) -> BaseOCR:
        """创建 OCR 提供者实例

        Args:
            provider_name: 提供者名称
            config: OCR 配置（覆盖默认配置）
            **kwargs: 其他参数（覆盖默认参数）

        Returns:
            OCR 提供者实例
        """
        if provider_name not in self._providers:
            raise ValueError(f"Provider not registered: {provider_name}")

        provider_info = self._providers[provider_name]
        provider_class = provider_info["cls"]

        merged_kwargs = provider_info["kwargs"].copy()
        merged_kwargs.update(kwargs)

        if config is None:
            config = provider_info["config"] or OCRConfig()

        instance = provider_class(config=config, **merged_kwargs)

        with self._lock:
            self._instances[provider_name] = instance

        logger.info(f"Created OCR provider instance: {provider_name}")
        return instance

    def get_provider(self, provider_name: str | None = None) -> BaseOCR:
        """获取已创建的 OCR 提供者实例

        Args:
            provider_name: 提供者名称，None 表示使用默认提供者

        Returns:
            OCR 提供者实例
        """
        if provider_name is None:
            provider_name = self._default_provider

        if provider_name is None:
            raise ValueError("No default provider set")

        if provider_name not in self._instances:
            return self.create_provider(provider_name)

        return self._instances[provider_name]

    def set_default_provider(self, provider_name: str) -> None:
        """设置默认提供者

        Args:
            provider_name: 提供者名称
        """
        if provider_name not in self._providers:
            raise ValueError(f"Provider not registered: {provider_name}")

        with self._lock:
            self._default_provider = provider_name
            logger.info(f"Set default OCR provider: {provider_name}")

    def get_default_provider(self) -> str | None:
        """获取默认提供者名称

        Returns:
            默认提供者名称
        """
        return self._default_provider

    def list_providers(self) -> list[str]:
        """列出所有已注册的提供者

        Returns:
            提供者名称列表
        """
        return list(self._providers.keys())

    def list_instances(self) -> list[str]:
        """列出所有已创建的实例

        Returns:
            实例名称列表
        """
        return list(self._instances.keys())

    def remove_instance(self, provider_name: str) -> bool:
        """移除提供者实例

        Args:
            provider_name: 提供者名称

        Returns:
            是否成功移除
        """
        with self._lock:
            instance = self._instances.pop(provider_name, None)

        if instance is None:
            return False

        if hasattr(instance, "close"):
            try:
                import asyncio

                loop = asyncio.new_event_loop()
                loop.run_until_complete(instance.close())
                loop.close()
            except Exception as e:
                logger.warning(f"Failed to close provider instance: {e}")

        logger.info(f"Removed OCR provider instance: {provider_name}")
        return True

    def clear_instances(self) -> None:
        """清除所有实例"""
        with self._lock:
            provider_names = list(self._instances.keys())

        for provider_name in provider_names:
            self.remove_instance(provider_name)

        logger.info("Cleared all OCR provider instances")

    async def close(self) -> None:
        """关闭所有提供者实例并清理资源"""
        with self._lock:
            instances = list(self._instances.items())
            self._instances.clear()

        for provider_name, instance in instances:
            if hasattr(instance, "close"):
                try:
                    await instance.close()
                except Exception as e:
                    logger.warning(
                        f"Failed to close provider instance {provider_name}: {e}"
                    )

        logger.info("Closed all OCR provider instances")

    def __del__(self):
        """析构函数，确保资源清理"""
        try:
            import sys

            if sys.meta_path is None or getattr(sys, "is_finalizing", lambda: False)():
                return
            import asyncio

            loop = asyncio.new_event_loop()
            loop.run_until_complete(self.close())
            loop.close()
        except Exception:
            return

    def process_file(
        self,
        file_path: str,
        provider_name: str | None = None,
        config: OCRConfig | None = None,
        include_fields: list[str] | None = None,
        upload_images: bool = False,
        **kwargs: Any,
    ) -> OCRResult:
        """使用指定提供者处理文件

        Args:
            file_path: 文件路径
            provider_name: 提供者名称，None 表示使用默认提供者
            config: OCR 配置
            include_fields: 需要返回的字段列表
            upload_images: 是否上传图片到对象存储
            **kwargs: 其他参数

        Returns:
            OCR 结果对象
        """
        provider = self.get_provider(provider_name)
        return provider.process_file(
            file_path,
            config=config,
            include_fields=include_fields,
            upload_images=upload_images,
            **kwargs,
        )

    async def process_file_async(
        self,
        file_path: str,
        provider_name: str | None = None,
        config: OCRConfig | None = None,
        include_fields: list[str] | None = None,
        upload_images: bool = False,
        **kwargs: Any,
    ) -> OCRResult:
        """异步使用指定提供者处理文件

        Args:
            file_path: 文件路径
            provider_name: 提供者名称，None 表示使用默认提供者
            config: OCR 配置
            include_fields: 需要返回的字段列表
            upload_images: 是否上传图片到对象存储
            **kwargs: 其他参数

        Returns:
            OCR 结果对象
        """
        provider = self.get_provider(provider_name)
        return await provider.process_file_async(
            file_path,
            config=config,
            include_fields=include_fields,
            upload_images=upload_images,
            **kwargs,
        )


_global_ocr_provider: OCRProvider | None = None
_global_ocr_provider_lock = threading.Lock()


def get_ocr_provider() -> OCRProvider:
    """获取全局 OCR 提供者工厂实例

    Returns:
        OCR 提供者工厂单例
    """
    global _global_ocr_provider
    if _global_ocr_provider is None:
        with _global_ocr_provider_lock:
            if _global_ocr_provider is None:
                _global_ocr_provider = OCRProvider()
    return _global_ocr_provider


def register_provider(
    provider_name: str,
    provider_class: type[BaseOCR],
    default_config: OCRConfig | None = None,
    **kwargs: Any,
) -> None:
    """注册 OCR 提供者（便捷函数）

    Args:
        provider_name: 提供者名称
        provider_class: 提供者类
        default_config: 默认配置
        **kwargs: 其他默认参数
    """
    get_ocr_provider().register_provider(
        provider_name, provider_class, default_config, **kwargs
    )


def create_provider(
    provider_name: str, config: OCRConfig | None = None, **kwargs: Any
) -> BaseOCR:
    """创建 OCR 提供者实例（便捷函数）

    Args:
        provider_name: 提供者名称
        config: OCR 配置
        **kwargs: 其他参数

    Returns:
        OCR 提供者实例
    """
    return get_ocr_provider().create_provider(provider_name, config, **kwargs)


def get_provider(provider_name: str | None = None) -> BaseOCR:
    """获取 OCR 提供者实例（便捷函数）

    Args:
        provider_name: 提供者名称，None 表示使用默认提供者

    Returns:
        OCR 提供者实例
    """
    return get_ocr_provider().get_provider(provider_name)


def process_file(
    file_path: str,
    provider_name: str | None = None,
    config: OCRConfig | None = None,
    include_fields: list[str] | None = None,
    upload_images: bool = False,
    **kwargs: Any,
) -> OCRResult:
    """处理文件（便捷函数）

    Args:
        file_path: 文件路径
        provider_name: 提供者名称，None 表示使用默认提供者
        config: OCR 配置
        include_fields: 需要返回的字段列表
        upload_images: 是否上传图片到对象存储
        **kwargs: 其他参数

    Returns:
        OCR 结果对象
    """
    return get_ocr_provider().process_file(
        file_path, provider_name, config, include_fields, upload_images, **kwargs
    )
