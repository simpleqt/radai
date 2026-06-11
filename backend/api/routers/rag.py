"""RAG 路由"""

import asyncio
import json
import os
import threading
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.config.loggers import get_logger
from backend.rag.content_filter import (
    FilterMode,
    FilteredData,
    RawRetrievalData,
    get_content_filter_manager,
)
from backend.rag.keywords.models import QueryType
from backend.rag.service import get_rag_service

router = APIRouter()
logger = get_logger(__name__)

_DEFAULT_KB_ID_CACHE: str | None = None

# Known KB metadata to avoid DB round-trips in tool/subagent worker threads.
# 通过环境变量 KB_INFO_MAP 配置，格式: "kb_id1=code1|name1,kb_id2=code2|name2"
_KNOWN_KB_INFO: dict[str, tuple[str, str]] = {
    **{
        item.split("=", 1)[0]: tuple(item.split("=", 1)[1].split("|"))
        for item in os.getenv("KB_INFO_MAP", "").split(",")
        if "=" in item and "|" in item.split("=", 1)[1]
    },
}

# 阶段1采用过采样召回，给后续 PPR/Rerank/MMR 留出可筛选空间。
_STAGE1_TOP_K_MULTIPLIER = 1.5
_STAGE1_CHUNK_TOP_K_MULTIPLIER = 5
_STAGE1_MAX_TOP_K = 80
_STAGE1_MAX_CHUNK_TOP_K = 60


def _ensure_query_type(value: QueryType | str | None) -> QueryType:
    """将外部 query_type 统一归一化为 QueryType 枚举。"""
    if isinstance(value, QueryType):
        return value
    if isinstance(value, str):
        try:
            return QueryType(value.strip().lower())
        except Exception:
            return QueryType.GENERAL
    return QueryType.GENERAL


def _compute_stage1_recall_window(top_k: int, chunk_top_k: int) -> tuple[int, int]:
    requested_top_k = max(1, int(top_k or 1))
    requested_chunk_top_k = max(1, int(chunk_top_k or 1))

    stage1_top_k = int(min(_STAGE1_MAX_TOP_K, max(requested_top_k, requested_top_k * _STAGE1_TOP_K_MULTIPLIER)))
    stage1_chunk_top_k = int(min(
        _STAGE1_MAX_CHUNK_TOP_K,
        max(requested_chunk_top_k, requested_chunk_top_k * _STAGE1_CHUNK_TOP_K_MULTIPLIER),
    ))
    return stage1_top_k, stage1_chunk_top_k


def _clamp_filtered_chunks(filtered_data: FilteredData | None, requested_chunk_top_k: int) -> int:
    if filtered_data is None:
        return 0

    limit = max(1, int(requested_chunk_top_k or 1))
    before = len(filtered_data.chunks)
    if before > limit:
        filtered_data.chunks = filtered_data.chunks[:limit]
        logger.info("[检索] 最终 chunk 收敛: %s -> %s", before, len(filtered_data.chunks))
    return len(filtered_data.chunks)


async def _extract_keywords(
    kb_id: str,
    query: str,
    external_query_type: QueryType | str | None = None,
    precomputed_query_embedding: Any | None = None,
) -> Tuple[List[str], List[str], QueryType, Dict[str, float]]:
    """
    提取高级和低级关键词
    
    Args:
        kb_id: 知识库 ID
        query: 查询文本
        
    Returns:
        (high_level_keywords, low_level_keywords)
    """
    hl_keywords = []
    ll_keywords = []
    query_type = QueryType.GENERAL
    seed_weights: Dict[str, float] = {}

    current_thread_name = threading.current_thread().name
    if current_thread_name.startswith("subagent-exec-"):
        # Subagent worker runs in dedicated thread/loop; avoid async DB/extractor
        # path to prevent cross-loop session/lock issues.
        return hl_keywords, ll_keywords, query_type, seed_weights
    
    try:
        from backend.rag.knowledge_base.manager import get_kb_manager
        from backend.db.connection import get_async_session_factory
        from backend.rag.service import get_rag_service
        
        rag_service = get_rag_service()
        
        embedding_func = None
        try:
            rag = await rag_service._get_rag_instance(kb_id)
            if rag and rag.lightrag:
                embedding_func = rag.lightrag.embedding_func
        except Exception:
            pass
        
        factory = get_async_session_factory()
        async with factory() as session:
            kb_manager = get_kb_manager()
            extractor = await kb_manager.get_keyword_extractor(
                session,
                kb_id,
                embedding_func,
            )
            keyword_result = await extractor.extract(
                query,
                external_query_type=external_query_type,
                precomputed_query_embedding=precomputed_query_embedding,
            )
            if keyword_result.high_level_keywords:
                hl_keywords = keyword_result.high_level_keywords
            if keyword_result.low_level_keywords:
                ll_keywords = keyword_result.low_level_keywords
            query_type = keyword_result.query_type
            if keyword_result.weighted_seed_scores:
                seed_weights = keyword_result.weighted_seed_scores
            logger.info(f"[关键词提取] 高级: {hl_keywords}, 低级: {ll_keywords}, 意图: {query_type}")
    except Exception as e:
        logger.warning(f"[关键词提取] 失败: {e}")
    
    return hl_keywords, ll_keywords, query_type, seed_weights


