"""
模型封装模块
提供统一的 LLM 模型接口，支持 DeepSeek, ZhipuAI (GLM), Qwen, OpenAI 等多种提供商

使用 LangChain 1.0.3 的标准接口封装模型
"""

import threading
from typing import Any

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from backend.config.loggers import get_logger
from backend.config.settings import settings

logger = get_logger(__name__)

_shared_http_client: httpx.Client | None = None
_shared_async_http_client: httpx.AsyncClient | None = None
_shared_http_clients_lock = threading.Lock()


def _get_shared_http_clients() -> tuple[httpx.Client, httpx.AsyncClient]:
    global _shared_http_client, _shared_async_http_client
    with _shared_http_clients_lock:
        if _shared_http_client is None or _shared_http_client.is_closed:
            _shared_http_client = httpx.Client(trust_env=False)
        if _shared_async_http_client is None or _shared_async_http_client.is_closed:
            _shared_async_http_client = httpx.AsyncClient(trust_env=False)
        return _shared_http_client, _shared_async_http_client


async def close_shared_http_clients() -> None:
    """Close process-owned LLM transports during application shutdown."""
    global _shared_http_client, _shared_async_http_client
    with _shared_http_clients_lock:
        sync_client, async_client = _shared_http_client, _shared_async_http_client
        _shared_http_client = None
        _shared_async_http_client = None
    if async_client is not None and not async_client.is_closed:
        await async_client.aclose()
    if sync_client is not None and not sync_client.is_closed:
        sync_client.close()


def _inject_no_proxy_http_clients(model_config: dict[str, Any]) -> dict[str, Any]:
    sync_client, async_client = _get_shared_http_clients()
    if "http_client" not in model_config:
        model_config["http_client"] = sync_client
    if "http_async_client" not in model_config:
        model_config["http_async_client"] = async_client
    return model_config


