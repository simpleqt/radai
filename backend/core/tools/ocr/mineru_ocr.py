"""MinerU OCR 服务实现

封装 MinerU API 调用，提供文档解析、状态查询、结果获取等功能。
基于 MinerU Tianshu API Server 的 RESTful 接口。
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from .base_ocr import BaseOCR, OCRConfig, OCRResult, OCRStatus

logger = logging.getLogger(__name__)


class MinerUOCR(BaseOCR):
    """MinerU OCR 服务实现

    封装 MinerU Tianshu API Server 的调用，支持：
    - 文档上传与解析任务提交
    - 任务状态查询
    - 解析结果获取（支持选择性获取字段）
    - 异步和同步调用
    """

    def __init__(
        self,
        api_url: str = "http://localhost:8009",
        config: OCRConfig | None = None,
        **kwargs: Any,
    ):
        """初始化 MinerU OCR 服务

        Args:
            api_url: MinerU API 服务地址
            config: OCR 配置
            **kwargs: 其他配置参数
        """
        super().__init__(config=config, provider_name="MinerU", **kwargs)
        self.api_url = api_url.rstrip("/")
        self.api_base = f"{self.api_url}/api/v1"
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _parse_status(self, status_str: str) -> OCRStatus:
        """解析状态字符串为枚举"""
        status_map = {
            "pending": OCRStatus.PENDING,
            "processing": OCRStatus.PROCESSING,
            "completed": OCRStatus.COMPLETED,
            "failed": OCRStatus.FAILED,
            "cancelled": OCRStatus.CANCELLED,
            "error": OCRStatus.FAILED,
        }
        return status_map.get(status_str.lower(), OCRStatus.PENDING)

    def _parse_datetime(self, dt_str: str | None) -> datetime | None:
        """解析日期时间字符串"""
        if dt_str:
            try:
                return datetime.fromisoformat(dt_str)
            except (ValueError, TypeError):
                pass
        return None

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
        ocr_config = config or self.config

        file_path_obj = Path(file_path)
        if not await asyncio.to_thread(file_path_obj.exists):
            raise FileNotFoundError(f"File not found: {file_path}")

        session = await self._get_session()

        file_handle = await asyncio.to_thread(open, file_path, "rb")

        data = aiohttp.FormData()
        data.add_field(
            "file",
            file_handle,
            filename=file_path_obj.name,
            content_type="application/octet-stream",
        )
        data.add_field("backend", ocr_config.backend)
        data.add_field("lang", ocr_config.lang)
        data.add_field("method", ocr_config.method)
        data.add_field("formula_enable", str(ocr_config.formula_enable).lower())
        data.add_field("table_enable", str(ocr_config.table_enable).lower())

        for key, value in ocr_config.extra_params.items():
            data.add_field(key, str(value))

        for key, value in kwargs.items():
            if key not in [
                "backend",
                "lang",
                "method",
                "formula_enable",
                "table_enable",
            ]:
                data.add_field(key, str(value))

        try:
            async with session.post(f"{self.api_base}/tasks/submit", data=data) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    task_id = result.get("task_id")
                    logger.info(f"Task submitted successfully: {task_id}")
                    return task_id
                else:
                    error = await resp.text()
                    raise Exception(f"Failed to submit task: {error}")
        finally:
            await asyncio.to_thread(file_handle.close)

    def submit_task(
        self, file_path: str, config: OCRConfig | None = None, **kwargs: Any
    ) -> str:
        """同步提交 OCR 任务（已废弃，请使用 submit_task_async）"""
        import warnings
        warnings.warn(
            "submit_task is deprecated, use submit_task_async instead",
            DeprecationWarning,
            stacklevel=2,
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.submit_task_async(file_path, config, **kwargs)
            )
        finally:
            loop.close()

    async def get_task_status_async(self, task_id: str, **kwargs: Any) -> OCRResult:
        """异步查询任务状态"""
        session = await self._get_session()

        async with session.get(f"{self.api_base}/tasks/{task_id}") as resp:
            if resp.status == 200:
                data = await resp.json()

                result = OCRResult(
                    task_id=task_id,
                    status=self._parse_status(data.get("status", "pending")),
                    file_name=data.get("file_name", ""),
                    error_message=data.get("error_message"),
                    created_at=self._parse_datetime(data.get("created_at")),
                    completed_at=self._parse_datetime(data.get("completed_at")),
                    metadata={
                        "backend": data.get("backend"),
                        "priority": data.get("priority"),
                        "worker_id": data.get("worker_id"),
                        "retry_count": data.get("retry_count"),
                        "result_path": data.get("result_path"),
                    },
                )

                if result.status == OCRStatus.COMPLETED and "data" in data:
                    result_data = data["data"]
                    if "content" in result_data:
                        result.markdown = result_data["content"]

                return result
            else:
                error = await resp.text()
                raise Exception(f"Failed to get task status: {error}")

    def get_task_status(self, task_id: str, **kwargs: Any) -> OCRResult:
        """同步查询任务状态（已废弃，请使用 get_task_status_async）"""
        import warnings
        warnings.warn(
            "get_task_status is deprecated, use get_task_status_async instead",
            DeprecationWarning,
            stacklevel=2,
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.get_task_status_async(task_id, **kwargs))
        finally:
            loop.close()

    async def get_task_data_async(
        self,
        task_id: str,
        include_fields: list[str] | None = None,
        upload_images: bool = False,
        include_metadata: bool = True,
        **kwargs: Any,
    ) -> OCRResult:
        """异步获取任务数据"""
        session = await self._get_session()

        if include_fields is None:
            include_fields = self.config.include_fields

        params = {
            "include_fields": ",".join(include_fields),
            "upload_images": str(upload_images).lower(),
            "include_metadata": str(include_metadata).lower(),
        }

        async with session.get(
            f"{self.api_base}/tasks/{task_id}/data", params=params
        ) as resp:
            if resp.status == 200:
                data = await resp.json()

                result = OCRResult(
                    task_id=task_id,
                    status=self._parse_status(data.get("status", "pending")),
                    file_name=data.get("file_name", ""),
                    created_at=self._parse_datetime(data.get("created_at")),
                    completed_at=self._parse_datetime(data.get("completed_at")),
                )

                if "data" in data:
                    result_data = data["data"]

                    if "md" in result_data:
                        result.markdown = result_data["md"].get("content")
                        result.md_url = result_data["md"].get("url")
                    elif "markdown" in result_data:
                        result.markdown = result_data["markdown"].get("content")
                        result.md_url = result_data["markdown"].get("url")

                    if "content_list" in result_data:
                        result.content_list = result_data["content_list"].get("content")

                    if "middle_json" in result_data:
                        result.middle_json = result_data["middle_json"].get("content")

                    if "model_output" in result_data:
                        result.model_output = result_data["model_output"].get("content")

                    if "images" in result_data:
                        result.images = result_data["images"]

                    if "layout_pdf" in result_data:
                        result.layout_pdf = result_data["layout_pdf"]

                    if "span_pdf" in result_data:
                        result.span_pdf = result_data["span_pdf"]

                    if "origin_pdf" in result_data:
                        result.origin_pdf = result_data["origin_pdf"]

                return result
            else:
                error = await resp.text()
                raise Exception(f"Failed to get task data: {error}")

    def get_task_data(
        self,
        task_id: str,
        include_fields: list[str] | None = None,
        upload_images: bool = False,
        include_metadata: bool = True,
        **kwargs: Any,
    ) -> OCRResult:
        """同步获取任务数据（已废弃，请使用 get_task_data_async）"""
        import warnings
        warnings.warn(
            "get_task_data is deprecated, use get_task_data_async instead",
            DeprecationWarning,
            stacklevel=2,
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.get_task_data_async(
                    task_id, include_fields, upload_images, include_metadata, **kwargs
                )
            )
        finally:
            loop.close()

    async def wait_for_completion_async(
        self,
        task_id: str,
        poll_interval: float = 2.0,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> OCRResult:
        """异步等待任务完成"""
        loop = asyncio.get_event_loop()
        start_time = loop.time()

        while True:
            result = await self.get_task_status_async(task_id, **kwargs)

            if result.status == OCRStatus.COMPLETED:
                logger.info(f"Task {task_id} completed successfully")
                return result
            elif result.status in [OCRStatus.FAILED, OCRStatus.CANCELLED]:
                logger.error(f"Task {task_id} failed with status: {result.status}")
                return result

            if timeout is not None:
                elapsed = loop.time() - start_time
                if elapsed >= timeout:
                    logger.warning(f"Task {task_id} timed out after {timeout} seconds")
                    result.status = OCRStatus.TIMEOUT
                    result.error_message = (
                        result.error_message or f"timeout after {timeout} seconds"
                    )
                    return result

            logger.debug(f"Task {task_id} status: {result.status}, waiting...")
            await asyncio.sleep(poll_interval)

    def wait_for_completion(
        self,
        task_id: str,
        poll_interval: float = 2.0,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> OCRResult:
        """同步等待任务完成（已废弃，请使用 wait_for_completion_async）"""
        import warnings
        warnings.warn(
            "wait_for_completion is deprecated, use wait_for_completion_async instead",
            DeprecationWarning,
            stacklevel=2,
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.wait_for_completion_async(task_id, poll_interval, timeout, **kwargs)
            )
        finally:
            loop.close()

    async def health_check_async(self) -> dict[str, Any]:
        """异步健康检查"""
        session = await self._get_session()

        async with session.get(f"{self.api_base}/health") as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                raise Exception(f"Health check failed: {resp.status}")

    def health_check(self) -> dict[str, Any]:
        """同步健康检查（已废弃，请使用 health_check_async）"""
        import warnings
        warnings.warn(
            "health_check is deprecated, use health_check_async instead",
            DeprecationWarning,
            stacklevel=2,
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.health_check_async())
        finally:
            loop.close()

    def __del__(self):
        """Never create or run an event loop during garbage collection."""
        if self._session and not self._session.closed:
            logger.warning("MinerUOCR session was not closed explicitly")
