"""
VLM (Vision Language Model) 客户端
用于图片信息提取和视觉理解
"""

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx

from backend.config.loggers import get_logger

logger = get_logger(__name__)


class VLMClient:
    """
    VLM客户端
    用于图片信息提取和视觉理解
    """

    def __init__(self):
        self._client = None
        self._initialized = False
        self._api_key = None
        self._api_base = None
        self._model = None

    async def initialize(self) -> bool:
        """
        初始化VLM客户端

        Returns:
            是否初始化成功
        """
        try:
            from backend.config.settings import settings

            self._api_key = settings.vlm_api_key
            self._api_base = settings.vlm_api_base
            self._model = settings.vlm_model or "glm-4.6v"

            if not self._api_key:
                logger.warning(
                    "VLM API key not configured, VLM features will be disabled"
                )
                self._initialized = False
                return False

            self._client = httpx.AsyncClient(timeout=300.0)
            self._initialized = True
            logger.info(
                f"VLM client initialized successfully with model: {self._model}"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to initialize VLM client: {e}")
            self._initialized = False
            return False

    async def analyze_image(
        self,
        image_path: str,
        prompt: str = "请详细描述这张图片中的所有内容，包括文字、表格、图表等。",
    ) -> dict[str, Any]:
        """
        分析图像

        Args:
            image_path: 图像路径
            prompt: 分析提示词

        Returns:
            分析结果
        """
        if not self._initialized:
            await self.initialize()

        if self._client is None:
            logger.warning("VLM client not available")
            return {"text": "", "confidence": 0.0, "error": "Client not initialized"}

        try:
            image_data = await self._encode_image(image_path)

            base_url = self._api_base.rstrip("/")
            response = await self._client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": self._model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{image_data}"
                                    },
                                },
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                    "temperature": 0.3,
                    "max_tokens": 2000,
                },
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code == 200:
                data = response.json()
                content = (
                    data.get("choices", [{}])[0].get("message", {}).get("content", "")
                )

                return {
                    "text": content,
                    "confidence": 0.9,
                    "raw_response": data,
                }
            else:
                logger.error(f"VLM API error: {response.status_code} - {response.text}")
                return {
                    "text": "",
                    "confidence": 0.0,
                    "error": f"API error: {response.status_code}",
                }

        except Exception as e:
            logger.error(f"VLM analysis failed: {e}")
            return {"text": "", "confidence": 0.0, "error": str(e)}

    async def _encode_image(self, image_path: str) -> str:
        """
        编码图像为 base64

        Args:
            image_path: 图像路径或URL

        Returns:
            base64 编码的图像
        """
        try:
            if image_path.startswith("http://") or image_path.startswith("https://"):
                logger.info(f"Downloading image from URL: {image_path}")
                if self._client is None:
                    raise RuntimeError("VLM async client not initialized")
                response = await self._client.get(image_path)
                response.raise_for_status()
                return base64.b64encode(response.content).decode("utf-8")
            else:
                raw = await asyncio.to_thread(Path(image_path).read_bytes)
                return base64.b64encode(raw).decode("utf-8")
        except Exception as e:
            logger.error(f"Failed to encode image: {e}")
            raise

    async def extract_text_from_image(self, image_path: str) -> dict[str, Any]:
        """
        从图像中提取文字

        Args:
            image_path: 图像路径

        Returns:
            提取的文本结果
        """
        return await self.analyze_image(
            image_path,
            prompt="请准确识别并提取这张图片中的所有文字内容，包括表格中的文字、公式中的符号等。",
        )

    async def extract_table_from_image(self, image_path: str) -> dict[str, Any]:
        """
        从图像中提取表格

        Args:
            image_path: 图像路径

        Returns:
            表格提取结果
        """
        return await self.analyze_image(
            image_path,
            prompt="请识别并提取这张图片中的所有表格内容，以结构化的格式输出，包括表头和所有单元格数据。",
        )

    async def extract_formula_from_image(self, image_path: str) -> dict[str, Any]:
        """
        从图像中提取公式

        Args:
            image_path: 图像路径

        Returns:
            公式提取结果
        """
        return await self.analyze_image(
            image_path,
            prompt="请识别并提取这张图片中的所有数学公式和符号，以 LaTeX 格式输出。",
        )

    async def analyze_document_page(
        self, page_image_path: str, analysis_type: str = "full"
    ) -> dict[str, Any]:
        """
        分析文档页面

        Args:
            page_image_path: 页面图像路径
            analysis_type: 分析类型 (full/text/table/formula)

        Returns:
            页面分析结果
        """
        prompts = {
            "full": "请全面分析这个文档页面，提取所有文字内容、表格、公式、图表等，并以结构化的格式输出。",
            "text": "请提取这个文档页面中的所有文字内容。",
            "table": "请识别并提取这个文档页面中的所有表格内容。",
            "formula": "请识别并提取这个文档页面中的所有数学公式和符号。",
        }

        prompt = prompts.get(analysis_type, prompts["full"])
        return await self.analyze_image(page_image_path, prompt)

    async def close(self) -> None:
        """关闭客户端"""
        if self._client:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.warning(f"Error closing VLM client: {e}")
        self._initialized = False
        self._client = None


async def get_vlm_client() -> VLMClient | None:
    """
    获取VLM客户端单例

    Returns:
        VLM客户端实例
    """
    client = VLMClient()
    if await client.initialize():
        return client
    return None