class BaseLLMProvider:
    """
    基础 LLM 提供商类

    提供统一的接口来获取不同提供商的模型实例
    """

    _instances: dict[str, BaseChatModel] = {}

    @classmethod
    def get_default_model(cls) -> BaseChatModel:
        """
        获取默认模型

        Returns:
            默认的聊天模型实例
        """
        return cls.get_model(model_name=None)

    @classmethod
    def get_model(
        cls,
        model_name: str | None = None,
        provider: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        streaming: bool | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        """
        获取配置好的聊天模型实例

        Args:
            model_name: 模型名称，如果为 None 则使用配置中的默认模型
            provider: 提供商名称，如果为 None 则使用配置中的默认提供商
            temperature: 温度参数 (0.0-2.0)
            max_tokens: 最大生成 token 数
            streaming: 是否启用流式输出
            **kwargs: 其他传递给模型的参数

        Returns:
            配置好的 ChatModel 实例
        """
        provider = provider or settings.llm_provider

        if provider == "deepseek":
            return cls._get_deepseek_model(
                model_name, temperature, max_tokens, streaming, **kwargs
            )
        elif provider == "glm4_flash":
            return cls._get_glm4_flash_model(
                model_name, temperature, max_tokens, streaming, **kwargs
            )
        elif provider == "zhipu":
            return cls._get_zhipu_model(
                model_name, temperature, max_tokens, streaming, **kwargs
            )
        elif provider == "qwen":
            return cls._get_qwen_model(
                model_name, temperature, max_tokens, streaming, **kwargs
            )
        elif provider == "minimax":
            return cls._get_minimax_model(
                model_name, temperature, max_tokens, streaming, **kwargs
            )
        elif provider == "openai":
            return cls._get_openai_model(
                model_name, temperature, max_tokens, streaming, **kwargs
            )
        else:
            raise ValueError(f"未知的提供商: {provider}")

    @classmethod
    def _get_deepseek_model(
        cls,
        model_name: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        streaming: bool | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        """
        获取 DeepSeek 模型

        Args:
            model_name: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            streaming: 是否流式输出
            **kwargs: 其他参数

        Returns:
            DeepSeek ChatModel 实例
        """
        model_name = model_name or settings.deepseek_model
        temperature = temperature if temperature is not None else 0.7
        streaming = streaming if streaming is not None else False

        model_config = {
            "model": model_name,
            "temperature": temperature,
            "streaming": streaming,
            "api_key": settings.deepseek_api_key,
            "base_url": settings.deepseek_api_base,
        }

        if max_tokens is not None:
            model_config["max_tokens"] = max_tokens

        model_config.update(kwargs)
        _inject_no_proxy_http_clients(model_config)

        logger.info(f"创建 DeepSeek 模型: {model_name}")

        try:
            model = ChatOpenAI(**model_config)
            return model
        except Exception as e:
            logger.error(f"创建 DeepSeek 模型失败: {e}")
            raise

    @classmethod
    def _get_minimax_model(
        cls,
        model_name: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        streaming: bool | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        """
        获取 MiniMax 模型

        Args:
            model_name: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            streaming: 是否流式输出
            **kwargs: 其他参数

        Returns:
            MiniMax ChatModel 实例
        """
        model_name = model_name or settings.minimax_model
        temperature = temperature if temperature is not None else 0.7
        streaming = streaming if streaming is not None else False
        max_tokens = max_tokens or settings.minimax_max_tokens or 32000

        model_config = {
            "model": model_name,
            "temperature": temperature,
            "streaming": streaming,
            "api_key": settings.minimax_api_key,
            "base_url": settings.minimax_api_base,
            "max_tokens": max_tokens,
        }

        model_config.update(kwargs)
        _inject_no_proxy_http_clients(model_config)

        logger.info(f"创建 MiniMax 模型: {model_name}")

        try:
            model = ChatOpenAI(**model_config)
            return model
        except Exception as e:
            logger.error(f"创建 MiniMax 模型失败: {e}")
            raise

    @classmethod
    def _get_zhipu_model(
        cls,
        model_name: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        streaming: bool | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        """
        获取 ZhipuAI (GLM) 模型

        Args:
            model_name: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            streaming: 是否流式输出
            **kwargs: 其他参数

        Returns:
            ZhipuAI ChatModel 实例
        """
        model_name = model_name or settings.zhipu_model
        temperature = temperature if temperature is not None else 0.7
        streaming = streaming if streaming is not None else False

        model_config = {
            "model": model_name,
            "temperature": temperature,
            "streaming": streaming,
            "api_key": settings.zhipu_api_key,
            "base_url": settings.zhipu_api_base,
        }

        if max_tokens is not None:
            model_config["max_tokens"] = max_tokens

        model_config.update(kwargs)
        _inject_no_proxy_http_clients(model_config)

        logger.info(f"创建 ZhipuAI 模型: {model_name}")

        try:
            model = ChatOpenAI(**model_config)
            return model
        except Exception as e:
            logger.error(f"创建 ZhipuAI 模型失败: {e}")
            raise

    @classmethod
    def _get_glm4_flash_model(
        cls,
        model_name: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        streaming: bool | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        """
        获取 GLM-4-Flash 模型

        Args:
            model_name: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            streaming: 是否流式输出
            **kwargs: 其他参数

        Returns:
            GLM-4-Flash ChatModel 实例
        """
        model_name = model_name or settings.glm4_flash_model
        temperature = temperature if temperature is not None else 0.7
        streaming = streaming if streaming is not None else False
        max_tokens = max_tokens or settings.glm4_flash_max_tokens or 65536

        model_config = {
            "model": model_name,
            "temperature": temperature,
            "streaming": streaming,
            "api_key": settings.glm4_flash_api_key,
            "base_url": settings.glm4_flash_api_base,
            "max_tokens": max_tokens,
        }

        model_config.update(kwargs)
        _inject_no_proxy_http_clients(model_config)

        logger.info(f"创建 GLM-4-Flash 模型: {model_name}")

        try:
            model = ChatOpenAI(**model_config)
            return model
        except Exception as e:
            logger.error(f"创建 GLM-4-Flash 模型失败: {e}")
            raise

    @classmethod
    def _get_qwen_model(
        cls,
        model_name: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        streaming: bool | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        """
        获取 Qwen (DashScope) 模型

        Args:
            model_name: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            streaming: 是否流式输出
            **kwargs: 其他参数

        Returns:
            Qwen ChatModel 实例
        """
        model_name = model_name or settings.dashscope_model
        temperature = temperature if temperature is not None else 0.7
        streaming = streaming if streaming is not None else False

        model_config = {
            "model": model_name,
            "temperature": temperature,
            "streaming": streaming,
            "api_key": settings.dashscope_api_key,
            "base_url": settings.dashscope_api_base,
        }

        if max_tokens is not None:
            model_config["max_tokens"] = max_tokens

        model_config.update(kwargs)
        _inject_no_proxy_http_clients(model_config)

        logger.info(f"创建 Qwen 模型: {model_name}")

        try:
            model = ChatOpenAI(**model_config)
            return model
        except Exception as e:
            logger.error(f"创建 Qwen 模型失败: {e}")
            raise

    @classmethod
    def _get_openai_model(
        cls,
        model_name: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        streaming: bool | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        """
        获取 OpenAI 模型

        Args:
            model_name: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            streaming: 是否流式输出
            **kwargs: 其他参数

        Returns:
            OpenAI ChatModel 实例
        """
        model_name = model_name or "gpt-4o"
        temperature = temperature if temperature is not None else 0.7
        streaming = streaming if streaming is not None else False

        model_config = {
            "model": model_name,
            "temperature": temperature,
            "streaming": streaming,
            "api_key": settings.openai_api_key,
            "base_url": settings.openai_api_base,
        }

        if max_tokens is not None:
            model_config["max_tokens"] = max_tokens

        model_config.update(kwargs)
        _inject_no_proxy_http_clients(model_config)

        logger.info(f"创建 OpenAI 模型: {model_name}")

        try:
            model = ChatOpenAI(**model_config)
            return model
        except Exception as e:
            logger.error(f"创建 OpenAI 模型失败: {e}")
            raise


def get_chat_model(
    model_name: str | None = None,
    provider: str | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """
    获取聊天模型的便捷函数

    Args:
        model_name: 模型名称
        provider: 提供商名称
        **kwargs: 其他参数

    Returns:
        ChatModel 实例
    """
    return BaseLLMProvider.get_model(model_name, provider, **kwargs)
