"""OCR 服务抽象基类

定义 OCR 服务的统一接口，支持文档解析、状态查询、结果获取等功能。
所有 OCR 提供者都需要继承此类并实现抽象方法。
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class OCRStatus(str, Enum):
    """OCR 任务状态枚举"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass
class OCRConfig:
    """OCR 配置类

    Attributes:
        backend: 处理后端（pipeline/vlm-transformers/vlm-vllm-engine）
        lang: 语言（ch/en/korean/japan等）
        method: 解析方法（auto/txt/ocr）
        formula_enable: 是否启用公式识别
        table_enable: 是否启用表格识别
        priority: 优先级，数字越大越优先
        timeout: 超时时间（秒）
        upload_images: 是否上传图片到对象存储
        include_fields: 需要返回的字段列表
    """

    backend: str = "pipeline"
    lang: str = "ch"
    method: str = "auto"
    formula_enable: bool = True
    table_enable: bool = True
    priority: int = 0
    timeout: int = 600
    upload_images: bool = False
    include_fields: list[str] = field(
        default_factory=lambda: ["md", "content_list", "images"]
    )
    extra_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class OCRResult:
    """OCR 解析结果

    Attributes:
        task_id: 任务 ID
        status: 任务状态
        file_name: 原始文件名
        total_pages: 总页数
        pages: 页面列表
        markdown: Markdown 内容
        md_url: Markdown 文件 URL
        content_list: 结构化内容列表（JSON）
        middle_json: 中间处理结果（JSON）
        model_output: 模型原始输出（JSON）
        images: 图片列表
        layout_pdf: Layout PDF 信息
        span_pdf: Span PDF 信息
        origin_pdf: Origin PDF 信息
        error_message: 错误信息
        created_at: 创建时间
        completed_at: 完成时间
        metadata: 其他元数据
    """

    task_id: str
    status: OCRStatus
    file_name: str
    total_pages: int = 0
    pages: list[Any] = field(default_factory=list)
    markdown: str | None = None
    md_url: str | None = None
    content_list: list[Any] | None = None
    middle_json: dict[str, Any] | None = None
    model_output: dict[str, Any] | None = None
    images: dict[str, Any] | None = None
    layout_pdf: dict[str, Any] | None = None
    span_pdf: dict[str, Any] | None = None
    origin_pdf: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_success(self) -> bool:
        """检查任务是否成功完成"""
        return self.status == OCRStatus.COMPLETED

    def get_markdown(self) -> str | None:
        """获取 Markdown 内容"""
        return self.markdown

    def get_content_list(self) -> list[Any] | None:
        """获取结构化内容列表"""
        return self.content_list

    def get_images(self) -> dict[str, Any] | None:
        """获取图片列表"""
        return self.images


