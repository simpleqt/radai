"""模型上下文与输出 token 限制解析（优先 MySQL llm_models）。"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# 数据库不可用或模型未配置时采用保守窗口，避免未知模型按大窗口过量注入。
DEFAULT_MAX_INPUT_TOKENS = max(
    2048,
    int(os.getenv("MODEL_CONTEXT_SAFE_FALLBACK_TOKENS", "8192") or "8192"),
)
# 默认最大输出 token（与 llm_models.max_tokens 列默认一致）
DEFAULT_MAX_OUTPUT_TOKENS = max(
    256,
    int(os.getenv("MODEL_OUTPUT_SAFE_FALLBACK_TOKENS", "2048") or "2048"),
)
_LIMITS_CACHE_TTL_SECONDS = max(
    0.0,
    float(os.getenv("MODEL_CONTEXT_LIMITS_CACHE_SECONDS", "60") or "60"),
)
_LIMITS_CACHE_LOCK = threading.RLock()
# 过期条目保留在缓存里作为"陈旧值":事件循环线程过期时直接返回陈旧值并由
# 后台线程刷新,避免在 loop 上做同步 MySQL 往返(冷启动首查仍是同步一次)。
_LIMITS_CACHE: dict[tuple[str, str], tuple[float, tuple[int | None, int | None]]] = {}
_LIMITS_REFRESH_INFLIGHT: set[tuple[str, str]] = set()


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _schedule_limits_refresh(
    cache_key: tuple[str, str],
    *,
    name: str | None,
    api_model: str | None,
) -> None:
    """Single-flight background refresh so expiry never blocks the event loop."""
    with _LIMITS_CACHE_LOCK:
        if cache_key in _LIMITS_REFRESH_INFLIGHT:
            return
        _LIMITS_REFRESH_INFLIGHT.add(cache_key)

    def _refresh() -> None:
        try:
            _query_model_limits_from_db(cache_key, name=name, api_model=api_model)
        finally:
            with _LIMITS_CACHE_LOCK:
                _LIMITS_REFRESH_INFLIGHT.discard(cache_key)

    threading.Thread(
        target=_refresh, name="model-limits-refresh", daemon=True
    ).start()


def _lookup_model_limits_from_db(
    *,
    name: str | None = None,
    api_model: str | None = None,
) -> tuple[int | None, int | None]:
    """从 MySQL 查询 (max_tokens, max_input_tokens)，未命中返回 (None, None)。"""
    if not name and not api_model:
        return None, None
    cache_key = (str(name or ""), str(api_model or ""))
    now = time.monotonic()
    if _LIMITS_CACHE_TTL_SECONDS > 0:
        with _LIMITS_CACHE_LOCK:
            cached = _LIMITS_CACHE.get(cache_key)
        if cached is not None:
            if cached[0] > now:
                return cached[1]
            if _has_running_loop():
                _schedule_limits_refresh(cache_key, name=name, api_model=api_model)
                return cached[1]
        elif _has_running_loop():
            # 冷启动首查同样不得在事件循环上做同步 MySQL 往返——DB 故障时该
            # 调用会按连接超时阻塞整个 loop(R4-LM-6)。后台单飞刷新,本次
            # 返回"未知"让上层走保守默认;不写负缓存,刷新完成后即拿到真值。
            _schedule_limits_refresh(cache_key, name=name, api_model=api_model)
            return (None, None)
    return _query_model_limits_from_db(cache_key, name=name, api_model=api_model)


def _query_model_limits_from_db(
    cache_key: tuple[str, str],
    *,
    name: str | None,
    api_model: str | None,
) -> tuple[int | None, int | None]:
    """同步 MySQL 查询并写缓存;只应在非事件循环线程或冷启动时调用。"""
    try:
        from sqlalchemy import select

        from backend.db.connection import get_session
        from backend.db.models.llm_model import LlmModel

        session = get_session()
        try:
            row = None
            for condition in _model_lookup_conditions(
                LlmModel,
                name=name,
                api_model=api_model,
            ):
                stmt = (
                    select(LlmModel.max_tokens, LlmModel.max_input_tokens)
                    .where(LlmModel.usage_scope == "chat", LlmModel.is_active == True)
                    .where(condition)
                    .order_by(LlmModel.is_default.desc(), LlmModel.sort_order)
                    .limit(1)
                )
                row = session.execute(stmt).first()
                if row:
                    break
            if not row:
                result = (None, None)
                _cache_model_limits(cache_key, result)
                return result
            max_out, max_in = row[0], row[1]
            out = max_out if isinstance(max_out, int) and max_out > 0 else None
            inp = max_in if isinstance(max_in, int) and max_in > 0 else None
            result = (out, inp)
            _cache_model_limits(cache_key, result)
            return result
        finally:
            session.close()
    except Exception as exc:
        logger.debug("lookup model limits from DB failed: %s", exc)
        # DB 异常绝不能覆盖最后已知好值——这是 stale-serve 的前提。仅当缓存里
        # 尚无任何条目(冷启动)时才写入保守负结果;已有条目(哪怕已过期)保留，
        # 让上层继续用旧的大窗口，而不是每个 TTL 周期都塌缩到保守 fallback。
        with _LIMITS_CACHE_LOCK:
            existing = _LIMITS_CACHE.get(cache_key)
        if existing is not None:
            return existing[1]
        _cache_model_limits(cache_key, (None, None))
    return None, None


def _cache_model_limits(
    cache_key: tuple[str, str],
    value: tuple[int | None, int | None],
) -> None:
    if _LIMITS_CACHE_TTL_SECONDS <= 0:
        return
    with _LIMITS_CACHE_LOCK:
        _LIMITS_CACHE[cache_key] = (
            time.monotonic() + _LIMITS_CACHE_TTL_SECONDS,
            value,
        )


def _model_lookup_conditions(
    model_type: Any,
    *,
    name: str | None,
    api_model: str | None,
) -> list[Any]:
    """返回确定性的查询顺序：配置名优先，API model 仅作回退。"""
    conditions: list[Any] = []
    if name:
        conditions.append(model_type.name == name)
    if api_model:
        conditions.append(model_type.model == api_model)
    return conditions


def load_chat_llm_config_from_db(
    *,
    name: str | None = None,
    api_model: str | None = None,
) -> dict[str, Any] | None:
    """按 name 或 API model 从 MySQL 加载完整对话模型配置（供 HTTP/RAG 调用）。"""
    if not name and not api_model:
        return None
    try:
        from sqlalchemy import select

        from backend.db.connection import get_session
        from backend.db.models.llm_model import LlmModel

        session = get_session()
        try:
            row = None
            for condition in _model_lookup_conditions(
                LlmModel,
                name=name,
                api_model=api_model,
            ):
                stmt = (
                    select(LlmModel)
                    .where(LlmModel.usage_scope == "chat", LlmModel.is_active == True)
                    .where(condition)
                    .order_by(LlmModel.is_default.desc(), LlmModel.sort_order)
                    .limit(1)
                )
                row = session.execute(stmt).scalar_one_or_none()
                if row is not None:
                    break
            if row is None:
                return None
            config = row.to_model_config_dict()
            config["name"] = row.name
            return config
        finally:
            session.close()
    except Exception as exc:
        logger.debug("load chat llm config from DB failed: %s", exc)
    return None


def resolve_max_input_tokens(
    llm_config: dict[str, Any] | None = None,
    *,
    model_name: str | None = None,
    api_model: str | None = None,
) -> int:
    """从配置字典或 MySQL 解析最大输入 token，未知模型使用保守窗口。"""
    if llm_config:
        explicit = llm_config.get("max_input_tokens")
        if isinstance(explicit, int) and explicit > 0:
            return explicit
        profile = llm_config.get("profile")
        if isinstance(profile, dict):
            profile_tokens = profile.get("max_input_tokens")
            if isinstance(profile_tokens, int) and profile_tokens > 0:
                return profile_tokens
        model_name = model_name or llm_config.get("name")
        api_model = api_model or llm_config.get("model")

    _, db_input = _lookup_model_limits_from_db(name=model_name, api_model=api_model)
    if db_input is not None:
        return db_input
    return DEFAULT_MAX_INPUT_TOKENS


def resolve_max_tokens(
    llm_config: dict[str, Any] | None = None,
    *,
    model_name: str | None = None,
    api_model: str | None = None,
) -> int:
    """从配置字典或 MySQL 解析最大输出 token（max_tokens）。"""
    if llm_config:
        explicit = llm_config.get("max_tokens")
        if isinstance(explicit, int) and explicit > 0:
            return explicit
        model_name = model_name or llm_config.get("name")
        api_model = api_model or llm_config.get("model")

    db_output, _ = _lookup_model_limits_from_db(name=model_name, api_model=api_model)
    if db_output is not None:
        return db_output
    return DEFAULT_MAX_OUTPUT_TOKENS


def enrich_llm_config(config: dict[str, Any]) -> dict[str, Any]:
    """为 LLM 配置附加 max_tokens / max_input_tokens（优先 MySQL）。"""
    enriched = dict(config)
    api_model = enriched.get("model")
    model_name = enriched.get("name")
    enriched["max_tokens"] = resolve_max_tokens(
        enriched, model_name=model_name, api_model=api_model
    )
    enriched["max_input_tokens"] = resolve_max_input_tokens(
        enriched, model_name=model_name, api_model=api_model
    )
    return enriched


def apply_profile_to_model_settings(model_settings: dict[str, Any], max_input_tokens: int | None) -> None:
    """为 LangChain ChatModel 注入 profile.max_input_tokens。"""
    tokens = (
        max_input_tokens
        if isinstance(max_input_tokens, int) and max_input_tokens > 0
        else DEFAULT_MAX_INPUT_TOKENS
    )
    profile = dict(model_settings.get("profile") or {})
    profile["max_input_tokens"] = tokens
    model_settings["profile"] = profile
