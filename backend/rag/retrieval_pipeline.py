"""Application-neutral RAG retrieval pipeline shared by HTTP and agent tools."""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from typing import Any

from backend.config.loggers import get_logger
from backend.rag.content_filter import (
    FilterMode,
    FilteredData,
    RawRetrievalData,
    get_content_filter_manager,
)
from backend.rag.keywords.models import QueryType
from backend.config.settings import settings
from backend.domain.knowledge_base.backend_policy import (
    BLOCKED_BACKEND,
    HYBRID_RETRIEVAL_BACKEND,
    LEGACY_BACKEND,
    SELF_BACKEND,
    resolve_retrieval_backend,
)

from backend.rag.service import get_rag_service

logger = get_logger(__name__)

_STAGE1_TOP_K_MULTIPLIER = 1.5
_STAGE1_CHUNK_TOP_K_MULTIPLIER = 5
_STAGE1_MAX_TOP_K = 80
_STAGE1_MAX_CHUNK_TOP_K = 60
_USE_DIRECT_RETRIEVER = os.getenv("USE_DIRECT_RETRIEVER", "true").lower() == "true"
_CACHE_MAXSIZE = max(1, int(os.getenv("RAG_RETRIEVAL_CACHE_MAXSIZE", "128") or "128"))
_DIRECT_RETRIEVER_CACHE: OrderedDict[str, Any] = OrderedDict()
_EMBEDDING_FUNC_CACHE: OrderedDict[str, Any] = OrderedDict()


def _cache_get(cache: OrderedDict[str, Any], key: str) -> Any | None:
    value = cache.get(key)
    if value is not None:
        cache.move_to_end(key)
    return value