class BaseOCR(ABC):
    """OCR 服务抽象基类

    定义了 OCR 服务的统一接口，包括：
    - 任务提交
    - 状态查询
    - 结果获取
    - 异步支持

    所有具体的 OCR 提供者（如 MinerU、Tesseract 等）都需要继承此类。
    """

    def __init__(self, config: OCRConfig | None = None, **kwargs: Any):
        """初始化 OCR 服务

        Args:
            config: OCR 配置
            **kwargs: 其他配置参数
        """
        self.config = config or OCRConfig()
        self._provider_name = kwargs.get("provider_name", "base")
        self._loop = None
        logger.info(f"Initialized OCR provider: {self._provider_name}")

    def _get_loop(self):
        """获取或创建事件循环"""
        import asyncio

        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        return self._loop

    @property
    def provider_name(self) -> str:
        """获取提供者名称"""
        return self._provider_name

    @abstractmethod
    async def submit_task_async(
        self, file_path: str, config: OCRConfig | None = None, **kwargs: Any
    ) -> str:
        """异步提交 OCR 任务

        Args:
            file_path: 文件路径
            config: OCR 配置（覆盖默认配置）
            **kwargs: 其他参数

        Returns:
            任务 ID
        """
        pass

    @abstractmethod
    def submit_task(
        self, file_path: str, config: OCRConfig | None = None, **kwargs: Any
    ) -> str:
        """同步提交 OCR 任务

        Args:
            file_path: 文件路径
            config: OCR 配置（覆盖默认配置）
            **kwargs: 其他参数

        Returns:
            任务 ID
        """
        pass

    @abstractmethod
    async def get_task_status_async(self, task_id: str, **kwargs: Any) -> OCRResult:
        """异步查询任务状态

        Args:
            task_id: 任务 ID
            **kwargs: 其他参数

        Returns:
            OCR 结果对象
        """
        pass

    @abstractmethod
    def get_task_status(self, task_id: str, **kwargs: Any) -> OCRResult:
        """同步查询任务状态

        Args:
            task_id: 任务 ID
            **kwargs: 其他参数

        Returns:
            OCR 结果对象
        """
        pass

    @abstractmethod
    async def get_task_data_async(
        self,
        task_id: str,
        include_fields: list[str] | None = None,
        upload_images: bool = False,
        include_metadata: bool = True,
        **kwargs: Any,
    ) -> OCRResult:
        """异步获取任务数据

        Args:
            task_id: 任务 ID
            include_fields: 需要返回的字段列表（如 ['md', 'content_list', 'images']）
            upload_images: 是否上传图片到对象存储
            include_metadata: 是否包含文件元数据
            **kwargs: 其他参数

        Returns:
            OCR 结果对象
        """
        pass

    @abstractmethod
    def get_task_data(
        self,
        task_id: str,
        include_fields: list[str] | None = None,
        upload_images: bool = False,
        include_metadata: bool = True,
        **kwargs: Any,
    ) -> OCRResult:
        """同步获取任务数据

        Args:
            task_id: 任务 ID
            include_fields: 需要返回的字段列表（如 ['md', 'content_list', 'images']）
            upload_images: 是否上传图片到对象存储
            include_metadata: 是否包含文件元数据
            **kwargs: 其他参数

        Returns:
            OCR 结果对象
        """
        pass

    @abstractmethod
    async def wait_for_completion_async(
        self,
        task_id: str,
        poll_interval: float = 2.0,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> OCRResult:
        """异步等待任务完成

        Args:
            task_id: 任务 ID
            poll_interval: 轮询间隔（秒）
            timeout: 超时时间（秒），None 表示不超时
            **kwargs: 其他参数

        Returns:
            OCR 结果对象
        """
        pass

    @abstractmethod
    def wait_for_completion(
        self,
        task_id: str,
        poll_interval: float = 2.0,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> OCRResult:
        """同步等待任务完成

        Args:
            task_id: 任务 ID
            poll_interval: 轮询间隔（秒）
            timeout: 超时时间（秒），None 表示不超时
            **kwargs: 其他参数

        Returns:
            OCR 结果对象
        """
        pass

    async def process_file_async(
        self,
        file_path: str,
        config: OCRConfig | None = None,
        include_fields: list[str] | None = None,
        upload_images: bool = False,
        **kwargs: Any,
    ) -> OCRResult:
        """异步处理文件（提交 + 等待完成 + 获取结果）

        Args:
            file_path: 文件路径
            config: OCR 配置
            include_fields: 需要返回的字段列表
            upload_images: 是否上传图片到对象存储
            **kwargs: 其他参数

        Returns:
            OCR 结果对象
        """
        task_id = await self.submit_task_async(file_path, config, **kwargs)
        result = await self.wait_for_completion_async(
            task_id, timeout=config.timeout if config else self.config.timeout, **kwargs
        )

        if result.is_success():
            result = await self.get_task_data_async(
                task_id,
                include_fields=include_fields,
                upload_images=upload_images,
                **kwargs,
            )

        return result

    def process_file(
        self,
        file_path: str,
        config: OCRConfig | None = None,
        include_fields: list[str] | None = None,
        upload_images: bool = False,
        **kwargs: Any,
    ) -> OCRResult:
        """同步处理文件（已废弃，请使用 process_file_async）

        Args:
            file_path: 文件路径
            config: OCR 配置
            include_fields: 需要返回的字段列表
            upload_images: 是否上传图片到对象存储
            **kwargs: 其他参数

        Returns:
            OCR 结果对象
        """
        import warnings
        warnings.warn(
            "process_file is deprecated, use process_file_async instead",
            DeprecationWarning,
            stacklevel=2,
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.process_file_async(
                    file_path, config, include_fields, upload_images, **kwargs
                )
            )
        finally:
            loop.close()

    def get_available_fields(self) -> list[str]:
        """获取可用的字段列表

        Returns:
            可用的字段列表
        """
        return [
            "md",
            "content_list",
            "middle_json",
            "model_output",
            "images",
            "layout_pdf",
            "span_pdf",
            "origin_pdf",
        ]
