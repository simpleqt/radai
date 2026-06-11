"""ASR 模型配置加载（MySQL llm_models, usage_scope=asr）。"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def load_asr_config_from_db(*, name: str | None = None) -> dict[str, Any] | None:
    """从 MySQL 加载 ASR 模型配置；未指定 name 时取默认启用模型。"""
    try:
        from sqlalchemy import select

        from backend.db.connection import get_session
        from backend.db.models.llm_model import LlmModel

        session = get_session()
        try:
            stmt = (
                select(LlmModel)
                .where(LlmModel.usage_scope == "asr", LlmModel.is_active == True)
                .order_by(LlmModel.is_default.desc(), LlmModel.sort_order)
            )
            if name:
                stmt = stmt.where(LlmModel.name == name)
            stmt = stmt.limit(1)
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return None

            extra = row.extra_body if isinstance(row.extra_body, dict) else {}
            return {
                "name": row.name,
                "display_name": row.display_name,
                "model": row.model,
                "api_key": row.resolve_api_key(),
                "base_url": row.resolve_base_url(),
                "request_timeout": row.request_timeout or 120.0,
                "default_language": extra.get("default_language", "zh"),
            }
        finally:
            session.close()
    except Exception as exc:
        logger.debug("load asr config from DB failed: %s", exc)
    return None
