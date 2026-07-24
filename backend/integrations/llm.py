"""
LLM (Large Language Model) 客户端
用于文本生成、摘要、问答等
"""

from typing import Any

import httpx

from backend.config.loggers import get_logger

logger = get_logger(__name__)


class LLMClient:
    """
    LLM客户端
    用于文本生成、摘要、问答等
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self._client: httpx.AsyncClient | None = None
        self._initialized = False
        self._api_key = api_key
        self._api_base = base_url
        self._model = model

    async def initialize(self) -> bool:
        """
        初始化LLM客户端

        Returns:
            是否初始化成功
        """
        try:
            from backend.config.settings import settings

            if not self._api_key or not self._api_base or not self._model:
                llm_config = settings.active_llm_config
                self._api_key = self._api_key or llm_config.get("api_key")
                self._api_base = self._api_base or llm_config.get("base_url")
                self._model = self._model or llm_config.get("model")

            if not self._api_key:
                logger.warning(
                    "LLM API key not configured, LLM features will be disabled"
                )
                self._initialized = False
                return False

            self._client = httpx.AsyncClient(trust_env=False, timeout=300.0)
            self._initialized = True
            logger.info(
                f"LLM client initialized successfully with model: {self._model}"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to initialize LLM client: {e}")
            self._initialized = False
            return False

    async def chat(
        self,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 2000,
    ) -> str:
        """
        聊天接口

        Args:
            prompt: 用户提示词
            temperature: 温度参数
            max_tokens: 最大token数

        Returns:
            模型回复
        """
        if not self._initialized:
            await self.initialize()

        if self._client is None:
            logger.warning("LLM client not available")
            return ""

        try:
            api_url = f"{self._api_base.rstrip('/')}/chat/completions"
            response = await self._client.post(
                api_url,
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
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
                return content
            else:
                logger.error(f"LLM API error: {response.status_code} - {response.text}")
                return ""

        except Exception as e:
            logger.error(f"LLM chat failed: {e}")
            return ""

    async def generate_summary(
        self, text: str, summary_type: str = "detailed"
    ) -> dict[str, Any]:
        """
        生成摘要

        Args:
            text: 文本内容
            summary_type: 摘要类型 (short/detailed/both)

        Returns:
            摘要结果
        """
        max_length = 4000
        if len(text) > max_length:
            text = text[:max_length]

        result = {
            "short": "",
            "detailed": "",
            "key_points": [],
            "topics": [],
        }

        if summary_type in ["short", "both"]:
            prompt = f"""请为以下文档生成一个简短摘要（1-2句话）：

文档内容：
{text}

要求：
1. 用中文回答
2. 摘要应该简洁明了
3. 包含文档的核心内容
4. 1-2句话即可
"""
            result["short"] = await self.chat(prompt, temperature=0.3, max_tokens=500)

        if summary_type in ["detailed", "both"]:
            prompt = f"""请为以下文档生成一个详细摘要（3-5句话）：

文档内容：
{text}

要求：
1. 用中文回答
2. 摘要应该详细但不过于冗长
3. 包含文档的主要内容和结构
4. 3-5句话
"""
            result["detailed"] = await self.chat(
                prompt, temperature=0.5, max_tokens=1000
            )

        if summary_type in ["detailed", "both"]:
            prompt = f"""请从以下文档中提取3-5个关键点：

文档内容：
{text}

要求：
1. 用中文回答
2. 每个关键点用一句话概括
3. 关键点应该简洁明了
4. 以JSON数组格式返回，格式：["关键点1", "关键点2", "关键点3"]
"""
            response = await self.chat(prompt, temperature=0.5, max_tokens=800)
            try:
                import json

                key_points = json.loads(response)
                if isinstance(key_points, list):
                    result["key_points"] = key_points[:5]
            except json.JSONDecodeError:
                lines = response.split("\n")
                result["key_points"] = [line.strip() for line in lines if line.strip()][
                    :5
                ]

        if summary_type in ["detailed", "both"]:
            prompt = f"""请从以下文档中提取3-5个主要主题：

文档内容：
{text}

要求：
1. 用中文回答
2. 每个主题用2-4个字概括
3. 主题应该简洁明了
4. 以JSON数组格式返回，格式：["主题1", "主题2", "主题3"]
"""
            response = await self.chat(prompt, temperature=0.5, max_tokens=800)
            try:
                import json

                topics = json.loads(response)
                if isinstance(topics, list):
                    result["topics"] = topics[:5]
            except json.JSONDecodeError:
                lines = response.split("\n")
                result["topics"] = [line.strip() for line in lines if line.strip()][:5]

        return result

    async def close(self) -> None:
        """关闭客户端"""
        if self._client:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.warning(f"Error closing LLM client: {e}")
        self._initialized = False
        self._client = None


async def get_llm_client(
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLMClient | None:
    """
    获取LLM客户端实例

    Args:
        model: 模型名称
        api_key: API密钥
        base_url: API基础URL

    Returns:
        LLM客户端实例
    """
    client = LLMClient(model=model, api_key=api_key, base_url=base_url)
    if await client.initialize():
        return client
    return None
