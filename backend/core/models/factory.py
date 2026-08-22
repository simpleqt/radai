"""Unified model factory — creates chat model instances.

``create_chat_model`` resolution depends on ``MODEL_CONFIG_SOURCE`` (default: ``db``):

- **db** (default): an active row in MySQL ``llm_models`` for the requested
  usage scope. The database catalog is authoritative and fails closed.
- **yaml** (or any value other than ``db``): skip the database → ``config.yaml`` →
  ``settings``.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Union

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult, ChatGenerationChunk
from langchain_core.language_models.chat_models import BaseChatModel

from backend.config.loggers import get_logger
from backend.core.models.base_llm import get_shared_http_clients

logger = get_logger(__name__)

_MODEL_FACTORY_ASYNC_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("MODEL_FACTORY_ASYNC_TIMEOUT_SECONDS", "10") or "10"),
)


def _is_cancelled_error(error: BaseException) -> bool:
    return isinstance(error, asyncio.CancelledError)

def _extract_llm_token_usage(response: LLMResult) -> dict[str, Any]:
    llm_output = response.llm_output if isinstance(response.llm_output, dict) else {}
    usage = llm_output.get("token_usage") if isinstance(llm_output, dict) else None
    if isinstance(usage, dict):
        return usage
    for generation in response.generations or []:
        for gen in generation:
            message = getattr(gen, "message", None)
            usage_metadata = getattr(message, "usage_metadata", None)
            if isinstance(usage_metadata, dict) and usage_metadata:
                return dict(usage_metadata)
    return {}


def _extract_chat_finish_reasons(response: LLMResult) -> list[str]:
    reasons: list[str] = []
    for generation in response.generations or []:
        for gen in generation:
            generation_info = getattr(gen, "generation_info", None)
            if isinstance(generation_info, dict):
                reason = generation_info.get("finish_reason") or generation_info.get("stop_reason")
                if isinstance(reason, str) and reason.strip():
                    reasons.append(reason.strip())
                    continue
            message = getattr(gen, "message", None)
            response_metadata = getattr(message, "response_metadata", None)
            if isinstance(response_metadata, dict):
                reason = response_metadata.get("finish_reason") or response_metadata.get("stop_reason")
                if isinstance(reason, str) and reason.strip():
                    reasons.append(reason.strip())
    return reasons


class LLMPerformanceCallback(BaseCallbackHandler):
    """Callback to log LLM performance and context size."""

    def _elapsed_ms(self, run_id: Any | None = None) -> float:
        starts = getattr(self, "_run_start_times", {})
        start = starts.get(run_id) if run_id is not None else None
        if start is None:
            start = getattr(self, "start_time", None)
        if start is None:
            return 0.0
        return (time.time() - start) * 1000

    def _finish_run(self, run_id: Any) -> None:
        getattr(self, "_run_start_times", {}).pop(run_id, None)
        getattr(self, "_first_token_times", {}).pop(run_id, None)

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[BaseMessage]],
        *,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        safe_metadata = metadata or {}
        model_name = safe_metadata.get("model_name") or serialized.get("name") or "unknown"
        total_chars = 0
        for msg_list in messages:
            for msg in msg_list:
                content = msg.content
                if isinstance(content, str):
                    total_chars += len(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and "text" in part:
                            total_chars += len(part["text"])
        
        logger.info(
            f"[LLMPerformance] 开始调用 model={model_name}, messages_count={sum(len(item) for item in messages)}, total_chars={total_chars}, run_id={run_id}"
        )
        started_at = time.time()
        self.start_time = started_at
        self._run_start_times = getattr(self, "_run_start_times", {})
        self._run_start_times[run_id] = started_at
        self._first_token_times = getattr(self, "_first_token_times", {})
        self._first_token_times.pop(run_id, None)

    def on_llm_new_token(
        self,
        token: str,
        *,
        chunk: Optional[Union[ChatGenerationChunk, LLMResult]] = None,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        first_tokens = getattr(self, "_first_token_times", {})
        if run_id not in first_tokens:
            first_token_time = time.time()
            first_tokens[run_id] = first_token_time
            self._first_token_times = first_tokens
            started_at = getattr(self, "_run_start_times", {}).get(run_id, first_token_time)
            ttft = (first_token_time - started_at) * 1000
            logger.info(f"[LLMPerformance] 首包延迟 (TTFT): {ttft:.2f}ms, run_id={run_id}")

    def on_chat_model_end(
        self,
        response: LLMResult,
        *,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        usage = _extract_llm_token_usage(response)
        total_tokens = usage.get("total_tokens", 0)
        finish_reasons = _extract_chat_finish_reasons(response)
        logger.info(
            "[LLMPerformance] Chat模型调用结束 duration=%.2fms, total_tokens=%s, "
            "finish_reason=%s, run_id=%s",
            self._elapsed_ms(run_id),
            total_tokens,
            finish_reasons or "-",
            run_id,
        )
        self._finish_run(run_id)

    def on_chat_model_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        if _is_cancelled_error(error):
            logger.info(
                "[LLMPerformance] Chat模型调用已取消 run_id=%s, duration=%.2fms, error=%r",
                run_id,
                self._elapsed_ms(run_id),
                error,
            )
            self._finish_run(run_id)
            return
        logger.error(
            "[LLMPerformance] Chat模型调用异常 run_id=%s, duration=%.2fms, error=%r",
            run_id,
            self._elapsed_ms(run_id),
            error,
            exc_info=error,
        )
        self._finish_run(run_id)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        usage = _extract_llm_token_usage(response)
        total_tokens = usage.get("total_tokens", 0)
        logger.info(
            "[LLMPerformance] LLM调用结束 duration=%.2fms, total_tokens=%s, run_id=%s",
            self._elapsed_ms(run_id),
            total_tokens,
            run_id,
        )
        self._finish_run(run_id)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        if _is_cancelled_error(error):
            logger.info(
                "[LLMPerformance] LLM调用已取消 run_id=%s, duration=%.2fms, error=%r",
                run_id,
                self._elapsed_ms(run_id),
                error,
            )
            self._finish_run(run_id)
            return
        logger.error(
            "[LLMPerformance] LLM调用异常 run_id=%s, duration=%.2fms, error=%r",
            run_id,
            self._elapsed_ms(run_id),
            error,
            exc_info=error,
        )
        self._finish_run(run_id)

_CONFIG_AVAILABLE: bool | None = None
_REASONING_COMPAT_CHAT_OPENAI = (
    "backend.core.models.patched_deepseek_openai:PatchedDeepSeekOpenAI"
)


def _resolve_model_use(model_config: Any) -> str:
    """Use the streaming/tool-call compatible adapter for ChatOpenAI models."""
    model_use = str(model_config.use)
    if model_use.lower() == "langchain_openai:chatopenai":
        return _REASONING_COMPAT_CHAT_OPENAI
    return model_use


def _check_config_available() -> bool:
    """Check if config.yaml-style model configuration is accessible."""
    global _CONFIG_AVAILABLE
    if _CONFIG_AVAILABLE is not None:
        return _CONFIG_AVAILABLE
    try:
        from backend.core.config import get_app_config
        config = get_app_config()
        _CONFIG_AVAILABLE = bool(config and config.models)
    except Exception:
        _CONFIG_AVAILABLE = False
    return _CONFIG_AVAILABLE


def _create_from_config(name: str | None, thinking_enabled: bool, **kwargs: Any) -> BaseChatModel:
    """Create a model instance from the backend config.yaml schema."""
    from backend.core.config import get_app_config
    from backend.core.utils.reflection import resolve_class

    config = get_app_config()
    if name is None:
        name = config.models[0].name

    model_config = config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model {name} not found in config.yaml")

    model_class = resolve_class(_resolve_model_use(model_config), BaseChatModel)
    model_settings = _build_model_settings(model_config, thinking_enabled, kwargs)
    model_settings.update(kwargs)

    model_instance = model_class(**model_settings)

    # 注入性能监控回调
    perf_callback = LLMPerformanceCallback()
    if model_instance.callbacks:
        model_instance.callbacks.append(perf_callback)
    else:
        model_instance.callbacks = [perf_callback]

    try:
        from backend.core.tracing import build_tracing_callbacks
        callbacks = build_tracing_callbacks()
        if callbacks:
            existing = model_instance.callbacks or []
            model_instance.callbacks = [*existing, *callbacks]
    except ImportError:
        pass

    return model_instance


def _apply_thinking_and_profile_settings(
    model_config: Any,
    model_settings: dict,
    thinking_enabled: bool,
    kwargs: dict,
    *,
    model_use: str,
) -> None:
    """合并思考模式参数，并注入 profile.max_input_tokens。"""
    from backend.core.models.context_limits import apply_profile_to_model_settings, resolve_max_input_tokens

    has_thinking_settings = (
        model_config.when_thinking_enabled is not None
    ) or (model_config.thinking is not None)
    effective_wte: dict = dict(model_config.when_thinking_enabled) if model_config.when_thinking_enabled else {}
    if model_config.thinking is not None:
        merged_thinking = {**(effective_wte.get("thinking") or {}), **model_config.thinking}
        effective_wte = {**effective_wte, "thinking": merged_thinking}

    if thinking_enabled and has_thinking_settings:
        if effective_wte:
            model_settings.update(effective_wte)

    if not thinking_enabled and has_thinking_settings:
        if effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
            model_settings["extra_body"] = _deep_merge(
                model_settings.get("extra_body"),
                {"thinking": {"type": "disabled"}},
            )
        elif _vllm_disable(effective_wte.get("extra_body", {}).get("chat_template_kwargs") or {}):
            model_settings["extra_body"] = _deep_merge(
                model_settings.get("extra_body"),
                {"chat_template_kwargs": _vllm_disable(effective_wte.get("extra_body", {}).get("chat_template_kwargs") or {})},
            )
        elif effective_wte.get("thinking", {}).get("type"):
            model_settings["thinking"] = {"type": "disabled"}

    if not thinking_enabled:
        kwargs.pop("reasoning_effort", None)
        _ensure_dashscope_disable(model_settings)
        _ensure_vllm_disable(model_settings, model_use=model_use)
        _force_disable_thinking(model_settings)
        if model_config.supports_reasoning_effort:
            model_settings["reasoning_effort"] = "none"

    if not model_config.supports_reasoning_effort:
        kwargs.pop("reasoning_effort", None)
        model_settings.pop("reasoning_effort", None)

    max_input_tokens = resolve_max_input_tokens(
        {"max_input_tokens": getattr(model_config, "max_input_tokens", None), "name": model_config.name, "model": model_config.model},
    )
    apply_profile_to_model_settings(model_settings, max_input_tokens)


def _create_from_settings(name: str | None, **kwargs: Any) -> BaseChatModel:
    """Create model from settings (BaseLLMProvider)."""
    from backend.core.models.base_llm import BaseLLMProvider
    return BaseLLMProvider.get_model(model_name=name, **kwargs)


def _load_from_db(name: str | None, usage_scope: str = "chat") -> dict | None:
    """从数据库加载模型配置（带进程内缓存 + TTL）"""
    from backend.core.models.model_cache import (
        current_cache_generation,
        get_cached_model_config,
        set_cached_model_config,
    )

    cached = get_cached_model_config(name, usage_scope)
    if cached:
        return cached

    # 在发起 DB 读取前捕获失效代数:读取期间若发生 invalidate,回填会被
    # set_cached_model_config 拒绝,避免旧配置复活(R4-LM-5)。
    generation = current_cache_generation()

    # 同步查询数据库
    from backend.db.connection import get_session
    from backend.db.models.llm_model import LlmModel
    from sqlalchemy import select

    session = get_session()
    try:
        if name is None:
            stmt = (
                select(LlmModel)
                .where(
                    LlmModel.usage_scope == usage_scope,
                    LlmModel.is_active == True,
                )
                .order_by(LlmModel.is_default.desc(), LlmModel.sort_order)
                .limit(1)
            )
        else:
            stmt = select(LlmModel).where(
                LlmModel.name == name,
                LlmModel.usage_scope == usage_scope,
                LlmModel.is_active == True,
            ).limit(1)

        result = session.execute(stmt)
        model = result.scalar_one_or_none()
        if model:
            config = model.to_model_config_dict()
            set_cached_model_config(
                name or "__default__", config, usage_scope, generation=generation
            )
            return config
    finally:
        session.close()

    return None

def _create_from_db_config(db_config: dict, thinking_enabled: bool, **kwargs: Any) -> BaseChatModel:
    """从数据库配置字典创建模型实例（复用核心逻辑）"""
    from backend.core.utils.reflection import resolve_class
    from backend.core.config.model_config import ModelConfig
    
    # 借助现有的 ModelConfig 对象承载和验证，利用它的默认值和结构
    model_config_obj = ModelConfig(**db_config)
    model_class = resolve_class(_resolve_model_use(model_config_obj), BaseChatModel)
    model_settings = _build_model_settings(model_config_obj, thinking_enabled, kwargs)
    model_settings.update(kwargs)

    model_instance = model_class(**model_settings)

    # 注入性能监控回调
    perf_callback = LLMPerformanceCallback()
    if model_instance.callbacks:
        model_instance.callbacks.append(perf_callback)
    else:
        model_instance.callbacks = [perf_callback]

    try:
        from backend.core.tracing import build_tracing_callbacks
        callbacks = build_tracing_callbacks()
        if callbacks:
            existing = model_instance.callbacks or []
            model_instance.callbacks = [*existing, *callbacks]
    except ImportError:
        pass

    return model_instance


def _ensure_tool_calling_stream_compat(model_settings: dict, *, model_use: str) -> None:
    """Avoid broken streamed tool-call deltas on OpenAI-compatible gateways.

    Some hosted vLLM/OpenAI-compatible gateways emit ``finish_reason=tool_calls``
    while omitting a complete tool call in streaming chunks. LangChain then
    cannot enter the tool node and the graph ends with an empty answer. For
    ChatOpenAI-compatible models, keep normal text streaming but force model
    rounds with bound tools to use a non-streaming request.
    """
    normalized_use = str(model_use).lower()
    if "chatopenai" not in normalized_use and "patcheddeepseekopenai" not in normalized_use:
        return
    model_settings.setdefault("disable_streaming", "tool_calling")


def _inject_no_proxy_http_clients(model_settings: dict, *, model_use: str) -> None:
    """Force OpenAI-compatible model clients to ignore proxy environment vars."""
    normalized_use = str(model_use).lower()
    if "chatopenai" not in normalized_use and "patcheddeepseekopenai" not in normalized_use:
        return
    sync_client, async_client = get_shared_http_clients()
    model_settings.setdefault("http_client", sync_client)
    model_settings.setdefault("http_async_client", async_client)


def _build_model_settings(model_config_obj: Any, thinking_enabled: bool, kwargs: dict) -> dict[str, Any]:
    model_use = _resolve_model_use(model_config_obj)
    model_settings = model_config_obj.model_dump(
        exclude_none=True,
        exclude={
            "use", "name", "display_name", "description",
            "supports_thinking", "supports_reasoning_effort",
            "when_thinking_enabled", "thinking", "supports_vision",
            "max_input_tokens",
        },
    )
    _apply_thinking_and_profile_settings(
        model_config_obj, model_settings, thinking_enabled, kwargs, model_use=model_use
    )
    _ensure_tool_calling_stream_compat(model_settings, model_use=model_use)
    _inject_no_proxy_http_clients(model_settings, model_use=model_use)
    return model_settings


def create_chat_model(
    name: str | None = None,
    thinking_enabled: bool = False,
    *,
    usage_scope: str = "chat",
    **kwargs: Any,
) -> BaseChatModel:
    """Create a chat model instance.

    Uses ``MODEL_CONFIG_SOURCE`` (default ``db``). When it is ``db``, MySQL
    ``llm_models`` is authoritative and the requested ``usage_scope`` must
    match. Otherwise, the legacy YAML/settings fallback is retained for
    development compatibility and supports only the ``chat`` scope.
    """
    config_source = os.environ.get("MODEL_CONFIG_SOURCE", "db").lower()

    if config_source == "db":
        try:
            db_config = _load_from_db(name, usage_scope)
            if db_config:
                return _create_from_db_config(db_config, thinking_enabled, **kwargs)
        except Exception as e:
            logger.error(
                "DB-based model creation unavailable for '%s' in scope '%s'",
                name,
                usage_scope,
                exc_info=e,
            )
            raise RuntimeError("数据库模型目录暂不可用") from e
        raise ValueError(
            f"Active model {name or '<default>'} not found in usage scope {usage_scope}"
        )

    if usage_scope != "chat":
        raise ValueError(
            f"Usage scope {usage_scope} requires MODEL_CONFIG_SOURCE=db"
        )

    if _check_config_available():
        try:
            return _create_from_config(name, thinking_enabled, **kwargs)
        except Exception as e:
            logger.warning("Config-based model creation failed for '%s': %s, falling back to settings", name, e)

    return _create_from_settings(name, **kwargs)


async def create_chat_model_async(
    name: str | None = None,
    thinking_enabled: bool = False,
    *,
    usage_scope: str = "chat",
    **kwargs: Any,
) -> BaseChatModel:
    """Create a model without blocking an async caller on DB/config I/O.

    构造被丢进 to_thread,工作线程内没有运行循环——若不干预,注入的 async HTTP
    client 会退回进程级兜底而非本循环的 per-loop client(R4-LM-1)。这里在调用方
    循环上取到 per-loop client 并经 ContextVar 预置(to_thread 传播 contextvars),
    让线程内的 get_shared_http_clients 优先取用它。仅当调用方未显式传入
    http_async_client 时生效。
    """
    from backend.core.models.base_llm import (
        get_shared_http_clients,
        reset_preferred_async_http_client,
        set_preferred_async_http_client,
    )

    token = None
    if "http_async_client" not in kwargs:
        try:
            _sync_client, async_client = get_shared_http_clients()
            token = set_preferred_async_http_client(async_client)
        except Exception:
            logger.debug("预置 per-loop async client 失败,回退线程内解析", exc_info=True)
            token = None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                create_chat_model,
                name,
                thinking_enabled,
                usage_scope=usage_scope,
                **kwargs,
            ),
            timeout=_MODEL_FACTORY_ASYNC_TIMEOUT_SECONDS,
        )
    finally:
        if token is not None:
            reset_preferred_async_http_client(token)


def create_scoped_chat_model(
    usage_scope: str,
    name: str | None = None,
    thinking_enabled: bool = False,
    **kwargs: Any,
) -> BaseChatModel:
    """Create an internal-purpose model without a chat-scope fallback."""

    if usage_scope == "chat":
        raise ValueError("Use create_chat_model for frontend chat models")
    return create_chat_model(
        name=name,
        thinking_enabled=thinking_enabled,
        usage_scope=usage_scope,
        **kwargs,
    )


async def create_scoped_chat_model_async(
    usage_scope: str,
    name: str | None = None,
    thinking_enabled: bool = False,
    **kwargs: Any,
) -> BaseChatModel:
    """Async internal-purpose model factory."""

    return await create_chat_model_async(
        name=name,
        thinking_enabled=thinking_enabled,
        usage_scope=usage_scope,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Helpers for thinking mode management
# ---------------------------------------------------------------------------

def _deep_merge(base: dict | None, override: dict) -> dict:
    merged = dict(base or {})
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _vllm_disable(chat_template_kwargs: dict) -> dict:
    disable_kwargs: dict[str, bool] = {}
    if "thinking" in chat_template_kwargs:
        disable_kwargs["thinking"] = False
    if "enable_thinking" in chat_template_kwargs:
        disable_kwargs["enable_thinking"] = False
    return disable_kwargs


def _ensure_dashscope_disable(model_settings: dict) -> None:
    base_url = model_settings.get("base_url")
    if not isinstance(base_url, str) or "dashscope" not in base_url.lower():
        return
    extra_body = model_settings.get("extra_body")
    if not isinstance(extra_body, dict):
        extra_body = {}
    model_settings["extra_body"] = _deep_merge(extra_body, {"enable_thinking": False})


def _ensure_vllm_disable(model_settings: dict, *, model_use: str = "") -> None:
    is_vllm = "vllm" in str(model_use).lower()
    if not is_vllm:
        base_url = model_settings.get("base_url", "")
        if isinstance(base_url, str) and "dashscope" in base_url.lower():
            return
        model_name = model_settings.get("model", "")
        if not (isinstance(base_url, str) and isinstance(model_name, str)
                and "qwen" in model_name.lower()):
            return

    extra_body = model_settings.get("extra_body")
    if not isinstance(extra_body, dict):
        extra_body = {}
    existing_ctk = extra_body.get("chat_template_kwargs")
    if isinstance(existing_ctk, dict) and "enable_thinking" in existing_ctk:
        return
    model_settings["extra_body"] = _deep_merge(
        extra_body, {"chat_template_kwargs": {"enable_thinking": False}}
    )


def _force_disable_thinking(model_settings: dict) -> None:
    extra_body = model_settings.get("extra_body")
    if not isinstance(extra_body, dict):
        return
    disabled_extra_body: dict = {}
    if "thinking" in extra_body:
        disabled_extra_body["thinking"] = {"type": "disabled"}
    chat_template_kwargs = extra_body.get("chat_template_kwargs")
    if isinstance(chat_template_kwargs, dict):
        if disable_kw := _vllm_disable(chat_template_kwargs):
            disabled_extra_body["chat_template_kwargs"] = disable_kw
    if disabled_extra_body:
        model_settings["extra_body"] = _deep_merge(extra_body, disabled_extra_body)
