"""Authoritative frontend-chat model selection.

The public chat surface may only select active ``usage_scope='chat'`` rows.
Internal ingestion and image-analysis models are deliberately invisible here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from sqlalchemy import select

from backend.db.connection import get_async_session_factory
from backend.db.models.llm_model import LlmModel

logger = logging.getLogger(__name__)


class ChatModelCatalogUnavailable(RuntimeError):
    """The authoritative model catalog could not be read."""


class ChatModelNotAvailable(ValueError):
    """The requested model is not an active frontend-chat model."""


@dataclass(frozen=True)
class ChatModelSelection:
    name: str
    supports_vision: bool


async def resolve_active_chat_model(
    name: str | None = None,
) -> ChatModelSelection:
    """Resolve a selected/default chat model and fail closed across scopes."""

    requested = str(name or "").strip() or None
    if os.getenv("MODEL_CONFIG_SOURCE", "db").strip().lower() != "db":
        return await asyncio.to_thread(_resolve_from_legacy_config, requested)

    try:
        factory = get_async_session_factory()
        async with factory() as session:
            stmt = select(LlmModel).where(
                LlmModel.usage_scope == "chat",
                LlmModel.is_active.is_(True),
            )
            if requested:
                stmt = stmt.where(LlmModel.name == requested)
            else:
                stmt = stmt.order_by(
                    LlmModel.is_default.desc(),
                    LlmModel.sort_order,
                    LlmModel.id,
                )
            row = await session.scalar(stmt.limit(1))
    except Exception as exc:  # noqa: BLE001 - mapped to a public 503 by the API
        logger.exception("Active chat-model catalog query failed")
        raise ChatModelCatalogUnavailable("模型目录暂不可用") from exc

    if row is None:
        raise ChatModelNotAvailable("所选模型不可用于前端对话")
    return ChatModelSelection(
        name=str(row.name),
        supports_vision=bool(row.supports_vision),
    )


def _resolve_from_legacy_config(name: str | None) -> ChatModelSelection:
    """Compatibility path used only when DB model configuration is disabled."""

    try:
        from backend.core.config import get_app_config

        config = get_app_config()
        model = config.get_model_config(name) if name else (config.models[0] if config.models else None)
    except Exception as exc:  # noqa: BLE001
        raise ChatModelCatalogUnavailable("模型目录暂不可用") from exc
    if model is None:
        raise ChatModelNotAvailable("所选模型不可用于前端对话")
    return ChatModelSelection(
        name=str(model.name),
        supports_vision=bool(getattr(model, "supports_vision", False)),
    )