async def _get_kb_info(kb_id: str) -> Tuple[str, str]:
    """
    获取知识库信息
    
    Returns:
        (kb_code, kb_name)
    """
    if kb_id in _KNOWN_KB_INFO:
        return _KNOWN_KB_INFO[kb_id]

    current_thread_name = threading.current_thread().name
    if current_thread_name.startswith("subagent-exec-"):
        # In subagent worker loops, async DB sessions may be bound to another loop.
        # Prefer deterministic fallback to avoid noisy cross-loop warnings.
        logger.info("[知识库信息] 子智能体线程跳过 DB 查询，使用回退元信息: kb_id=%s", kb_id)
        return kb_id, kb_id

    try:
        from backend.db.connection import get_async_session_factory
        from backend.db.models.knowledge_base import KnowledgeBase
        from sqlalchemy import select
        
        factory = get_async_session_factory()
        async with factory() as session:
            stmt = select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id).limit(1)
            result = await session.execute(stmt)
            kb = result.scalar_one_or_none()
            if kb:
                return kb.kb_code, kb.kb_name
    except Exception as e:
        logger.warning(f"[知识库信息] 获取失败: {e}")
    return kb_id, kb_id


async def _get_default_kb_id() -> str | None:
    """
    获取默认知识库 ID
    
    优先级：
    1. is_default=True 的知识库
    2. kb_code='nuclear_electronics' 的知识库
    3. 返回 None
    """
    global _DEFAULT_KB_ID_CACHE
    
    if _DEFAULT_KB_ID_CACHE:
        return _DEFAULT_KB_ID_CACHE
    
    try:
        from backend.db.connection import get_async_session_factory
        from backend.db.models.knowledge_base import KnowledgeBase
        from sqlalchemy import select
        
        factory = get_async_session_factory()
        async with factory() as session:
            stmt = select(KnowledgeBase).where(KnowledgeBase.is_default == True).limit(1)
            result = await session.execute(stmt)
            kb = result.scalar_one_or_none()
            
            if kb:
                _DEFAULT_KB_ID_CACHE = kb.kb_id
                logger.info(f"[默认知识库] 使用默认知识库: {kb.kb_name} (kb_id={kb.kb_id})")
                return kb.kb_id
            
            stmt = select(KnowledgeBase).where(
                KnowledgeBase.kb_code == "nuclear_electronics"
            ).limit(1)
            result = await session.execute(stmt)
            kb = result.scalar_one_or_none()
            
            if kb:
                _DEFAULT_KB_ID_CACHE = kb.kb_id
                logger.info(f"[默认知识库] 使用核电子学知识库: {kb.kb_name} (kb_id={kb.kb_id})")
                return kb.kb_id
            
            logger.warning("[默认知识库] 未找到默认知识库")
            return None
    except Exception as e:
        logger.warning(f"[默认知识库] 获取默认知识库失败: {e}")
        return None


async def _stage1_recall(
    query: str,
    hl_keywords: List[str],
    ll_keywords: List[str],
    kb_id: str,
    top_k: int = 100,
    chunk_top_k: int = 30,
    query_type: Optional[QueryType] = None,
) -> RawRetrievalData:
    """
    阶段1：放宽初筛，获取原始全量上下文
    
    使用 aquery_data 获取结构化的实体、关系和文本块数据
    禁用内部重排序，避免不必要的计算开销
    
    优化：同时获取 chunk 的 embedding，供 MMR 复用，避免重复计算
    """
    raw_data = RawRetrievalData()
    
    try:
        from lightrag import QueryParam
        
        rag_service = get_rag_service()
        
        rag = await rag_service._get_rag_instance(kb_id)
        if not rag or not rag.lightrag:
            logger.warning("[阶段1] RAG 实例未初始化")
            return raw_data
        
        search_query = query
        
        # 关键词必须注入 LightRAG，否则在 mix 模式下 LightRAG 会由于 keyword 为空而触发耗时的 LLM 关键词提取
        effective_hl = hl_keywords
        effective_ll = ll_keywords
        
        param = QueryParam(
            mode="mix",
            stream=False,
            only_need_context=True,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            max_entity_tokens=7000,
            max_relation_tokens=7000,
            max_total_tokens=50000,
            hl_keywords=effective_hl,
            ll_keywords=effective_ll,
            enable_rerank=False,
        )
        
        lightrag = rag.lightrag
        query_result = await lightrag.aquery_data(search_query, param=param)
        
        if query_result and query_result.get("status") == "success":
            data_section = query_result.get("data", {})
            raw_data.entities = data_section.get("entities", [])
            raw_data.relationships = data_section.get("relationships", [])
            raw_data.chunks = data_section.get("chunks", [])
            raw_data.references = data_section.get("references", [])
            
            if raw_data.chunks and hasattr(lightrag, 'embedding_func'):
                try:
                    contents = [c.get("content", "")[:500] for c in raw_data.chunks]
                    embeddings = await lightrag.embedding_func(contents)
                    for i, chunk in enumerate(raw_data.chunks):
                        if i < len(embeddings):
                            chunk["_embedding"] = embeddings[i]
                    logger.info(f"[阶段1] 已获取 {len(raw_data.chunks)} 个 chunk 的 embedding，供 MMR 复用")
                except Exception as e:
                    logger.warning(f"[阶段1] 获取 chunk embedding 失败: {e}，MMR 将实时计算")
            
            logger.info(f"[阶段1] 召回数据: 实体={len(raw_data.entities)}, 关系={len(raw_data.relationships)}, 文本块={len(raw_data.chunks)}")
        else:
            message = query_result.get("message", "Unknown error") if query_result else "No result"
            logger.warning(f"[阶段1] 召回失败: {message}")
            
    except Exception as e:
        logger.error(f"[阶段1] 召回异常: {e}", exc_info=True)
    
    return raw_data


_embedding_func_cache = {}

# ── DirectRetriever 开关与缓存 ────────────────────────────────────────────────
import os as _os
_USE_DIRECT_RETRIEVER: bool = _os.getenv("USE_DIRECT_RETRIEVER", "true").lower() == "true"
_direct_retriever_cache: Dict[str, Any] = {}


