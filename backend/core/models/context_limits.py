"""模型上下文与输出 token 限制解析（优先 MySQL llm_models）。"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 128K 上下文（token 数）
DEFAULT_MAX_INPUT_TOKENS = 131072
# 默认最大输出 token（与 llm_models.max_tokens 列默认一致）
DEFAULT_MAX_OUTPUT_TOKENS = 16384


def _lookup_model_limits_from_db(
    *,
    name: str | None = None,
    api_model: str | None = None,
) -> tuple[int | None, int | None]:
    """从 MySQL 查询 (max_tokens, max_input_tokens)，未命中返回 (None, None)。"""
    if not name and not api_model:
        return None, None
    try:
        from sqlalchemy import or_, select

        from backend.db.connection import get_session
        from backend.db.models.llm_model import LlmModel

        conditions = []
        if name:
            conditions.append(LlmModel.name == name)
        if api_model:
            conditions.append(LlmModel.model == api_model)

        session = get_session()
        try:
            stmt = (
                select(LlmModel.max_tokens, LlmModel.max_input_tokens)
                .where(LlmModel.usage_scope == "chat", LlmModel.is_active == True)
                .where(or_(*conditions))
                .order_by(LlmModel.is_default.desc(), LlmModel.sort_order)
                .limit(1)
            )
            row = session.execute(stmt).first()
            if not row:
                return None, None
            max_out, max_in = row[0], row[1]
            out = max_out if isinstance(max_out, int) and max_out > 0 else None
            inp = max_in if isinstance(max_in, int) and max_in > 0 else None
            return out, inp
        finally:
            session.close()
    except Exception as exc:
        logger.debug("lookup model limits from DB failed: %s", exc)
    return None, None


def load_chat_llm_config_from_db(
    *,
    name: str | None = None,
    api_model: str | None = None,
) -> dict[str, Any] | None:
    """按 name 或 API model 从 MySQL 加载完整对话模型配置（供 HTTP/RAG 调用）。"""
    if not name and not api_model:
        return None
    try:
        from sqlalchemy import or_, select

        from backend.db.connection import get_session
        from backend.db.models.llm_model import LlmModel

        conditions = []
        if name:
            conditions.append(LlmModel.name == name)
        if api_model:
            conditions.append(LlmModel.model == api_model)

        session = get_session()
        try:
            stmt = (
                select(LlmModel)
                .where(LlmModel.usage_scope == "chat", LlmModel.is_active == True)
                .where(or_(*conditions))
                .order_by(LlmModel.is_default.desc(), LlmModel.sort_order)
                .limit(1)
            )
            row = session.execute(stmt).scalar_one_or_none()
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
    """从配置字典或 MySQL 解析最大输入 token，统一默认 128K。"""
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
