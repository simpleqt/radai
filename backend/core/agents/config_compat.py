"""Config compatibility helpers.

Provides thin wrappers for model info and default model name, aligned with
``GET /api/v1/rag/models``: prefer MySQL ``llm_models`` (``usage_scope='chat'``,
``is_active``), then fall back to ``config.yaml`` via ``get_app_config``.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

_DB_CACHE_TTL_SECONDS = 30.0
_DB_FAILURE_BACKOFF_SECONDS = 60.0
_db_rows_cache: tuple[float, list] | None = None
_db_unavailable_until = 0.0


def _fetch_active_chat_llm_models():
    """Return active chat-scope LlmModel rows ordered by sort_order, or []."""
    global _db_rows_cache, _db_unavailable_until

    config_source = os.environ.get("MODEL_CONFIG_SOURCE", "db").lower()
    if config_source != "db":
        return []

    now = time.monotonic()
    if _db_rows_cache and now - _db_rows_cache[0] < _DB_CACHE_TTL_SECONDS:
        return list(_db_rows_cache[1])
    if now < _db_unavailable_until:
        return []

    try:
        from sqlalchemy import select

        from backend.db.connection import get_session
        from backend.db.models.llm_model import LlmModel

        session = get_session()
        try:
            stmt = (
                select(LlmModel)
                .where(
                    LlmModel.usage_scope == "chat",
                    LlmModel.is_active == True,
                )
                .order_by(LlmModel.sort_order)
            )
            result = session.execute(stmt)
            rows = list(result.scalars().all())
            _db_rows_cache = (now, rows)
            _db_unavailable_until = 0.0
            return rows
        finally:
            session.close()
    except Exception as e:
        _db_unavailable_until = now + _DB_FAILURE_BACKOFF_SECONDS
        logger.debug("llm_models DB unavailable, falling back to config.yaml: %s", e)
        return []


def get_available_model_info() -> list[dict]:
    """Return available model info dicts exposed to frontend selectors."""
    rows = _fetch_active_chat_llm_models()
    if rows:
        return [
            {
                "name": m.name,
                "display_name": m.display_name or m.name,
                "supports_vision": bool(getattr(m, "supports_vision", False)),
                "supports_thinking": bool(getattr(m, "supports_thinking", False)),
            }
            for m in rows
        ]

    try:
        from backend.core.config import get_app_config

        config = get_app_config()
        return [
            {
                "name": m.name,
                "display_name": m.display_name or m.name,
                "supports_vision": bool(getattr(m, "supports_vision", False)),
                "supports_thinking": bool(getattr(m, "supports_thinking", False)),
            }
            for m in config.models
        ]
    except ImportError:
        logger.debug("backend.core.config unavailable, returning empty model list")
        return []
    except Exception as e:
        logger.warning("Failed to load model info from config: %s", e)
        return []


def get_default_model_name() -> str | None:
    """Return default chat model *name* (API id): DB ``is_default`` then first active, else YAML first."""
    rows = _fetch_active_chat_llm_models()
    if rows:
        for m in rows:
            if m.is_default:
                return m.name
        return rows[0].name

    try:
        from backend.core.config import get_app_config

        config = get_app_config()
        if config.models:
            return config.models[0].name
    except Exception:
        pass
    return None
