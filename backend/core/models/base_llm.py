"""
模型封装模块
提供统一的 LLM 模型接口，支持 DeepSeek, ZhipuAI (GLM), Qwen, OpenAI 等多种提供商

使用 LangChain 1.0.3 的标准接口封装模型
"""

import asyncio
import os
import threading
import weakref
from contextvars import ContextVar
from typing import Any

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from backend.config.loggers import get_logger
from backend.config.settings import settings

logger = get_logger(__name__)

# sync client 不绑事件循环，保持进程单例；async client 的连接绑定创建它的循环，
# 必须按运行循环隔离——否则临时 ``asyncio.run`` 循环建立的 keep-alive 连接会在
# 该循环销毁后残留在共享池里，被主循环租用时报 "Event loop is closed"
# （跨循环连接污染，与第 11 轮 embedding/redis 客户端同类问题）。
_shared_http_client: httpx.Client | None = None
_shared_async_http_client: httpx.AsyncClient | None = None
_shared_async_http_clients_by_loop: (
    "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]"
) = weakref.WeakKeyDictionary()
_shared_http_clients_lock = threading.Lock()

# 当模型经 create_chat_model_async 在 to_thread 工作线程里构造时,线程内没有
# 运行循环,get_shared_http_clients 会走 loop-less 兜底、返回进程级 async
# client,击穿按循环隔离(R4-LM-1)。异步工厂在调用方循环上取到 per-loop
# client 后写入此 ContextVar;asyncio.to_thread 传播 contextvars,工作线程即可
# 优先取用它,而不是进程兜底。
_preferred_async_http_client: ContextVar[httpx.AsyncClient | None] = ContextVar(
    "preferred_async_http_client", default=None
)


def set_preferred_async_http_client(client: httpx.AsyncClient | None):
    """Bind the caller-loop async client for an upcoming off-loop construction."""
    return _preferred_async_http_client.set(client)


def reset_preferred_async_http_client(token) -> None:
    try:
        _preferred_async_http_client.reset(token)
    except ValueError:
        # 跨 Context 重置(理论上不会发生于同任务 set/reset)退化为置空。
        _preferred_async_http_client.set(None)


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _llm_http_timeout() -> httpx.Timeout:
    """显式请求超时，取代 openai SDK 的隐式默认（约 600s）。

    connect 短（快速暴露坏网关），read/write/pool 给足长上下文与流式
    间隔；总调用量级仍由各链路自己的 wait_for/deadline 收口。默认值
    与 SDK 隐式行为对齐（不改变现网时序），可用 env 收紧。
    """
    return httpx.Timeout(
        connect=float(_positive_int_env("LLM_HTTP_CONNECT_TIMEOUT_SECONDS", 10)),
        read=float(_positive_int_env("LLM_HTTP_READ_TIMEOUT_SECONDS", 600)),
        write=float(_positive_int_env("LLM_HTTP_WRITE_TIMEOUT_SECONDS", 600)),
        pool=float(_positive_int_env("LLM_HTTP_POOL_TIMEOUT_SECONDS", 600)),
    )


def _llm_http_limits() -> httpx.Limits:
    """Connection-pool limits for the shared LLM transports.

    httpx defaults to 100 connections with only 20 keep-alive slots; under many
    concurrent SSE streams the excess connections are torn down and re-opened
    (TCP/TLS churn against the same few gateways). Keep a deep keep-alive pool
    with a long expiry instead; both knobs stay env-tunable per deployment.
    """
    return httpx.Limits(
        max_connections=_positive_int_env("LLM_HTTP_MAX_CONNECTIONS", 256),
        max_keepalive_connections=_positive_int_env(
            "LLM_HTTP_MAX_KEEPALIVE_CONNECTIONS", 64
        ),
        keepalive_expiry=float(
            _positive_int_env("LLM_HTTP_KEEPALIVE_EXPIRY_SECONDS", 300)
        ),
    )


def _current_running_loop() -> "asyncio.AbstractEventLoop | None":
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def get_shared_http_clients() -> tuple[httpx.Client, httpx.AsyncClient]:
    """Return the process sync transport and the current loop's async transport.

    The async client is isolated per running loop; loop-less bootstrap/CLI
    callers keep a legacy fallback client.
    """
    global _shared_http_client, _shared_async_http_client
    loop = _current_running_loop()
    with _shared_http_clients_lock:
        if _shared_http_client is None or _shared_http_client.is_closed:
            _shared_http_client = httpx.Client(
                trust_env=False, limits=_llm_http_limits(), timeout=_llm_http_timeout()
            )
        if loop is None:
            # 在 to_thread 工作线程里(无运行循环)优先使用调用方循环预置的
            # per-loop client,避免退回进程兜底而击穿隔离(R4-LM-1)。
            preferred = _preferred_async_http_client.get()
            if preferred is not None and not preferred.is_closed:
                return _shared_http_client, preferred
            if (
                _shared_async_http_client is None
                or _shared_async_http_client.is_closed
            ):
                _shared_async_http_client = httpx.AsyncClient(
                    trust_env=False, limits=_llm_http_limits(), timeout=_llm_http_timeout()
                )
            return _shared_http_client, _shared_async_http_client
        # 已关闭循环的客户端无法靠弱引用回收(连接持有对循环的强引用),主动清扫。
        for dead in [
            item
            for item in _shared_async_http_clients_by_loop.keys()
            if item.is_closed()
        ]:
            _shared_async_http_clients_by_loop.pop(dead, None)
        async_client = _shared_async_http_clients_by_loop.get(loop)
        if async_client is None or async_client.is_closed:
            async_client = httpx.AsyncClient(
                trust_env=False, limits=_llm_http_limits(), timeout=_llm_http_timeout()
            )
            _shared_async_http_clients_by_loop[loop] = async_client
        return _shared_http_client, async_client


async def close_shared_async_http_client_for_current_loop() -> None:
    """Close only the current loop's async LLM transport (ephemeral-loop teardown).

    Sync tool bridges run each call on a disposable ``asyncio.run`` loop; its
    async client must be closed before the loop dies, or its keep-alive
    connections survive on a loop nothing can drive again and later poison the
    shared pool for the main loop.
    """
    loop = _current_running_loop()
    if loop is None:
        return
    with _shared_http_clients_lock:
        async_client = _shared_async_http_clients_by_loop.pop(loop, None)
    if async_client is not None and not async_client.is_closed:
        await async_client.aclose()


async def close_shared_http_clients() -> None:
    """Close process-owned LLM transports during application shutdown."""
    global _shared_http_client, _shared_async_http_client
    with _shared_http_clients_lock:
        sync_client, async_client = _shared_http_client, _shared_async_http_client
        _shared_http_client = None
        _shared_async_http_client = None
        loop_clients = list(_shared_async_http_clients_by_loop.values())
        _shared_async_http_clients_by_loop.clear()
    for client in [async_client, *loop_clients]:
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:
                logger.debug(
                    "skipping LLM async client owned by a dead loop", exc_info=True
                )
    if sync_client is not None and not sync_client.is_closed:
        sync_client.close()


def _inject_no_proxy_http_clients(model_config: dict[str, Any]) -> dict[str, Any]:
    sync_client, async_client = get_shared_http_clients()
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


