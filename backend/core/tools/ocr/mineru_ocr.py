"""MinerU OCR 服务实现

封装 MinerU API 调用，提供文档解析、状态查询、结果获取等功能。
基于 MinerU Tianshu API Server 的 RESTful 接口。
"""

import asyncio
import json
import logging
import mimetypes
import os
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from .base_ocr import BaseOCR, OCRConfig, OCRResult, OCRStatus

logger = logging.getLogger(__name__)

# 显式请求级超时：aiohttp 默认 total=300s 会让单次轮询挂满整个轮询预算，
# 网络半死时表现为整体超时而非快速失败重试。
_SUBMIT_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=10, sock_connect=10, sock_read=60)
_STATUS_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=15)
_RESULT_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=120)


class MinerUOCR(BaseOCR):
    """MinerU v2 OCR 服务实现。

    封装 MinerU 3.4+ FastAPI 的异步任务接口，支持：
    - 文档上传与解析任务提交
    - 任务状态查询
    - 解析结果获取（支持选择性获取字段）
    - 异步和同步调用
    """

    def __init__(
        self,
        api_url: str = "http://localhost:8000",
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
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _connector_retry_settings() -> tuple[int, float, float]:
        """Return bounded retry settings for short MinerU restart windows."""
        try:
            attempts = max(int(os.getenv("MINERU_CONNECT_RETRY_ATTEMPTS", "12")), 1)
        except (TypeError, ValueError):
            attempts = 12
        try:
            base_delay = max(float(os.getenv("MINERU_CONNECT_RETRY_BASE_SECONDS", "2")), 0)
        except (TypeError, ValueError):
            base_delay = 2.0
        try:
            max_delay = max(float(os.getenv("MINERU_CONNECT_RETRY_MAX_SECONDS", "10")), 0)
        except (TypeError, ValueError):
            max_delay = 10.0
        return attempts, base_delay, max_delay

    async def _connector_retry_delay(
        self,
        *,
        operation: str,
        attempt: int,
        attempts: int,
        error: aiohttp.ClientConnectorError,
    ) -> None:
        _, base_delay, max_delay = self._connector_retry_settings()
        delay = min(base_delay * (2 ** max(attempt - 1, 0)), max_delay)
        logger.warning(
            "MinerU %s connection failed, retrying %d/%d in %.1fs: %s",
            operation,
            attempt,
            attempts,
            delay,
            error,
        )
        await asyncio.sleep(delay)

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

        attempts, _, _ = self._connector_retry_settings()
        return_images = bool(kwargs.pop("return_images", True))
        response_format_zip = bool(kwargs.pop("response_format_zip", False))
        for attempt in range(1, attempts + 1):
            session = await self._get_session()
            # aiohttp 发送 FormData 里的文件句柄会在事件循环线程同步
            # read()（几十 MB 的 PDF），先在工作线程读入 bytes。
            file_bytes = await asyncio.to_thread(file_path_obj.read_bytes)
            data = aiohttp.FormData()
            data.add_field(
                "files",
                file_bytes,
                filename=file_path_obj.name,
                content_type="application/pdf",
            )
            data.add_field("backend", ocr_config.backend)
            data.add_field("lang_list", ocr_config.lang)
            data.add_field("parse_method", ocr_config.method)
            data.add_field("formula_enable", str(ocr_config.formula_enable).lower())
            data.add_field("table_enable", str(ocr_config.table_enable).lower())
            data.add_field("return_md", "true")
            data.add_field("return_content_list", "true")
            data.add_field("return_middle_json", "true")
            data.add_field("return_images", str(return_images).lower())
            data.add_field("response_format_zip", str(response_format_zip).lower())

            for key, value in ocr_config.extra_params.items():
                data.add_field(key, str(value))

            for key, value in kwargs.items():
                if key not in [
                    "backend",
                    "lang",
                    "method",
                    "parse_method",
                    "lang",
                    "lang_list",
                    "formula_enable",
                    "table_enable",
                ]:
                    data.add_field(key, str(value))

            try:
                async with session.post(
                    f"{self.api_url}/tasks",
                    data=data,
                    timeout=_SUBMIT_TIMEOUT,
                ) as resp:
                    if resp.status in {200, 202}:
                        result = await resp.json()
                        task_id = result.get("task_id")
                        if not task_id:
                            raise RuntimeError(f"MinerU task response missing task_id: {result}")
                        logger.info(f"Task submitted successfully: {task_id}")
                        return task_id
                    error = await resp.text()
                    raise Exception(f"Failed to submit task: {error}")
            except aiohttp.ClientConnectorError as exc:
                if attempt >= attempts:
                    raise
                await self._connector_retry_delay(
                    operation="task submission",
                    attempt=attempt,
                    attempts=attempts,
                    error=exc,
                )

        raise RuntimeError("unreachable MinerU submission retry state")

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
        attempts, _, _ = self._connector_retry_settings()
        for attempt in range(1, attempts + 1):
            session = await self._get_session()
            try:
                async with session.get(
                    f"{self.api_url}/tasks/{task_id}", timeout=_STATUS_TIMEOUT
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        file_names = data.get("file_names") or []
                        file_name = file_names[0] if file_names else ""

                        result = OCRResult(
                            task_id=task_id,
                            status=self._parse_status(data.get("status", "pending")),
                            file_name=file_name,
                            error_message=data.get("error"),
                            created_at=self._parse_datetime(data.get("created_at")),
                            completed_at=self._parse_datetime(data.get("completed_at")),
                            metadata={
                                "backend": data.get("backend"),
                                "started_at": data.get("started_at"),
                                "queued_ahead": data.get("queued_ahead"),
                                "status_url": data.get("status_url"),
                                "result_url": data.get("result_url"),
                            },
                        )

                        return result
                    error = await resp.text()
                    raise Exception(f"Failed to get task status: {error}")
            except aiohttp.ClientConnectorError as exc:
                if attempt >= attempts:
                    raise
                await self._connector_retry_delay(
                    operation="task status query",
                    attempt=attempt,
                    attempts=attempts,
                    error=exc,
                )

        raise RuntimeError("unreachable MinerU status retry state")

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

        async with session.get(
            f"{self.api_url}/tasks/{task_id}/result", timeout=_RESULT_TIMEOUT
        ) as resp:
            if resp.status == 200:
                headers = getattr(resp, "headers", {}) or {}
                content_type = str(headers.get("Content-Type") or "").lower()
                disposition = str(headers.get("Content-Disposition") or "").lower()
                if "application/zip" in content_type or ".zip" in disposition:
                    return await self._read_streamed_zip_result(
                        resp,
                        task_id=task_id,
                        upload_images=upload_images,
                        image_object_prefix=str(
                            kwargs.get("image_object_prefix")
                            or f"ocr/{task_id}/images"
                        ),
                        image_bucket_name=kwargs.get("image_bucket_name"),
                    )

                data = await resp.json()
                result_map = data.get("results") or {}
                file_name, result_data = next(iter(result_map.items()), ("", {}))
                if not isinstance(result_data, dict):
                    result_data = {}

                result = OCRResult(
                    task_id=task_id,
                    status=OCRStatus.COMPLETED,
                    file_name=file_name,
                    metadata={
                        "backend": data.get("backend"),
                        "version": data.get("version"),
                    },
                )

                result.markdown = result_data.get("md_content")
                result.content_list = self._decode_json_field(
                    result_data.get("content_list"), expected_type=list
                )
                result.middle_json = self._decode_json_field(
                    result_data.get("middle_json"), expected_type=dict
                )
                result.model_output = self._decode_json_field(
                    result_data.get("model_output"), expected_type=dict
                )
                images = result_data.get("images")
                if isinstance(images, dict):
                    result.images = {
                        name: self._strip_data_url(value)
                        for name, value in images.items()
                    }

                return result
            else:
                error = await resp.text()
                raise Exception(f"Failed to get task data: {error}")

    async def _read_streamed_zip_result(
        self,
        response: Any,
        *,
        task_id: str,
        upload_images: bool,
        image_object_prefix: str,
        image_bucket_name: str | None,
    ) -> OCRResult:
        """Stream a large MinerU ZIP result to disk and normalize its contents."""

        fd, temporary_name = tempfile.mkstemp(
            prefix=f"mineru-{task_id}-",
            suffix=".zip",
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            with temporary_path.open("wb") as output:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    if chunk:
                        output.write(chunk)
            return await asyncio.to_thread(
                self._normalize_zip_result,
                temporary_path,
                task_id=task_id,
                upload_images=upload_images,
                image_object_prefix=image_object_prefix,
                image_bucket_name=image_bucket_name,
            )
        finally:
            await asyncio.to_thread(temporary_path.unlink, missing_ok=True)

    @staticmethod
    def _normalize_zip_result(
        zip_path: Path,
        *,
        task_id: str,
        upload_images: bool,
        image_object_prefix: str,
        image_bucket_name: str | None,
    ) -> OCRResult:
        """Read small JSON/text members and stream image members into MinIO."""

        from backend.config.settings import settings
        from backend.storage import _get_minio_client, get_presigned_url

        markdown: str | None = None
        content_list: list[Any] | None = None
        middle_json: dict[str, Any] | None = None
        model_output: dict[str, Any] | None = None
        images: dict[str, Any] = {}
        file_name = ""
        target_bucket = image_bucket_name or settings.minio_bucket_name
        prefix = image_object_prefix.strip().strip("/") or f"ocr/{task_id}/images"

        with zipfile.ZipFile(zip_path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            for info in members:
                member = Path(info.filename.replace("\\", "/"))
                name = member.name
                lower_name = name.lower()
                if not file_name and len(member.parts) >= 2:
                    file_name = member.parts[0]

                if lower_name.endswith("_content_list.json") and not lower_name.endswith(
                    "_content_list_v2.json"
                ):
                    decoded = json.loads(archive.read(info).decode("utf-8"))
                    if isinstance(decoded, list):
                        content_list = decoded
                elif lower_name.endswith("_middle.json"):
                    decoded = json.loads(archive.read(info).decode("utf-8"))
                    if isinstance(decoded, dict):
                        middle_json = decoded
                elif lower_name.endswith("_model.json"):
                    decoded = json.loads(archive.read(info).decode("utf-8"))
                    if isinstance(decoded, dict):
                        model_output = decoded
                elif lower_name.endswith(".md"):
                    markdown = archive.read(info).decode("utf-8")

            if upload_images:
                client = _get_minio_client()
                for info in members:
                    member = Path(info.filename.replace("\\", "/"))
                    if "images" not in member.parts:
                        continue
                    mime_type = mimetypes.guess_type(member.name)[0]
                    if not mime_type or not mime_type.startswith("image/"):
                        continue
                    object_name = f"{prefix}/{member.name}"
                    with archive.open(info) as source:
                        client.put_object(
                            target_bucket,
                            object_name,
                            source,
                            length=info.file_size,
                            content_type=mime_type,
                        )
                    url = get_presigned_url(
                        object_name,
                        bucket_name=target_bucket,
                    )
                    images[member.name] = {
                        "url": url,
                        "object_key": object_name,
                        "size": info.file_size,
                    }

        return OCRResult(
            task_id=task_id,
            status=OCRStatus.COMPLETED,
            file_name=file_name,
            markdown=markdown,
            content_list=content_list,
            middle_json=middle_json,
            model_output=model_output,
            images=images or None,
            metadata={
                "transport": "zip_stream",
                "images_uploaded": len(images),
            },
        )

    @staticmethod
    def _decode_json_field(value: Any, *, expected_type: type) -> Any:
        if isinstance(value, expected_type):
            return value
        if isinstance(value, str) and value.strip():
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return None
            return decoded if isinstance(decoded, expected_type) else None
        return None

    @staticmethod
    def _strip_data_url(value: Any) -> Any:
        if not isinstance(value, str) or not value.startswith("data:"):
            return value
        marker = ";base64,"
        return value.split(marker, 1)[1] if marker in value else value

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

        async with session.get(f"{self.api_url}/health") as resp:
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