async def _get_direct_retriever(kb_id: str, kb_code: str):
    """
    获取/构建 DirectRetriever 实例（进程内 LRU 缓存）
    若依赖缺失（embedding_func / nebula_storage）则返回 None，安全回退
    """
    cache_key = f"{kb_id}:{kb_code}"
    if cache_key in _direct_retriever_cache:
        return _direct_retriever_cache[cache_key]

    try:
        from backend.rag.retriever.direct_retriever import build_direct_retriever
        from backend.rag.knowledge_base.manager import get_kb_manager
        from backend.rag.retrieval_profile import (
            build_direct_retriever_config_for_profile,
            resolve_retrieval_profile,
        )
        from backend.db.connection import get_async_session_factory

        profile = resolve_retrieval_profile(kb_id=kb_id, kb_code=kb_code)
        dr_config = build_direct_retriever_config_for_profile(profile)

        rag_service = get_rag_service()
        rag = await rag_service._get_rag_instance(kb_id)
        if not rag or not rag.lightrag:
            return None

        factory = get_async_session_factory()
        async with factory() as session:
            kb_manager = get_kb_manager()
            # 通过 keyword extractor 获取底层 PerKBKeywordIndex
            extractor = await kb_manager.get_keyword_extractor(
                session, kb_id,
                embedding_func=getattr(rag.lightrag, "embedding_func", None),
            )
            keyword_index = await extractor._get_index()

        retriever = await build_direct_retriever(
            kb_code=kb_code,
            kb_id=kb_id,
            rag_instance=rag,
            keyword_index=keyword_index,
            config=dr_config,
        )
        if retriever is not None:
            _direct_retriever_cache[cache_key] = retriever
            logger.info(
                "[DirectRetriever] 已构建并缓存: kb_id=%s profile=%s path_a=%s",
                kb_id,
                profile.name,
                profile.enable_path_a,
            )
        return retriever
    except Exception as e:
        logger.warning("[DirectRetriever] 构建失败，将回退 LightRAG: %s", e)
        return None


async def _stage1_direct(
    query: str,
    hl_keywords: List[str],
    ll_keywords: List[str],
    kb_id: str,
    kb_code: str,
    query_type: QueryType,
    weighted_seed_scores: Dict[str, float],
    top_k: int = 80,
    chunk_top_k: int = 60,
    query_embedding: Any | None = None,
) -> Tuple["RawRetrievalData", Dict[str, float]]:
    """
    直连多路并行召回（替代 _stage1_recall + lightrag.aquery_data）

    Returns:
        (raw_data, enhanced_seed_weights)
    """
    retriever = await _get_direct_retriever(kb_id, kb_code)
    if retriever is None:
        logger.warning("[DirectRetriever] 实例获取失败，回退 LightRAG")
        raw_data = await _stage1_recall(
            query, hl_keywords, ll_keywords, kb_id,
            top_k=top_k, chunk_top_k=chunk_top_k, query_type=query_type,
        )
        return raw_data, weighted_seed_scores

    try:
        import numpy as _np
        qemb = None
        if query_embedding is not None:
            qemb = _np.array(query_embedding, dtype=_np.float32)

        raw_data, enhanced_weights = await retriever.retrieve(
            query=query,
            hl_keywords=hl_keywords,
            ll_keywords=ll_keywords,
            query_type=query_type,
            weighted_seed_scores=weighted_seed_scores,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            query_embedding=qemb,
        )
        return raw_data, enhanced_weights
    except Exception as e:
        logger.error("[DirectRetriever] 召回异常，回退 LightRAG: %s", e, exc_info=True)
        raw_data = await _stage1_recall(
            query, hl_keywords, ll_keywords, kb_id,
            top_k=top_k, chunk_top_k=chunk_top_k, query_type=query_type,
        )
        return raw_data, weighted_seed_scores


async def _get_embedding_func_for_kb(kb_id: str):
    """
    获取指定知识库对应的 embedding_func，带有进程内简单缓存。
    """
    if kb_id in _embedding_func_cache:
        return _embedding_func_cache[kb_id]
        
    try:
        rag_service = get_rag_service()
        rag = await rag_service._get_rag_instance(kb_id)
        if rag and rag.lightrag and hasattr(rag.lightrag, "embedding_func"):
            func = rag.lightrag.embedding_func
            _embedding_func_cache[kb_id] = func
            return func
    except Exception as e:
        logger.warning(f"[embedding] 获取 embedding_func 失败: {e}")
    return None


