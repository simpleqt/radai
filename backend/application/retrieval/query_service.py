"""Application-facing retrieval use cases.

API and startup code should depend on this module instead of RAG runtime
internals.  The functions here are intentionally thin for now, but they give the
HTTP layer a stable use-case boundary while the runtime continues to be split.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def get_default_kb_id() -> str | None:
    from backend.rag.retrieval_pipeline import get_default_kb_id as _impl

    return await _impl()


async def get_direct_retriever(kb_id: str, kb_code: str) -> Any:
    from backend.rag.retrieval_pipeline import get_direct_retriever as _impl

    return await _impl(kb_id, kb_code)


async def retrieve_context(
    *,
    query: str,
    kb_id: str,
    top_k: int,
    chunk_top_k: int,
    external_hl_keywords: list[str] | None = None,
    external_ll_keywords: list[str] | None = None,
    external_query_type: str | None = None,
    disable_stage1_oversample: bool = False,
) -> tuple[Any, str, list[dict[str, Any]], dict[str, int]]:
    from backend.rag.retrieval_pipeline import retrieve_context as _impl

    return await _impl(
        query=query,
        kb_id=kb_id,
        top_k=top_k,
        chunk_top_k=chunk_top_k,
        external_hl_keywords=external_hl_keywords,
        external_ll_keywords=external_ll_keywords,
        external_query_type=external_query_type,
        disable_stage1_oversample=disable_stage1_oversample,
    )


async def execute_internal_multi_kb_search(
    *,
    query: str,
    kb_ids: list[str],
    top_k: int,
    chunk_top_k: int,
    merge_strategy: str,
    external_hl_keywords: list[str] | None = None,
    external_ll_keywords: list[str] | None = None,
    external_query_type: str | None = None,
    content_filter_mode: str | None = None,
    max_result_chars: int | None = None,
) -> dict[str, Any]:
    from backend.application.agent_executors.image_url_registry import get_registry
    from backend.application.tools.providers.multi_kb_search_tool import (
        execute_multi_kb_search_structured,
    )
    from backend.rag.content_filter import FilterMode

    try:
        parsed_filter_mode = (
            FilterMode(content_filter_mode) if content_filter_mode else None
        )
    except ValueError as exc:
        raise ValueError("Unsupported content_filter_mode") from exc

    result = await execute_multi_kb_search_structured(
        query=query,
        kb_ids=kb_ids,
        top_k=top_k,
        chunk_top_k=chunk_top_k,
        merge_strategy=merge_strategy,
        external_hl_keywords=external_hl_keywords,
        external_ll_keywords=external_ll_keywords,
        external_query_type=external_query_type,
        content_filter_mode=parsed_filter_mode,
        max_result_chars=max_result_chars,
        _force_local=True,
    )
    registry = get_registry()
    result["registered_images"] = [
        {
            "token": token,
            "url": entry.url,
            "caption": entry.caption,
        }
        for token, entry in registry.iter_entries()
    ]
    return result


async def list_chat_models() -> dict[str, Any]:
    """Return active chat models, falling back to static settings."""
    from backend.config.settings import settings
    from backend.db.connection import get_async_session_factory
    from backend.db.models.llm_model import LlmModel
    from sqlalchemy import select

    session_factory = get_async_session_factory()
    try:
        async with session_factory() as session:
            stmt = (
                select(LlmModel)
                .where(
                    LlmModel.usage_scope == "chat",
                    LlmModel.is_active == True,
                )
                .order_by(LlmModel.sort_order)
            )
            result = await session.execute(stmt)
            db_models = result.scalars().all()
            if db_models:
                return {
                    "models": [
                        {
                            "id": model.name,
                            "name": model.display_name or model.name,
                            "provider": model.provider,
                            "description": model.description,
                        }
                        for model in db_models
                    ],
                    "default_model": next(
                        (model.name for model in db_models if model.is_default),
                        db_models[0].name,
                    ),
                }
    except Exception as exc:
        logger.warning("从数据库读取可用模型失败: %s", exc)

    models = settings.get_available_models()
    return {
        "models": [
            {
                "id": model.get("id", ""),
                "name": model.get("name", model.get("id", "")),
                "provider": model.get("provider", "custom"),
                "description": model.get("description"),
            }
            for model in models
        ],
        "default_model": settings.dashscope_model or "qwen3-max",
    }


async def get_rag_health() -> dict[str, Any]:
    from backend.rag.service import get_rag_service

    service = get_rag_service()
    try:
        await service.initialize()
        return {"status": "healthy"}
    except Exception as exc:
        return {"status": "unhealthy", "error": str(exc)}


async def insert_content_list(
    *,
    content_list: list[dict[str, Any]],
    file_path: str,
    kb_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    from backend.rag.service import get_rag_service

    service = get_rag_service()
    await service.initialize()
    return await service.insert_content_list(
        content_list=content_list,
        file_path=file_path,
        kb_id=kb_id,
        **kwargs,
    )


async def process_multimodal_document(
    *,
    file_path: str,
    kb_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    from backend.rag.service import get_rag_service

    service = get_rag_service()
    await service.initialize()
    return await service.process_multimodal(
        file_path=file_path,
        kb_id=kb_id,
        **kwargs,
    )


async def insert_docx_document(
    *,
    file_path: str,
    kb_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    from backend.rag.service import get_rag_service

    service = get_rag_service()
    await service.initialize()
    return await service.insert_docx(
        file_path=file_path,
        kb_id=kb_id,
        **kwargs,
    )


async def reset_rag_runtime(
    *,
    new_work_dir: str | None = None,
    clear_old_work_dir: bool = True,
) -> dict[str, Any]:
    from backend.rag.service import get_rag_service

    service = get_rag_service()
    return await service.reset(
        new_work_dir=new_work_dir,
        clear_old_work_dir=clear_old_work_dir,
    )


async def delete_rag_document(
    *,
    doc_id: str,
    delete_llm_cache: bool = False,
    kb_id: str | None = None,
) -> dict[str, Any]:
    from backend.rag.service import get_rag_service

    service = get_rag_service()
    return await service.delete_doc_by_id(
        doc_id=doc_id,
        delete_llm_cache=delete_llm_cache,
        kb_id=kb_id,
    )


__all__ = [
    "delete_rag_document",
    "execute_internal_multi_kb_search",
    "get_default_kb_id",
    "get_direct_retriever",
    "get_rag_health",
    "insert_content_list",
    "insert_docx_document",
    "list_chat_models",
    "process_multimodal_document",
    "reset_rag_runtime",
    "retrieve_context",
]
