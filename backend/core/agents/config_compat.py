"""Config compatibility helpers.

Provides thin wrappers for model info and default model name, aligned with
``GET /api/v1/rag/models``. When ``MODEL_CONFIG_SOURCE=db``, MySQL
``llm_models`` is authoritative and an empty/unavailable catalog fails closed;
legacy YAML fallback is used only when database model configuration is disabled.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_DB_CACHE_TTL_SECONDS = 30.0
_DB_FAILURE_BACKOFF_SECONDS = 60.0
_db_rows_cache: tuple[float, list] | None = None
_db_non_chat_names_cache: tuple[float, frozenset] | None = None
_db_unavailable_until = 0.0
_db_cache_lock = threading.RLock()


def invalidate_model_catalog_cache() -> None:
    """Invalidate the frontend/default chat-model catalog immediately."""

    global _db_rows_cache, _db_unavailable_until
    with _db_cache_lock:
        _db_rows_cache = None
        _db_non_chat_names_cache = None
        _db_unavailable_until = 0.0


def _fetch_active_chat_llm_models():
    """Return active chat-scope LlmModel rows ordered by sort_order, or []."""
    global _db_rows_cache, _db_unavailable_until

    config_source = os.environ.get("MODEL_CONFIG_SOURCE", "db").lower()
    if config_source != "db":
        return []

    now = time.monotonic()
    with _db_cache_lock:
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
            with _db_cache_lock:
                _db_rows_cache = (time.monotonic(), rows)
                _db_unavailable_until = 0.0
            return rows
        finally:
            session.close()
    except Exception as e:
        with _db_cache_lock:
            _db_unavailable_until = time.monotonic() + _DB_FAILURE_BACKOFF_SECONDS
        logger.warning("llm_models DB unavailable; managed catalog fails closed: %s", e)
        return []


def _fetch_active_non_chat_model_names() -> frozenset[str]:
    """Return names that are active in any non-chat scope (cached)."""
    global _db_non_chat_names_cache, _db_unavailable_until

    config_source = os.environ.get("MODEL_CONFIG_SOURCE", "db").lower()
    if config_source != "db":
        return frozenset()
    now = time.monotonic()
    with _db_cache_lock:
        if _db_non_chat_names_cache and now - _db_non_chat_names_cache[0] < _DB_CACHE_TTL_SECONDS:
            return _db_non_chat_names_cache[1]
        if now < _db_unavailable_until:
            return frozenset()
    try:
        from sqlalchemy import select

        from backend.db.connection import get_session
        from backend.db.models.llm_model import LlmModel

        session = get_session()
        try:
            stmt = (
                select(LlmModel.name)
                .where(
                    LlmModel.usage_scope != "chat",
                    LlmModel.is_active == True,
                )
                .distinct()
            )
            result = session.execute(stmt)
            names = frozenset(str(name) for name in result.scalars().all())
            with _db_cache_lock:
                _db_non_chat_names_cache = (time.monotonic(), names)
                _db_unavailable_until = 0.0
            return names
        finally:
            session.close()
    except Exception as e:
        with _db_cache_lock:
            _db_unavailable_until = time.monotonic() + _DB_FAILURE_BACKOFF_SECONDS
        logger.warning("llm_models non-chat scope lookup unavailable: %s", e)
        return frozenset()


def get_available_model_info() -> list[dict]:
    """Return available model info dicts exposed to frontend selectors."""
    rows = _fetch_active_chat_llm_models()
    if rows or os.environ.get("MODEL_CONFIG_SOURCE", "db").lower() == "db":
        excluded = _fetch_active_non_chat_model_names()
        return [
            {
                "name": m.name,
                "display_name": m.display_name or m.name,
                "supports_vision": bool(getattr(m, "supports_vision", False)),
                "supports_thinking": bool(getattr(m, "supports_thinking", False)),
            }
            for m in rows
            if m.name not in excluded
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
    """Return the default chat model name without crossing catalog sources."""
    rows = _fetch_active_chat_llm_models()
    if rows:
        for m in rows:
            if m.is_default:
                return m.name
        return rows[0].name
    if os.environ.get("MODEL_CONFIG_SOURCE", "db").lower() == "db":
        return None

    try:
        from backend.core.config import get_app_config

        config = get_app_config()
        if config.models:
            return config.models[0].name
    except Exception:
        pass
    return None