async def retrieve_context(
    query: str,
    kb_id: str | None = None,
    top_k: int = 30,
    chunk_top_k: int = 10,
    external_hl_keywords: List[str] | None = None,
    external_ll_keywords: List[str] | None = None,
    external_query_type: str | QueryType | None = None,
    seed_weights: Dict[str, float] | None = None,
    *,
    disable_stage1_oversample: bool = False,
    precomputed_query_embedding: Any | None = None,
    content_filter_mode: FilterMode | None = None,
    retrieval_profile_override: str | None = None,
) -> Tuple[FilteredData, str, List[dict[str, Any]], dict[str, int]]:
    """
    公共检索函数：执行完整的检索流程
    
    Args:
        query: 查询文本
        kb_id: 知识库 ID（可选，默认使用默认知识库）
        top_k: 检索数量
        chunk_top_k: 文档片段数量
        disable_stage1_oversample: 为 True 时阶段1按请求的 top_k/chunk_top_k 召回，不做过采样放大
        precomputed_query_embedding: 可选，与关键词提取 / 阶段1 直连召回共用，避免重复 embedding
        content_filter_mode: Stage-2 过滤模式；None 时使用 KB Profile 默认值
        retrieval_profile_override: 强制使用的 Profile 名（default/general_qa/domain/lightweight）
        
    Returns:
        (filtered_data, context_prompt, images, stats): 过滤后的数据、受控上下文、图片列表、统计信息
    """
    from backend.rag.retrieval_profile import resolve_retrieval_profile

    kb_id = kb_id or await _get_default_kb_id()
    if not kb_id:
        raise ValueError("未指定知识库且无法获取默认知识库")
    
    kb_code, kb_name = await _get_kb_info(kb_id)
    profile = resolve_retrieval_profile(
        kb_id=kb_id,
        kb_code=kb_code,
        override=retrieval_profile_override,
    )
    effective_filter_mode = content_filter_mode or profile.content_filter_mode
    effective_disable_oversample = disable_stage1_oversample or profile.disable_stage1_oversample
    effective_seed_weights: Dict[str, float] = dict(seed_weights or {})

    # 仅当显式传入高/低关键词列表时视为“外部关键词”；单独传 query_type 仍需走提取以得到 hl/ll，
    # 与 multi_kb_search / 路由侧关键词工具行为一致。
    if external_hl_keywords is not None or external_ll_keywords is not None:
        hl_keywords = list(external_hl_keywords or [])
        ll_keywords = list(external_ll_keywords or [])
        query_type = _ensure_query_type(external_query_type or "general")
        logger.info(
            "[检索] 使用外部关键词: 高级=%s, 低级=%s, 意图=%s",
            hl_keywords,
            ll_keywords,
            query_type,
        )
        if not effective_seed_weights:
            _, _, _, extracted_seed_weights = await _extract_keywords(
                kb_id,
                query,
                external_query_type=query_type,
                precomputed_query_embedding=precomputed_query_embedding,
            )
            effective_seed_weights = extracted_seed_weights
        else:
            logger.info("[检索] 使用外部 seed_weights，跳过关键词提取")
    else:
        hl_keywords, ll_keywords, query_type, extracted_seed_weights = await _extract_keywords(
            kb_id,
            query,
            external_query_type=external_query_type,
            precomputed_query_embedding=precomputed_query_embedding,
        )
        query_type = _ensure_query_type(query_type)
        if not effective_seed_weights:
            effective_seed_weights = extracted_seed_weights
    
    if effective_disable_oversample:
        stage1_top_k = max(1, int(top_k or 1))
        stage1_chunk_top_k = max(1, int(chunk_top_k or 1))
        logger.info(
            "[检索] 阶段1无过采样: top_k=%s, chunk_top_k=%s (profile=%s)",
            stage1_top_k,
            stage1_chunk_top_k,
            profile.name,
        )
    else:
        stage1_top_k, stage1_chunk_top_k = _compute_stage1_recall_window(top_k, chunk_top_k)
        logger.info(
            "[检索] 阶段1过采样参数: requested(top_k=%s, chunk_top_k=%s) -> stage1(top_k=%s, chunk_top_k=%s) profile=%s",
            top_k,
            chunk_top_k,
            stage1_top_k,
            stage1_chunk_top_k,
            profile.name,
        )

    if _USE_DIRECT_RETRIEVER:
        # ── 直连多路并行召回（无 LightRAG aquery_data）─────────────────────
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
        logger.info(
            "[检索] DirectRetriever 召回: 实体=%d, 关系=%d, chunks=%d",
            len(raw_data.entities), len(raw_data.relationships), len(raw_data.chunks),
        )
    else:
        # ── 原有 LightRAG mix 模式（保留作为回退）────────────────────────
        raw_data = await _stage1_recall(
            query,
            hl_keywords,
            ll_keywords,
            kb_id,
            top_k=stage1_top_k,
            chunk_top_k=stage1_chunk_top_k,
            query_type=query_type,
        )

    filter_manager = get_content_filter_manager()
    embedding_func = await _get_embedding_func_for_kb(kb_id)
    content_filter = await filter_manager.get_filter(
        kb_code=kb_code,
        kb_id=kb_id,
        embedding_func=embedding_func,
    )

    logger.info(
        "[检索] Profile=%s, 内容过滤模式: %s",
        profile.name,
        effective_filter_mode.value,
    )
    filter_result = await content_filter.filter_content(
        raw_data=raw_data,
        hl_keywords=hl_keywords,
        query=query,
        query_type=query_type,
        seed_weights=effective_seed_weights,
        mode=effective_filter_mode,
    )

    final_chunk_count = _clamp_filtered_chunks(filter_result.filtered_data, chunk_top_k)
    filter_result.stats["chunks"] = final_chunk_count
    
    logger.info(f"[检索] 高级关键词: {hl_keywords}, 意图: {query_type}")
    logger.info(f"[检索] 低级关键词: {ll_keywords}")
    
    images = await _extract_images_from_chunks(
        filter_result.filtered_data.chunks if filter_result.filtered_data else [],
    )
    
    stats = {
        "entities": filter_result.stats.get("entities", 0),
        "relationships": filter_result.stats.get("relationships", 0),
        "chunks": final_chunk_count,
    }
    
    return filter_result.filtered_data, filter_result.prompt, images, stats