def _cache_put(cache: OrderedDict[str, Any], key: str, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_MAXSIZE:
        cache.popitem(last=False)


def ensure_query_type(value: QueryType | str | None) -> QueryType:
    if isinstance(value, QueryType):
        return value
    if isinstance(value, str):
        try:
            return QueryType(value.strip().lower())
        except Exception:
            pass
    return QueryType.GENERAL


def compute_stage1_recall_window(top_k: int, chunk_top_k: int) -> tuple[int, int]:
    requested_top_k = max(1, int(top_k or 1))
    requested_chunk_top_k = max(1, int(chunk_top_k or 1))
    return (
        int(min(_STAGE1_MAX_TOP_K, max(requested_top_k, requested_top_k * _STAGE1_TOP_K_MULTIPLIER))),
        int(
            min(
                _STAGE1_MAX_CHUNK_TOP_K,
                max(requested_chunk_top_k, requested_chunk_top_k * _STAGE1_CHUNK_TOP_K_MULTIPLIER),
            )
        ),
    )


def clamp_filtered_chunks(filtered_data: FilteredData | None, requested_chunk_top_k: int) -> int:
    if filtered_data is None:
        return 0
    limit = max(1, int(requested_chunk_top_k or 1))
    before = len(filtered_data.chunks)
    if before > limit:
        filtered_data.chunks = filtered_data.chunks[:limit]
        logger.info("[检索] 最终 chunk 收敛: %s -> %s", before, len(filtered_data.chunks))
    return len(filtered_data.chunks)


def _chunk_is_image(chunk: dict[str, Any]) -> bool:
    content = str((chunk or {}).get("content") or "")
    return (
        "Image Content Analysis:" in content
        or "Image Path:" in content
        or "媒体类型: image" in content
    )


def _chunk_rank_score(chunk: dict[str, Any]) -> float:
    for key in ("_fused_score", "_chunk_score", "_rerank_score", "_ppr_score", "_coverage_score"):
        try:
            value = float((chunk or {}).get(key) or 0.0)
        except Exception:
            value = 0.0
        if value:
            return value
    return 0.0


def _query_has_image_intent(query: str) -> bool:
    try:
        from backend.rag.retriever_v2.direct_retriever import _is_image_query

        return bool(_is_image_query(query))
    except Exception:
        value = str(query or "")
        return any(marker in value for marker in ("图片", "图像", "示意图", "分类图", "模式图", "的图"))


def _order_final_chunks_for_query(filtered_data: FilteredData | None, query: str) -> None:
    if filtered_data is None or not filtered_data.chunks:
        return
    if _query_has_image_intent(query):
        return
    if not any(_chunk_is_image(chunk) for chunk in filtered_data.chunks):
        return
    filtered_data.chunks.sort(
        key=lambda chunk: (
            1 if _chunk_is_image(chunk) else 0,
            -_chunk_rank_score(chunk),
        )
    )


async def extract_keywords(
    kb_id: str,
    query: str,
    external_query_type: QueryType | str | None = None,
    precomputed_query_embedding: Any | None = None,
) -> tuple[list[str], list[str], QueryType, dict[str, float]]:
    if threading.current_thread().name.startswith("subagent-exec-"):
        return [], [], QueryType.GENERAL, {}
    try:
        from backend.db.connection import get_async_session_factory
        from backend.rag.knowledge_base.manager import get_kb_manager

        rag = await get_rag_service()._get_rag_instance(kb_id)
        embedding_func = rag.lightrag.embedding_func if rag and rag.lightrag else None
        factory = get_async_session_factory()
        async with factory() as session:
            extractor = await get_kb_manager().get_keyword_extractor(
                session,
                kb_id,
                embedding_func,
            )
            result = await extractor.extract(
                query,
                external_query_type=external_query_type,
                precomputed_query_embedding=precomputed_query_embedding,
            )
        return (
            list(result.high_level_keywords or []),
            list(result.low_level_keywords or []),
            result.query_type,
            dict(result.weighted_seed_scores or {}),
        )
    except Exception as exc:
        logger.warning("[关键词提取] 失败: %s", exc)
        return [], [], QueryType.GENERAL, {}


async def get_kb_info(kb_id: str) -> tuple[str, str]:
    try:
        from backend.application.knowledge_bases.catalog_service import (
            load_knowledge_base_catalog,
        )

        entries = await load_knowledge_base_catalog([kb_id])
        if entries:
            return entries[0].kb_code, entries[0].kb_name
    except Exception as exc:
        logger.warning("[知识库信息] 获取失败: %s", exc)
    return kb_id, kb_id


async def get_default_kb_id() -> str | None:
    try:
        from backend.application.knowledge_bases.catalog_service import (
            load_knowledge_base_catalog,
        )

        entries = await load_knowledge_base_catalog(defaults_only=True)
        if entries:
            return entries[0].kb_id
    except Exception as exc:
        logger.warning("[默认知识库] 获取失败: %s", exc)
    return None


async def _stage1_recall(
    query: str,
    hl_keywords: list[str],
    ll_keywords: list[str],
    kb_id: str,
    *,
    top_k: int,
    chunk_top_k: int,
    query_type: QueryType,
) -> RawRetrievalData:
    del query_type
    raw_data = RawRetrievalData()
    try:
        from lightrag import QueryParam

        rag = await get_rag_service()._get_rag_instance(kb_id)
        if not rag or not rag.lightrag:
            return raw_data
        result = await rag.lightrag.aquery_data(
            query,
            param=QueryParam(
                mode="mix",
                stream=False,
                only_need_context=True,
                top_k=top_k,
                chunk_top_k=chunk_top_k,
                max_entity_tokens=7000,
                max_relation_tokens=7000,
                max_total_tokens=50000,
                hl_keywords=hl_keywords,
                ll_keywords=ll_keywords,
                enable_rerank=False,
            ),
        )
        if result and result.get("status") == "success":
            data = result.get("data", {})
            raw_data.entities = data.get("entities", [])
            raw_data.relationships = data.get("relationships", [])
            raw_data.chunks = data.get("chunks", [])
            raw_data.references = data.get("references", [])
            embedding_func = getattr(rag.lightrag, "embedding_func", None)
            if raw_data.chunks and embedding_func is not None:
                try:
                    contents = [chunk.get("content", "") for chunk in raw_data.chunks]
                    embeddings = await embedding_func(contents)
                    for chunk, embedding in zip(raw_data.chunks, embeddings, strict=False):
                        chunk["_embedding"] = embedding
                    logger.info(
                        "[阶段1] 已获取 %s 个 chunk 的 embedding，供 MMR 复用",
                        len(raw_data.chunks),
                    )
                except Exception as exc:
                    logger.warning("[阶段1] 获取 chunk embedding 失败，MMR 将实时计算: %s", exc)
    except Exception as exc:
        logger.error("[阶段1] 召回异常: %s", exc, exc_info=True)
    return raw_data


async def _get_direct_retriever(kb_id: str, kb_code: str):
    cache_key = f"{kb_id}:{kb_code}"
    cached = _cache_get(_DIRECT_RETRIEVER_CACHE, cache_key)
    if cached is not None:
        return cached
    try:
        from backend.db.connection import get_async_session_factory
        from backend.rag.knowledge_base.manager import get_kb_manager
        from backend.rag.retrieval_profile import (
            build_direct_retriever_config_for_profile,
            resolve_retrieval_profile,
        )
        from backend.rag.retriever.direct_retriever import build_direct_retriever

        profile = resolve_retrieval_profile(kb_id=kb_id, kb_code=kb_code)
        rag = await get_rag_service()._get_rag_instance(kb_id)
        if not rag or not rag.lightrag:
            return None
        async with get_async_session_factory()() as session:
            extractor = await get_kb_manager().get_keyword_extractor(
                session,
                kb_id,
                embedding_func=getattr(rag.lightrag, "embedding_func", None),
            )
            keyword_index = await extractor._get_index()
        retriever = await build_direct_retriever(
            kb_code=kb_code,
            kb_id=kb_id,
            rag_instance=rag,
            keyword_index=keyword_index,
            config=build_direct_retriever_config_for_profile(profile),
        )
        if retriever is not None:
            _cache_put(_DIRECT_RETRIEVER_CACHE, cache_key, retriever)
        return retriever
    except Exception as exc:
        logger.warning("[DirectRetriever] 构建失败，将回退 LightRAG: %s", exc)
        return None


async def get_direct_retriever(kb_id: str, kb_code: str):
    """Public compatibility hook for startup prewarm callers."""
    return await _get_direct_retriever(kb_id, kb_code)


async def _stage1_direct(
    *,
    query: str,
    hl_keywords: list[str],
    ll_keywords: list[str],
    kb_id: str,
    kb_code: str,
    query_type: QueryType,
    weighted_seed_scores: dict[str, float],
    top_k: int,
    chunk_top_k: int,
    query_embedding: Any | None,
) -> tuple[RawRetrievalData, dict[str, float]]:
    retriever = await _get_direct_retriever(kb_id, kb_code)
    if retriever is None:
        return (
            await _stage1_recall(
                query,
                hl_keywords,
                ll_keywords,
                kb_id,
                top_k=top_k,
                chunk_top_k=chunk_top_k,
                query_type=query_type,
            ),
            weighted_seed_scores,
        )
    try:
        import numpy as np

        embedding = None
        if query_embedding is not None:
            embedding = np.array(query_embedding, dtype=np.float32)
        return await retriever.retrieve(
            query=query,
            hl_keywords=hl_keywords,
            ll_keywords=ll_keywords,
            query_type=query_type,
            weighted_seed_scores=weighted_seed_scores,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            query_embedding=embedding,
        )
    except Exception as exc:
        logger.error("[DirectRetriever] 召回异常，回退 LightRAG: %s", exc, exc_info=True)
        return (
            await _stage1_recall(
                query,
                hl_keywords,
                ll_keywords,
                kb_id,
                top_k=top_k,
                chunk_top_k=chunk_top_k,
                query_type=query_type,
            ),
            weighted_seed_scores,
        )


async def _resolve_kb_retrieval_backend(kb_id: str) -> str:
    try:
        from backend.db.connection import get_async_session_factory
        from backend.rag.knowledge_base.manager import get_kb_manager

        factory = get_async_session_factory()
        async with factory() as session:
            kb = await get_kb_manager().get_knowledge_base(session, kb_id)
            if kb is None:
                return LEGACY_BACKEND
            return resolve_retrieval_backend(settings, kb=kb)
    except Exception as exc:
        logger.warning("[检索路由] 解析 retrieval_backend 失败，回退 legacy: %s", exc)
        return LEGACY_BACKEND


async def _stage1_self_direct(
    *,
    query: str,
    kb_id: str,
    kb_code: str,
    chunk_top_k: int,
) -> tuple[RawRetrievalData, dict[str, float]]:
    from backend.db.connection import get_async_session_factory
    from backend.rag.retriever_v2 import SelfDirectRetrieverV2

    raw_data = RawRetrievalData()
    async with get_async_session_factory()() as session:
        retriever = SelfDirectRetrieverV2(
            session=session,
            kb_id=kb_id,
            kb_code=kb_code,
            graph_profile=settings.self_graph_retrieval_profile,
        )
        result = await retriever.retrieve(query, top_k=chunk_top_k)
        raw_data = result.to_raw_data()
    logger.info(
        "[SelfDirectRetrieverV2] 完成: kb=%s, entities=%d, relationships=%d, chunks=%d",
        kb_code,
        len(raw_data.entities),
        len(raw_data.relationships),
        len(raw_data.chunks),
    )
    return raw_data, {}


def _merge_hybrid_raw_data(
    legacy_data: RawRetrievalData,
    self_data: RawRetrievalData,
) -> RawRetrievalData:
    from backend.rag.retriever_v2 import HybridRetriever

    legacy_items = HybridRetriever._normalize_backend_items(
        legacy_data.chunks,
        backend=LEGACY_BACKEND,
        source_index_default="legacy",
        weight=0.9,
    )
    self_items = HybridRetriever._normalize_backend_items(
        self_data.chunks,
        backend=SELF_BACKEND,
        source_index_default="chunks_v2",
        weight=1.0,
    )
    merged_chunks = HybridRetriever.merge_ranked_results(legacy_items, self_items)
    merged_chunks.sort(key=lambda item: -float(item.get("_hybrid_score") or 0.0))
    return RawRetrievalData(
        entities=list(legacy_data.entities) + list(self_data.entities),
        relationships=list(legacy_data.relationships) + list(self_data.relationships),
        chunks=merged_chunks,
        query_embedding=legacy_data.query_embedding or self_data.query_embedding,
    )


async def _stage1_hybrid_direct(
    *,
    query: str,
    hl_keywords: list[str],
    ll_keywords: list[str],
    kb_id: str,
    kb_code: str,
    query_type: QueryType,
    weighted_seed_scores: dict[str, float],
    top_k: int,
    chunk_top_k: int,
    query_embedding: Any | None,
) -> tuple[RawRetrievalData, dict[str, float]]:
    legacy_data, legacy_weights = await _stage1_direct(
        query=query,
        hl_keywords=hl_keywords,
        ll_keywords=ll_keywords,
        kb_id=kb_id,
        kb_code=kb_code,
        query_type=query_type,
        weighted_seed_scores=weighted_seed_scores,
        top_k=top_k,
        chunk_top_k=chunk_top_k,
        query_embedding=query_embedding,
    )
    self_data, self_weights = await _stage1_self_direct(
        query=query,
        kb_id=kb_id,
        kb_code=kb_code,
        chunk_top_k=chunk_top_k,
    )
    merged = _merge_hybrid_raw_data(legacy_data, self_data)
    logger.info(
        "[HybridRetriever] 完成: kb=%s, legacy_chunks=%d, self_chunks=%d, merged=%d",
        kb_code,
        len(legacy_data.chunks),
        len(self_data.chunks),
        len(merged.chunks),
    )
    return merged, {**legacy_weights, **self_weights}


async def _get_embedding_func_for_kb(kb_id: str):
    cached = _cache_get(_EMBEDDING_FUNC_CACHE, kb_id)
    if cached is not None:
        return cached
    try:
        rag = await get_rag_service()._get_rag_instance(kb_id)
        if rag and rag.lightrag and hasattr(rag.lightrag, "embedding_func"):
            _cache_put(_EMBEDDING_FUNC_CACHE, kb_id, rag.lightrag.embedding_func)
            return rag.lightrag.embedding_func
    except Exception as exc:
        logger.warning("[embedding] 获取 embedding_func 失败: %s", exc)
    return None


async def _extract_images_from_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from backend.application.image_pipeline import normalize_image_url
    from backend.rag.media_blocks import iter_image_blocks

    images: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for chunk in chunks:
        for block in iter_image_blocks(chunk.get("content", "")):
            normalized = normalize_image_url(block.get("image_path", "").strip())
            if not normalized or normalized in seen_paths:
                continue
            seen_paths.add(normalized)
            images.append(
                {
                    "marker": f"IMAGE_{len(images) + 1}",
                    "url": normalized,
                    "path": normalized,
                    "caption": str(block.get("caption") or "").strip(),
                    "footnote": str(block.get("footnote") or "").strip(),
                    "visual_analysis": str(block.get("visual_analysis") or "").strip(),
                    "media_ref": str(block.get("media_ref") or "").strip(),
                    "media_title": str(block.get("media_title") or "").strip(),
                    "ocr_text": str(block.get("ocr_text") or "").strip(),
                }
            )
    return images


async def retrieve_context(
    query: str,
    kb_id: str | None = None,
    top_k: int = 30,
    chunk_top_k: int = 10,
    external_hl_keywords: list[str] | None = None,
    external_ll_keywords: list[str] | None = None,
    external_query_type: str | QueryType | None = None,
    seed_weights: dict[str, float] | None = None,
    *,
    disable_stage1_oversample: bool = False,
    precomputed_query_embedding: Any | None = None,
    content_filter_mode: FilterMode | None = None,
    retrieval_profile_override: str | None = None,
) -> tuple[FilteredData, str, list[dict[str, Any]], dict[str, int]]:
    from backend.rag.retrieval_profile import resolve_retrieval_profile

    kb_id = kb_id or await get_default_kb_id()
    if not kb_id:
        raise ValueError("未指定知识库且无法获取默认知识库")
    kb_code, _kb_name = await get_kb_info(kb_id)
    profile = resolve_retrieval_profile(
        kb_id=kb_id,
        kb_code=kb_code,
        override=retrieval_profile_override,
    )
    effective_filter_mode = content_filter_mode or profile.content_filter_mode
    effective_disable_oversample = disable_stage1_oversample or profile.disable_stage1_oversample
    effective_seed_weights = dict(seed_weights or {})
    retrieval_backend = await _resolve_kb_retrieval_backend(kb_id)
    if retrieval_backend == BLOCKED_BACKEND:
        raise RuntimeError(
            "知识库配置为 self_ingestion/hybrid 检索，但全局开关、知识库开关或白名单未满足，"
            "已阻塞以避免回退 legacy_lightrag 造成新旧索引串线"
        )

    if (
        retrieval_backend == SELF_BACKEND
        and external_hl_keywords is None
        and external_ll_keywords is None
    ) or (
        retrieval_backend == SELF_BACKEND
        and not (external_hl_keywords or [])
        and not (external_ll_keywords or [])
    ):
        hl_keywords = []
        ll_keywords = []
        query_type = ensure_query_type(external_query_type)
    elif external_hl_keywords is not None or external_ll_keywords is not None:
        hl_keywords = list(external_hl_keywords or [])
        ll_keywords = list(external_ll_keywords or [])
        query_type = ensure_query_type(external_query_type or "general")
        if not effective_seed_weights:
            _, _, _, effective_seed_weights = await extract_keywords(
                kb_id,
                query,
                external_query_type=query_type,
                precomputed_query_embedding=precomputed_query_embedding,
            )
    else:
        hl_keywords, ll_keywords, query_type, extracted_weights = await extract_keywords(
            kb_id,
            query,
            external_query_type=external_query_type,
            precomputed_query_embedding=precomputed_query_embedding,
        )
        query_type = ensure_query_type(query_type)
        if not effective_seed_weights:
            effective_seed_weights = extracted_weights

    if effective_disable_oversample:
        stage1_top_k = max(1, int(top_k or 1))
        stage1_chunk_top_k = max(1, int(chunk_top_k or 1))
    else:
        stage1_top_k, stage1_chunk_top_k = compute_stage1_recall_window(top_k, chunk_top_k)

    if retrieval_backend == SELF_BACKEND:
        raw_data, effective_seed_weights = await _stage1_self_direct(
            query=query,
            kb_id=kb_id,
            kb_code=kb_code,
            chunk_top_k=stage1_chunk_top_k,
        )
    elif retrieval_backend == HYBRID_RETRIEVAL_BACKEND and _USE_DIRECT_RETRIEVER:
        raw_data, effective_seed_weights = await _stage1_hybrid_direct(
            query=query,
            hl_keywords=hl_keywords,
            ll_keywords=ll_keywords,
            kb_id=kb_id,
            kb_code=kb_code,
            query_type=query_type,
            weighted_seed_scores=effective_seed_weights,
            top_k=stage1_top_k,
            chunk_top_k=stage1_chunk_top_k,
            query_embedding=precomputed_query_embedding,
        )
    elif _USE_DIRECT_RETRIEVER:
        raw_data, effective_seed_weights = await _stage1_direct(
            query=query,
            hl_keywords=hl_keywords,
            ll_keywords=ll_keywords,
            kb_id=kb_id,
            kb_code=kb_code,
            query_type=query_type,
            weighted_seed_scores=effective_seed_weights,
            top_k=stage1_top_k,
            chunk_top_k=stage1_chunk_top_k,
            query_embedding=precomputed_query_embedding,
        )
    else:
        raw_data = await _stage1_recall(
            query,
            hl_keywords,
            ll_keywords,
            kb_id,
            top_k=stage1_top_k,
            chunk_top_k=stage1_chunk_top_k,
            query_type=query_type,
        )

    # Self-ingestion retrieval already returns the chunk candidates needed by
    # the filter and does not depend on a legacy LightRAG instance.  Looking up
    # its embedding function here can initialize that legacy instance anyway;
    # worse, a previously cancelled initialization task then aborts an otherwise
    # successful self-index query with ``CancelledError``.
    embedding_func = (
        None
        if retrieval_backend == SELF_BACKEND
        else await _get_embedding_func_for_kb(kb_id)
    )
    content_filter = await get_content_filter_manager().get_filter(
        kb_code=kb_code,
        kb_id=kb_id,
        embedding_func=embedding_func,
    )
    filter_result = await content_filter.filter_content(
        raw_data=raw_data,
        hl_keywords=hl_keywords,
        query=query,
        query_type=query_type,
        seed_weights=effective_seed_weights,
        mode=effective_filter_mode,
    )
    _order_final_chunks_for_query(filter_result.filtered_data, query)
    final_chunk_count = clamp_filtered_chunks(filter_result.filtered_data, chunk_top_k)
    filter_result.stats["chunks"] = final_chunk_count
    images = await _extract_images_from_chunks(
        filter_result.filtered_data.chunks if filter_result.filtered_data else []
    )
    return (
        filter_result.filtered_data,
        filter_result.prompt,
        images,
        {
            "entities": filter_result.stats.get("entities", 0),
            "relationships": filter_result.stats.get("relationships", 0),
            "chunks": final_chunk_count,
        },
    )
