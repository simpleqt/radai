"""Unified model factory — creates chat model instances.

``create_chat_model`` resolution order depends on ``MODEL_CONFIG_SOURCE`` (default: ``db``):

- **db** (default): active row in MySQL ``llm_models`` (``usage_scope='chat'``) →
  ``config.yaml`` via ``get_app_config()`` → ``BaseLLMProvider`` / ``settings``.
- **yaml** (or any value other than ``db``): skip the database → ``config.yaml`` →
  ``settings``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Union

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, AIMessage
from langchain_core.outputs import LLMResult, ChatGenerationChunk
from langchain_core.language_models.chat_models import BaseChatModel

from backend.config.loggers import get_logger

logger = get_logger(__name__)

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

    def _elapsed_ms(self) -> float:
        start = getattr(self, "start_time", None)
        if start is None:
            return 0.0
        return (time.time() - start) * 1000

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
        model_name = metadata.get("model_name") or serialized.get("name") or "unknown"
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
            f"[LLMPerformance] 开始调用 model={model_name}, messages_count={len(messages[0])}, total_chars={total_chars}, run_id={run_id}"
        )
        self.start_time = time.time()
        self.first_token_time = None

    def on_llm_new_token(
        self,
        token: str,
        *,
        chunk: Optional[Union[ChatGenerationChunk, LLMResult]] = None,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        if self.first_token_time is None:
            self.first_token_time = time.time()
            ttft = (self.first_token_time - self.start_time) * 1000
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
            self._elapsed_ms(),
            total_tokens,
            finish_reasons or "-",
            run_id,
        )

    def on_chat_model_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        logger.error(
            "[LLMPerformance] Chat模型调用异常 run_id=%s, duration=%.2fms, error=%r",
            run_id,
            self._elapsed_ms(),
            error,
            exc_info=error,
        )

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
            self._elapsed_ms(),
            total_tokens,
            run_id,
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        logger.error(
            "[LLMPerformance] LLM调用异常 run_id=%s, duration=%.2fms, error=%r",
            run_id,
            self._elapsed_ms(),
            error,
            exc_info=error,
        )

_CONFIG_AVAILABLE: bool | None = None
_DB_FAILURE_BACKOFF_SECONDS = 60.0
_db_unavailable_until = 0.0


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

    model_class = resolve_class(model_config.use, BaseChatModel)
    model_settings = _build_model_settings(model_config, thinking_enabled, kwargs)

    model_instance = model_class(**kwargs, **model_settings)

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


def _load_from_db(name: str | None) -> dict | None:
    """从数据库加载模型配置（带进程内缓存 + TTL）"""
    from backend.core.models.model_cache import get_cached_model_config, set_cached_model_config
    
    cached = get_cached_model_config(name)
    if cached:
        return cached

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
                    LlmModel.usage_scope == "chat",
                    LlmModel.is_active == True,
                )
                .order_by(LlmModel.is_default.desc(), LlmModel.sort_order)
                .limit(1)
            )
        else:
            stmt = select(LlmModel).where(
                LlmModel.name == name,
                LlmModel.usage_scope == "chat",
                LlmModel.is_active == True,
            ).limit(1)

        result = session.execute(stmt)
        model = result.scalar_one_or_none()
        if model:
            config = model.to_model_config_dict()
            set_cached_model_config(name or "__default__", config)
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
    name = model_config_obj.name
    
    model_class = resolve_class(model_config_obj.use, BaseChatModel)
    model_settings = _build_model_settings(model_config_obj, thinking_enabled, kwargs)

    model_instance = model_class(**kwargs, **model_settings)

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
    if "chatopenai" not in str(model_use).lower():
        return
    model_settings.setdefault("disable_streaming", "tool_calling")


def _build_model_settings(model_config_obj: Any, thinking_enabled: bool, kwargs: dict) -> dict[str, Any]:
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
        model_config_obj, model_settings, thinking_enabled, kwargs, model_use=model_config_obj.use
    )
    _ensure_tool_calling_stream_compat(model_settings, model_use=model_config_obj.use)
    return model_settings


def create_chat_model(
    name: str | None = None,
    thinking_enabled: bool = False,
    **kwargs: Any,
) -> BaseChatModel:
    """Create a chat model instance.

    Uses ``MODEL_CONFIG_SOURCE`` (default ``db``): when ``db``, tries MySQL
    ``llm_models`` first, then ``config.yaml``, then ``BaseLLMProvider`` /
    ``settings``. When not ``db``, skips the database and uses ``config.yaml``
    then ``settings``.
    """
    global _db_unavailable_until

    config_source = os.environ.get("MODEL_CONFIG_SOURCE", "db").lower()

    if config_source == "db" and time.monotonic() >= _db_unavailable_until:
        try:
            db_config = _load_from_db(name)
            if db_config:
                return _create_from_db_config(db_config, thinking_enabled, **kwargs)
        except Exception as e:
            _db_unavailable_until = time.monotonic() + _DB_FAILURE_BACKOFF_SECONDS
            logger.debug("DB-based model creation unavailable for '%s', falling back to config.yaml: %s", name, e)

    if _check_config_available():
        try:
            return _create_from_config(name, thinking_enabled, **kwargs)
        except Exception as e:
            logger.warning("Config-based model creation failed for '%s': %s, falling back to settings", name, e)

    return _create_from_settings(name, **kwargs)


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