async def _extract_images_from_chunks(
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    从文本块中提取图片路径及描述信息
    """
    import re
    from backend.storage import normalize_minio_url
    
    images = []
    seen_paths = set()
    
    for chunk in chunks:
        content = chunk.get("content", "")
        if not content:
            continue
        
        image_blocks = re.findall(
            r"Image Content Analysis:\s*\n"
            r"Image Path:\s*([^\r\n]+?\.(?:jpg|jpeg|png|gif|bmp|webp|tiff|tif)(?:\?[^\r\n]*)?)\s*\n"
            r"Captions:\s*([^\r\n]*)\s*\n"
            r"Footnotes:\s*([^\r\n]*)\s*\n+"
            r"Visual Analysis:\s*([^\r\n]+(?:\n(?![A-Z][a-z]+(?:\s+[A-Z][a-z]+)*:)[^\r\n]+)*)",
            content,
            re.IGNORECASE | re.MULTILINE,
        )
        
        for img_path, captions, footnotes, visual_analysis in image_blocks:
            p = img_path.strip()
            if not p:
                continue
            # 单编码 → 双编码规范化（已双编码的不受影响）
            p = normalize_minio_url(p)
            if p in seen_paths:
                continue
            seen_paths.add(p)

            marker = f"IMAGE_{len(images) + 1}"
            images.append({
                "marker": marker,
                "url": p,
                "path": p,
                "caption": captions.strip(),
                "footnote": footnotes.strip(),
                "visual_analysis": visual_analysis.strip(),
            })
    
    return images


class RagRetrieveRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    top_k: int = Field(default=30, description="检索数量")
    chunk_top_k: int = Field(default=10, description="文档片段数量")


class RagRetrieveWithKeywordsRequest(BaseModel):
    """带关键词的内部检索请求，供子智能体线程通过 HTTP API 调用"""
    query: str = Field(..., description="用户查询")
    kb_id: str = Field(..., description="知识库 ID")
    top_k: int = Field(default=30, description="检索数量")
    chunk_top_k: int = Field(default=10, description="文档片段数量")
    hl_keywords: list[str] = Field(default_factory=list, description="高级关键词")
    ll_keywords: list[str] = Field(default_factory=list, description="低级关键词")
    query_type: str = Field(default="general", description="查询类型")
    disable_stage1_oversample: bool = Field(
        default=False,
        description="为 True 时阶段1不做过采样，按 top_k/chunk_top_k 原样召回",
    )


class ImageInfo(BaseModel):
    marker: str = Field(..., description="图片标识")
    url: str = Field(..., description="图片访问URL")
    path: str = Field(..., description="图片原始路径")


class ChunkMeta(BaseModel):
    content: str = Field(default="", description="文本块内容")
    file_path: str = Field(default="", description="来源文件路径")
    fused_score: float = Field(default=0.0, description="融合得分（含时间衰减）")
    ppr_score: float = Field(default=0.0, description="PPR 得分")
    rerank_score: float = Field(default=0.0, description="Rerank 得分")
    time_score: float = Field(default=0.0, description="时间衰减得分")
    time_alpha: float = Field(default=0.0, description="时间敏感度")
    time_beta: float = Field(default=0.0, description="时间可信度")
    doc_time_year: int | None = Field(default=None, description="文档年份")
    doc_time_month: int | None = Field(default=None, description="文档月份")
    doc_time_confidence: float = Field(default=0.0, description="时间可信度系数")
    doc_id: str = Field(default="", description="文档ID")
    full_doc_id: str = Field(default="", description="完整文档ID")


class RagRetrieveResponse(BaseModel):
    context: str = Field(..., description="检索到的上下文内容")
    images: List[ImageInfo] = Field(default_factory=list, description="相关图片列表")
    entity_count: int = Field(default=0, description="实体数量")
    relation_count: int = Field(default=0, description="关系数量")
    chunk_count: int = Field(default=0, description="文档片段数量")
    knowledge_paths: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="知识路径（证据链条），每条路径包含 nodes/relations/score",
    )
    chunks: List[ChunkMeta] = Field(
        default_factory=list,
        description="文本块元数据（含融合得分、时间衰减等），供多知识库合并使用",
    )


def _extract_chunk_metas_from_filtered(filtered_data: FilteredData | None) -> list[ChunkMeta]:
    if not filtered_data:
        return []
    raw_chunks = filtered_data.chunks or []
    metas: list[ChunkMeta] = []
    for chunk in raw_chunks:
        if not isinstance(chunk, dict):
            continue
        raw_content = chunk.get("content", "")
        normalized_content = raw_content if raw_content else ""
        metas.append(ChunkMeta(
            content=normalized_content,
            file_path=chunk.get("file_path", chunk.get("source", "")),
            fused_score=float(chunk.get("_fused_score", 0.0)),
            ppr_score=float(chunk.get("_ppr_score", 0.0)),
            rerank_score=float(chunk.get("_rerank_score", 0.0)),
            time_score=float(chunk.get("_time_score", 0.0)),
            time_alpha=float(chunk.get("_time_alpha", 0.0)),
            time_beta=float(chunk.get("_time_beta", 0.0)),
            doc_time_year=chunk.get("_doc_time_year"),
            doc_time_month=chunk.get("_doc_time_month"),
            doc_time_confidence=float(chunk.get("_doc_time_confidence", 0.0)),
            doc_id=chunk.get("doc_id", chunk.get("_doc_id", "")),
            full_doc_id=chunk.get("full_doc_id", ""),
        ))
    return metas


def _extract_chunk_metas(filter_result) -> list[ChunkMeta]:
    if not filter_result or not filter_result.filtered_data:
        return []
    return _extract_chunk_metas_from_filtered(filter_result.filtered_data)


def _build_rag_retrieve_response(
    filtered_data: FilteredData | None,
    prompt: str,
    images: List[dict[str, Any]],
    stats: dict[str, int],
) -> RagRetrieveResponse:
    image_infos = [
        ImageInfo(
            marker=img.get("marker", ""),
            url=img.get("url", ""),
            path=img.get("path", ""),
        )
        for img in images
        if isinstance(img, dict)
    ]
    return RagRetrieveResponse(
        context=prompt,
        images=image_infos,
        entity_count=stats.get("entities", 0),
        relation_count=stats.get("relationships", 0),
        chunk_count=stats.get("chunks", 0),
        knowledge_paths=(
            filtered_data.knowledge_paths if filtered_data else []
        ),
        chunks=_extract_chunk_metas_from_filtered(filtered_data),
    )


class RagQueryStreamRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    model_id: str | None = Field(default=None, description="模型ID（可选）")
    top_k: int = Field(default=30, description="检索数量")
    chunk_top_k: int = Field(default=10, description="文档片段数量")
    temperature: float = Field(default=0.7, description="生成温度")
    max_tokens: int | None = Field(default=None, description="最大生成token数（默认从 MySQL 模型配置读取）")


DEFAULT_RAG_SYSTEM_PROMPT = """你是一个专业的知识库问答助手。请根据提供的上下文信息（包括结构化实体关系和文档内容）回答用户问题。

## 回答要求
1. 基于上下文信息准确回答，不要编造内容
2. 充分利用提供的**相关实体**和**相关关系**来理解知识脉络，将零散的文档内容串联起来
3. 如果上下文中没有相关信息，请明确告知用户
4. 使用清晰的结构组织回答，可以使用标题、列表、表格等格式
5. 保持回答简洁，突出重点

## 图片展示
当上下文中包含图片路径时（格式如 `Image Path: /path/to/image.png`），请使用 Markdown 格式嵌入图片：
- 使用语法：`![图片描述](图片URL)`
- 将图片嵌入到相关段落中，实现图文混排
"""


@router.post("/retrieve", response_model=RagRetrieveResponse)
async def rag_retrieve(
    request: RagRetrieveRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """
    仅检索知识库，不生成答案
    
    返回检索到的上下文内容和相关图片信息，供调用方自行处理。
    与 Agent 工具共用 ``retrieve_context``（DirectRetriever + Profile 驱动 Stage-2）。
    """
    total_start = time.time()
    
    try:
        kb_id = request.kb_id or await _get_default_kb_id()
        if not kb_id:
            raise HTTPException(status_code=400, detail="未指定知识库且无法获取默认知识库")

        filtered_data, prompt, images, stats = await retrieve_context(
            query=request.query,
            kb_id=kb_id,
            top_k=request.top_k,
            chunk_top_k=request.chunk_top_k,
        )

        total_duration = time.time() - total_start
        logger.info("[检索流水线] 总耗时: %.1fms", total_duration * 1000)
        return _build_rag_retrieve_response(filtered_data, prompt, images, stats)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"RAG 检索失败: {e}")
        raise HTTPException(status_code=500, detail=f"RAG 检索失败: {str(e)}")


@router.post("/retrieve_internal", response_model=RagRetrieveResponse)
async def rag_retrieve_internal(
    request: RagRetrieveWithKeywordsRequest,
    x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
):
    """
    内部检索接口（需携带内部共享密钥），供子智能体线程通过 HTTP API 调用。
    
    与 /retrieve 的区别：
    - 使用 X-Internal-Secret 头认证（非 API Key）
    - 接受外部传入的关键词，跳过关键词提取步骤
    - 在主线程/主事件循环中执行，复用已预热的缓存
    """
    from backend.config.settings import settings as app_settings
    
    expected_secret = app_settings.internal_api_secret
    if expected_secret:
        if not x_internal_secret or x_internal_secret != expected_secret:
            logger.warning("[内部检索] 拒绝未授权访问: secret 不匹配")
            raise HTTPException(status_code=403, detail="Forbidden: invalid internal secret")
    else:
        logger.warning(
            "[内部检索] internal_api_secret 未配置，内部接口存在暴露风险！"
            "请在 .env 中设置 INTERNAL_API_SECRET"
        )
    
    total_start = time.time()
    
    try:
        filtered_data, prompt, images, stats = await retrieve_context(
            query=request.query,
            kb_id=request.kb_id,
            top_k=request.top_k,
            chunk_top_k=request.chunk_top_k,
            external_hl_keywords=request.hl_keywords,
            external_ll_keywords=request.ll_keywords,
            external_query_type=request.query_type,
            disable_stage1_oversample=request.disable_stage1_oversample,
        )

        total_duration = time.time() - total_start
        logger.info("[内部检索流水线] 总耗时: %.1fms", total_duration * 1000)
        return _build_rag_retrieve_response(filtered_data, prompt, images, stats)
    except Exception as e:
        logger.exception(f"内部 RAG 检索失败: {e}")
        raise HTTPException(status_code=500, detail=f"内部 RAG 检索失败: {str(e)}")


async def _rag_stream_generator(
    service,
    query: str,
    kb_id: str | None,
    model_config: dict | None,
    temperature: float,
    max_tokens: int,
    top_k: int,
    chunk_top_k: int,
) -> AsyncGenerator[str, None]:
    """
    生成 RAG 流式响应
    
    完整检索流程：
    1. 关键词提取（向量搜索 + BM25 + 图拓扑融合）
    2. 阶段1：召回原始数据（实体、关系、文本块）
    3. 阶段2：PPR 图过滤
    4. Rerank 加权融合
    5. MMR 多样性重排序
    6. 阶段3：资产组装
    7. 流式生成答案
    """
    total_start = time.time()
    
    try:
        if not kb_id:
            yield f"data: {json.dumps({'type': 'error', 'content': '未指定知识库'})}\n\n"
            return
        
        yield f"data: {json.dumps({'type': 'status', 'content': '正在检索知识库...'})}\n\n"
        
        filtered_data, context_prompt, images, stats = await retrieve_context(
            query=query,
            kb_id=kb_id,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
        )
        logger.info(
            "[检索] 流式问答召回完成 | 实体=%s, 关系=%s, 文本块=%s",
            stats.get("entities", 0),
            stats.get("relationships", 0),
            stats.get("chunks", 0),
        )
        
        image_urls = []
        for img in images:
            if isinstance(img, dict):
                path = img.get("path", "")
                url = img.get("url", "")
                if path and url:
                    image_urls.append({"path": path, "url": url})
        
        yield f"data: {json.dumps({'type': 'context', 'images': image_urls})}\n\n"
        
        yield f"data: {json.dumps({'type': 'status', 'content': '正在生成回答...'})}\n\n"
        
        from backend.config.settings import settings
        
        if model_config:
            llm_config = model_config
        else:
            llm_config = settings.active_llm_config
        
        from backend.core.models.context_limits import resolve_max_tokens

        api_base = (llm_config.get("base_url") or "").rstrip("/")
        api_key = llm_config.get("api_key")
        model = llm_config.get("model")
        effective_max_tokens = (
            max_tokens
            if max_tokens is not None
            else resolve_max_tokens(llm_config, api_model=model)
        )

        messages = [
            {"role": "system", "content": DEFAULT_RAG_SYSTEM_PROMPT},
            {"role": "user", "content": f"# 用户问题\n\n{query}\n\n---\n\n# 相关上下文\n\n{context_prompt}\n\n---\n\n# 请回答以下问题\n\n{query}"}
        ]
        
        import httpx
        
        request_json = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": effective_max_tokens,
            "stream": True,
        }

        if "qwen" in (model or "").lower():
            request_json["thinking"] = {"type": "off"}
        
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "POST",
                f"{api_base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=request_json,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield f"data: {json.dumps({'type': 'chunk', 'content': content})}\n\n"
                        except json.JSONDecodeError:
                            continue
        
        total_duration = time.time() - total_start
        logger.info(f"[检索流水线] 总耗时: {total_duration*1000:.1f}ms")
        
        yield "data: [DONE]\n\n"
        
    except asyncio.CancelledError:
        logger.warning("RAG 流式连接被取消")
        yield f"data: {json.dumps({'type': 'error', 'message': '连接已取消'})}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        logger.exception(f"流式查询失败: {e}")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        yield "data: [DONE]\n\n"


@router.post("/query_stream")
async def rag_query_stream(
    request: RagQueryStreamRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """
    检索知识库并流式生成答案
    
    返回 SSE (Server-Sent Events) 格式的流式响应：
    - type: "status" - 状态更新
    - type: "context" - 检索上下文和图片信息
    - type: "chunk" - 生成的文本片段
    - type: "error" - 错误信息
    
    完整检索流程：
    1. 关键词提取（向量搜索 + BM25 + 图拓扑融合）
    2. 阶段1：召回原始数据
    3. 阶段2：PPR 图过滤
    4. Rerank 加权融合
    5. MMR 多样性重排序
    6. 阶段3：资产组装
    7. 流式生成答案
    """
    try:
        from backend.config.settings import settings
        
        model_config = None
        if request.model_id:
            model_config = settings.get_model_config(request.model_id)
            if not model_config:
                raise HTTPException(
                    status_code=400, detail=f"不支持的模型: {request.model_id}"
                )
        
        service = get_rag_service()
        
        kb_id = request.kb_id or await _get_default_kb_id()
        
        return StreamingResponse(
            _rag_stream_generator(
                service,
                request.query,
                kb_id,
                model_config,
                request.temperature,
                request.max_tokens,
                request.top_k,
                request.chunk_top_k,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"流式查询初始化失败: {e}")
        raise HTTPException(status_code=500, detail=f"流式查询初始化失败: {str(e)}")


class RagInsertRequest(BaseModel):
    content_list: List[Dict[str, Any]] = Field(..., description="OCR content_list")
    file_path: str = Field(default="unknown_document", description="来源文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagInsertResponse(BaseModel):
    result: Dict[str, Any]


class RagProcessMultimodalRequest(BaseModel):
    file_path: str = Field(..., description="文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagInsertDocxRequest(BaseModel):
    file_path: str = Field(..., description="DOCX 文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class ImageInfoData(BaseModel):
    marker: str
    url: str
    path: str


class ImageUsageInfo(BaseModel):
    total: int
    used: int
    unused: int
    used_list: List[ImageInfoData] = Field(default_factory=list)
    unused_list: List[ImageInfoData] = Field(default_factory=list)
    all: List[ImageInfoData] = Field(default_factory=list)


class AcademicFormatInfo(BaseModel):
    has_abstract: bool = False
    has_introduction: bool = False
    has_conclusion: bool = False
    has_references: bool = False
    has_appendix: bool = False


class MetadataInfo(BaseModel):
    title: str
    intent: str
    intent_label: str
    confidence_level: str
    confidence_label: str
    knowledge_base_coverage: float
    coverage_percentage: str
    template_used: str


class RetrievalInfo(BaseModel):
    mode: str
    query_expansion_enabled: bool
    multi_hop_enabled: bool
    expanded_queries: List[str] = Field(default_factory=list)
    is_multi_hop: bool = False
    sub_queries: List[str] = Field(default_factory=list)


class RagResetRequest(BaseModel):
    new_work_dir: str | None = Field(default=None, description="新的 RAG_WORK_DIR")
    clear_old_work_dir: bool = Field(default=True, description="是否清空旧工作目录")
    kb_id: str | None = Field(default=None, description="知识库 ID")


class RagResetResponse(BaseModel):
    result: Dict[str, Any]


class RagDeleteDocRequest(BaseModel):
    doc_id: str = Field(..., description="需要删除的 doc_id")
    delete_llm_cache: bool = Field(default=False, description="是否删除 LLM 缓存")
    kb_id: str | None = Field(default=None, description="知识库 ID")


class RagDeleteDocResponse(BaseModel):
    result: Dict[str, Any]


class ModelInfo(BaseModel):
    id: str = Field(..., description="模型ID")
    name: str = Field(..., description="模型显示名称")
    provider: str = Field(..., description="服务商")
    description: str | None = Field(default=None, description="模型描述")


class ModelsListResponse(BaseModel):
    models: List[ModelInfo] = Field(default_factory=list, description="可用模型列表")
    default_model: str = Field(..., description="默认模型ID")


@router.get("/models", response_model=ModelsListResponse)
async def get_available_models():
    """获取可用对话模型列表 — 优先从数据库读取（usage_scope='chat'）"""
    from backend.db.connection import get_async_session_factory
    from backend.db.models.llm_model import LlmModel
    from sqlalchemy import select

    session_factory = get_async_session_factory()
    try:
        async with session_factory() as session:
            # 尝试从数据库读取
            stmt = select(LlmModel).where(
                LlmModel.usage_scope == "chat",
                LlmModel.is_active == True,
            ).order_by(LlmModel.sort_order)
            result = await session.execute(stmt)
            db_models = result.scalars().all()

            if db_models:
                model_list = [
                    ModelInfo(
                        id=m.name,
                        name=m.display_name or m.name,
                        provider=m.provider,
                        description=m.description,
                    )
                    for m in db_models
                ]
                from backend.core.agents.config_compat import get_default_model_name
                default_model = next(
                    (m.name for m in db_models if m.is_default),
                    db_models[0].name if db_models else (get_default_model_name() or "default-model")
                )
                return ModelsListResponse(models=model_list, default_model=default_model)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"从数据库读取可用模型失败: {e}")

    # 兜底：从 Settings 读取
    from backend.config.settings import settings

    models = settings.get_available_models()
    model_list = [
        ModelInfo(
            id=m.get("id", ""),
            name=m.get("name", m.get("id", "")),
            provider=m.get("provider", "custom"),
            description=m.get("description"),
        )
        for m in models
    ]

    default_model = settings.dashscope_model or "qwen3-max"

    return ModelsListResponse(models=model_list, default_model=default_model)


@router.get("/health")
async def rag_health(auth: ApiKeyAuth = Depends(verify_api_key)):
    service = get_rag_service()
    try:
        await service.initialize()
        return {"status": "healthy"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


@router.post("/insert", response_model=RagInsertResponse)
async def rag_insert(
    request: RagInsertRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """
    插入文档到知识库（OCR content_list 格式）
    
    Args:
        request: 包含 content_list、file_path、kb_id 等参数
        
    Returns:
        插入结果
    """
    try:
        service = get_rag_service()
        await service.initialize()
        
        kb_id = request.kb_id or await _get_default_kb_id()
        
        result = await service.insert_content_list(
            content_list=request.content_list,
            file_path=request.file_path,
            kb_id=kb_id,
            **request.kwargs,
        )
        
        return RagInsertResponse(result=result)
    except Exception as e:
        logger.exception(f"文档插入失败: {e}")
        raise HTTPException(status_code=500, detail=f"文档插入失败: {str(e)}")


@router.post("/process_multimodal")
async def rag_process_multimodal(
    request: RagProcessMultimodalRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """
    处理多模态文档（PDF、图片等）
    
    使用 OCR 服务处理文档，然后插入到知识库
    """
    try:
        service = get_rag_service()
        await service.initialize()
        
        kb_id = request.kb_id or await _get_default_kb_id()
        
        result = await service.process_multimodal_document(
            file_path=request.file_path,
            kb_id=kb_id,
            **request.kwargs,
        )
        
        return result
    except Exception as e:
        logger.exception(f"多模态文档处理失败: {e}")
        raise HTTPException(status_code=500, detail=f"多模态文档处理失败: {str(e)}")


@router.post("/insert_docx")
async def rag_insert_docx(
    request: RagInsertDocxRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """
    插入 DOCX 文档到知识库
    """
    try:
        service = get_rag_service()
        await service.initialize()
        
        kb_id = request.kb_id or await _get_default_kb_id()
        
        result = await service.insert_docx(
            file_path=request.file_path,
            kb_id=kb_id,
            **request.kwargs,
        )
        
        return result
    except Exception as e:
        logger.exception(f"DOCX 插入失败: {e}")
        raise HTTPException(status_code=500, detail=f"DOCX 插入失败: {str(e)}")

@router.post("/reset", response_model=RagResetResponse)
async def rag_reset(
    request: RagResetRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """
    重置 RAG 服务
    
    可选参数：
    - new_work_dir: 新的工作目录
    - clear_old_work_dir: 是否清空旧工作目录
    - kb_id: 指定知识库
    """
    try:
        service = get_rag_service()
        
        result = await service.reset(
            new_work_dir=request.new_work_dir,
            clear_old_work_dir=request.clear_old_work_dir,
            kb_id=request.kb_id,
        )
        
        return RagResetResponse(result=result)
    except Exception as e:
        logger.exception(f"RAG 重置失败: {e}")
        raise HTTPException(status_code=500, detail=f"RAG 重置失败: {str(e)}")


@router.post("/delete_doc", response_model=RagDeleteDocResponse)
async def rag_delete_doc(
    request: RagDeleteDocRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """
    删除文档
    
    Args:
        doc_id: 需要删除的文档 ID
        delete_llm_cache: 是否同时删除 LLM 缓存
        kb_id: 指定知识库
    """
    try:
        service = get_rag_service()
        
        result = await service.delete_document(
            doc_id=request.doc_id,
            delete_llm_cache=request.delete_llm_cache,
            kb_id=request.kb_id,
        )
        
        return RagDeleteDocResponse(result=result)
    except Exception as e:
        logger.exception(f"文档删除失败: {e}")
        raise HTTPException(status_code=500, detail=f"文档删除失败: {str(e)}")
