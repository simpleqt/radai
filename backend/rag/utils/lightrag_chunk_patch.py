"""
LightRAG chunk 查询 Monkey Patch

功能：
1. Patch _find_related_text_unit_from_entities 函数
2. 在 Redis text_chunks_db 查询失败时，自动 fallback 到 Milvus chunks_vdb
3. 解决 text_chunks_db 为空导致 entity_chunks 召回失败的问题

使用方法：
    在 RAG 初始化时调用 apply_chunk_query_patch()
"""

import logging
import time
from typing import Any, Dict, List, Optional

from backend.core.utils.env import positive_float_from_env

logger = logging.getLogger(__name__)


# MilvusClient.query 是同步 RPC，不能直接在异步事件循环中调用。
# 外层线程等待略长于 RPC timeout，用于接收服务端返回的超时异常。
_MILVUS_CHUNK_QUERY_TIMEOUT_SECONDS = positive_float_from_env(
    "MILVUS_CHUNK_QUERY_TIMEOUT_SECONDS", 15.0
)
_MILVUS_CHUNK_QUERY_WAIT_SECONDS = positive_float_from_env(
    "MILVUS_CHUNK_QUERY_WAIT_SECONDS", 17.0
)


async def _query_chunks_from_milvus_batch(
    chunk_ids: List[str],
    chunks_vdb,
    batch_size: int = 100,
) -> Dict[str, Dict[str, Any]]:
    """
    从 Milvus 批量查询 chunk 数据
    
    Args:
        chunk_ids: chunk ID 列表
        chunks_vdb: Milvus chunks_vdb 存储
        batch_size: 批量查询大小
        
    Returns:
        {chunk_id: chunk_data} 字典
    """
    milvus_client = chunks_vdb._client
    collection_name = chunks_vdb.final_namespace

    # run_blocking 使用应用级有界线程池：请求超时后不会继续占用事件循环，
    # 同时会保留线程池容量直到未协作的底层 RPC 真正结束，避免无限制创建线程。
    from backend.application.blocking import run_blocking
    
    query_fields = ["id", "content", "file_path", "full_doc_id", "created_at"]
    
    results = {}
    
    for i in range(0, len(chunk_ids), batch_size):
        batch_chunk_ids = chunk_ids[i:i + batch_size]
        
        try:
            if len(batch_chunk_ids) == 1:
                expr = f'id == "{batch_chunk_ids[0]}"'
            else:
                chunk_ids_str = '", "'.join(batch_chunk_ids)
                expr = f'id in ["{chunk_ids_str}"]'
            
            def _query_sync():
                return milvus_client.query(
                    collection_name=collection_name,
                    filter=expr,
                    output_fields=query_fields,
                    limit=len(batch_chunk_ids),
                    timeout=_MILVUS_CHUNK_QUERY_TIMEOUT_SECONDS,
                )

            started_at = time.monotonic()
            try:
                milvus_results = await run_blocking(
                    _query_sync,
                    timeout_seconds=_MILVUS_CHUNK_QUERY_WAIT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "Milvus chunk 查询批次 %d 超时（%.1fs），跳过 fallback，避免阻塞事件循环: collection=%s",
                    i // batch_size + 1,
                    time.monotonic() - started_at,
                    collection_name,
                )
                break
            
            if milvus_results:
                for record in milvus_results:
                    chunk_id = record.get('id')
                    if chunk_id and 'content' in record:
                        results[chunk_id] = record
        except Exception as e:
            logger.warning(f"Milvus 查询批次 {i//batch_size + 1} 失败: {e}")
            continue
    
    return results


async def get_chunks_with_milvus_fallback(
    chunk_ids: List[str],
    text_chunks_db,
    chunks_vdb=None,
    batch_size: int = 100,
) -> List[Optional[Dict[str, Any]]]:
    """
    获取 chunk 数据，优先从 Redis 查询，缺失时 fallback 到 Milvus
    
    Args:
        chunk_ids: chunk ID 列表
        text_chunks_db: Redis text_chunks 存储
        chunks_vdb: Milvus chunks_vdb 存储（可选）
        batch_size: 批量查询大小
        
    Returns:
        chunk 数据列表，与 chunk_ids 顺序对应，缺失的为 None
    """
    if not chunk_ids:
        return []
    
    redis_results = await text_chunks_db.get_by_ids(chunk_ids)
    
    missing_indices = []
    missing_chunk_ids = []
    
    for i, (chunk_id, chunk_data) in enumerate(zip(chunk_ids, redis_results)):
        if chunk_data is None or "content" not in chunk_data:
            missing_indices.append(i)
            missing_chunk_ids.append(chunk_id)
    
    if missing_chunk_ids and chunks_vdb:
        try:
            logger.info(
                f"Redis 缺失 {len(missing_chunk_ids)} 个 chunks，从 Milvus fallback 查询"
            )
            
            milvus_results = await _query_chunks_from_milvus_batch(
                missing_chunk_ids, chunks_vdb, batch_size
            )
            
            for idx, chunk_id in zip(missing_indices, missing_chunk_ids):
                if chunk_id in milvus_results:
                    redis_results[idx] = milvus_results[chunk_id]
            
            logger.info(
                f"Milvus fallback 查询完成: 找到 {len(milvus_results)}/{len(missing_chunk_ids)} 个 chunks"
            )
        except Exception as e:
            logger.warning(f"Milvus fallback 查询失败: {e}")
    
    return redis_results


def patch_find_related_text_unit_from_entities():
    """
    Patch _find_related_text_unit_from_entities 函数添加 Milvus fallback
    
    这个 patch 修改了 LightRAG 的 chunk 查询逻辑：
    - 原逻辑：只从 text_chunks_db (Redis) 查询
    - 新逻辑：优先从 text_chunks_db 查询，缺失时 fallback 到 chunks_vdb (Milvus)
    """
    import lightrag.operate as operate_module
    
    async def patched_func(
        node_datas: list[dict],
        query_param,
        text_chunks_db,
        knowledge_graph_inst=None,
        query: str = None,
        chunks_vdb=None,
        chunk_tracking: dict = None,
        query_embedding=None,
    ):
        """
        Patched version of _find_related_text_unit_from_entities
        添加了 Milvus fallback 支持
        """
        from lightrag.utils import split_string_by_multi_markers, GRAPH_FIELD_SEP
        from lightrag.constants import (
            DEFAULT_KG_CHUNK_PICK_METHOD,
            DEFAULT_RELATED_CHUNK_NUMBER,
        )
        
        logger.debug(f"Finding text chunks from {len(node_datas)} entities")
        
        if not node_datas:
            return []
        
        entities_with_chunks = []
        for entity in node_datas:
            if entity.get("source_id"):
                chunks = split_string_by_multi_markers(
                    entity["source_id"], [GRAPH_FIELD_SEP]
                )
                if chunks:
                    entities_with_chunks.append(
                        {
                            "entity_name": entity["entity_name"],
                            "chunks": chunks,
                            "entity_data": entity,
                        }
                    )
        
        if not entities_with_chunks:
            logger.warning("No entities with text chunks found")
            return []
        
        kg_chunk_pick_method = text_chunks_db.global_config.get(
            "kg_chunk_pick_method", DEFAULT_KG_CHUNK_PICK_METHOD
        )
        max_related_chunks = text_chunks_db.global_config.get(
            "related_chunk_number", DEFAULT_RELATED_CHUNK_NUMBER
        )
        
        chunk_occurrence_count = {}
        for entity_info in entities_with_chunks:
            deduplicated_chunks = []
            for chunk_id in entity_info["chunks"]:
                chunk_occurrence_count[chunk_id] = (
                    chunk_occurrence_count.get(chunk_id, 0) + 1
                )
                if chunk_occurrence_count[chunk_id] == 1:
                    deduplicated_chunks.append(chunk_id)
            entity_info["chunks"] = deduplicated_chunks
        
        total_entity_chunks = 0
        for entity_info in entities_with_chunks:
            sorted_chunks = sorted(
                entity_info["chunks"],
                key=lambda chunk_id: chunk_occurrence_count.get(chunk_id, 0),
                reverse=True,
            )
            entity_info["sorted_chunks"] = sorted_chunks
            total_entity_chunks += len(sorted_chunks)
        
        selected_chunk_ids = []
        
        if kg_chunk_pick_method == "VECTOR" and query and chunks_vdb:
            num_of_chunks = int(max_related_chunks * len(entities_with_chunks) / 2)
            
            actual_embedding_func = text_chunks_db.embedding_func
            if not actual_embedding_func:
                logger.warning("No embedding function found, falling back to WEIGHT method")
                kg_chunk_pick_method = "WEIGHT"
            else:
                try:
                    from lightrag.utils import pick_by_vector_similarity
                    
                    selected_chunk_ids = await pick_by_vector_similarity(
                        query=query,
                        text_chunks_storage=text_chunks_db,
                        chunks_vdb=chunks_vdb,
                        num_of_chunks=num_of_chunks,
                        entity_info=entities_with_chunks,
                        embedding_func=actual_embedding_func,
                        query_embedding=query_embedding,
                    )
                    
                    if selected_chunk_ids == []:
                        kg_chunk_pick_method = "WEIGHT"
                        logger.warning(
                            "No entity-related chunks selected by vector similarity, falling back to WEIGHT method"
                        )
                    else:
                        logger.info(
                            f"Selecting {len(selected_chunk_ids)} from {total_entity_chunks} entity-related chunks by vector similarity"
                        )
                except Exception as e:
                    logger.error(
                        f"Error in vector similarity sorting: {e}, falling back to WEIGHT method"
                    )
                    kg_chunk_pick_method = "WEIGHT"
        
        if kg_chunk_pick_method == "WEIGHT":
            from lightrag.utils import pick_by_weighted_polling
            
            selected_chunk_ids = pick_by_weighted_polling(
                entities_with_chunks, max_related_chunks, min_related_chunks=1
            )
            
            logger.info(
                f"Selecting {len(selected_chunk_ids)} from {total_entity_chunks} entity-related chunks by weighted polling"
            )
        
        if not selected_chunk_ids:
            return []
        
        unique_chunk_ids = list(dict.fromkeys(selected_chunk_ids))
        
        # ===== 关键修改：使用 Milvus fallback =====
        chunk_data_list = await get_chunks_with_milvus_fallback(
            unique_chunk_ids, text_chunks_db, chunks_vdb
        )
        # ==========================================
        
        result_chunks = []
        for i, (chunk_id, chunk_data) in enumerate(zip(unique_chunk_ids, chunk_data_list)):
            if chunk_data is not None and "content" in chunk_data:
                chunk_data_copy = chunk_data.copy()
                chunk_data_copy["source_type"] = "entity"
                chunk_data_copy["chunk_id"] = chunk_id
                result_chunks.append(chunk_data_copy)
                
                if chunk_tracking is not None:
                    chunk_tracking[chunk_id] = {
                        "source": "E",
                        "frequency": chunk_occurrence_count.get(chunk_id, 1),
                        "order": i + 1,
                    }
        
        return result_chunks
    
    operate_module._find_related_text_unit_from_entities = patched_func
    
    logger.info("✅ 已 patch _find_related_text_unit_from_entities 添加 Milvus fallback")


def patch_find_related_text_unit_from_relations():
    """
    Patch _find_related_text_unit_from_relations 函数添加 Milvus fallback
    """
    import lightrag.operate as operate_module
    
    async def patched_func(
        edge_datas: list[dict],
        query_param,
        text_chunks_db,
        entity_chunks: list[dict] = None,
        query: str = None,
        chunks_vdb=None,
        chunk_tracking: dict = None,
        query_embedding=None,
    ):
        """
        Patched version of _find_related_text_unit_from_relations
        添加了 Milvus fallback 支持
        """
        from lightrag.utils import split_string_by_multi_markers, GRAPH_FIELD_SEP
        from lightrag.constants import (
            DEFAULT_KG_CHUNK_PICK_METHOD,
            DEFAULT_RELATED_CHUNK_NUMBER,
        )
        
        logger.debug(f"Finding text chunks from {len(edge_datas)} relations")
        
        if not edge_datas:
            return []
        
        relations_with_chunks = []
        for relation in edge_datas:
            if relation.get("source_id"):
                chunks = split_string_by_multi_markers(
                    relation["source_id"], [GRAPH_FIELD_SEP]
                )
                if chunks:
                    if "src_tgt" in relation:
                        rel_key = tuple(sorted(relation["src_tgt"]))
                    else:
                        rel_key = tuple(
                            sorted([relation.get("src_id"), relation.get("tgt_id")])
                        )
                    
                    relations_with_chunks.append(
                        {
                            "relation_key": rel_key,
                            "chunks": chunks,
                            "relation_data": relation,
                        }
                    )
        
        if not relations_with_chunks:
            logger.warning("No relation-related chunks found")
            return []
        
        kg_chunk_pick_method = text_chunks_db.global_config.get(
            "kg_chunk_pick_method", DEFAULT_KG_CHUNK_PICK_METHOD
        )
        max_related_chunks = text_chunks_db.global_config.get(
            "related_chunk_number", DEFAULT_RELATED_CHUNK_NUMBER
        )
        
        entity_chunk_ids = set()
        if entity_chunks:
            for chunk in entity_chunks:
                chunk_id = chunk.get("chunk_id")
                if chunk_id:
                    entity_chunk_ids.add(chunk_id)
        
        chunk_occurrence_count = {}
        removed_entity_chunk_ids = set()
        
        for rel_info in relations_with_chunks:
            deduplicated_chunks = []
            for chunk_id in rel_info["chunks"]:
                if chunk_id in entity_chunk_ids and chunk_id not in removed_entity_chunk_ids:
                    chunk_occurrence_count[chunk_id] = chunk_occurrence_count.get(chunk_id, 0)
                    removed_entity_chunk_ids.add(chunk_id)
                    continue
                
                chunk_occurrence_count[chunk_id] = (
                    chunk_occurrence_count.get(chunk_id, 0) + 1
                )
                if chunk_occurrence_count[chunk_id] == 1:
                    deduplicated_chunks.append(chunk_id)
            
            rel_info["chunks"] = deduplicated_chunks
        
        total_relation_chunks = 0
        for rel_info in relations_with_chunks:
            sorted_chunks = sorted(
                rel_info["chunks"],
                key=lambda chunk_id: chunk_occurrence_count.get(chunk_id, 0),
                reverse=True,
            )
            rel_info["sorted_chunks"] = sorted_chunks
            total_relation_chunks += len(sorted_chunks)
        
        selected_chunk_ids = []
        
        if kg_chunk_pick_method == "VECTOR" and query and chunks_vdb:
            num_of_chunks = int(max_related_chunks * len(relations_with_chunks) / 2)
            
            actual_embedding_func = text_chunks_db.embedding_func
            if not actual_embedding_func:
                logger.warning("No embedding function found, falling back to WEIGHT method")
                kg_chunk_pick_method = "WEIGHT"
            else:
                try:
                    from lightrag.utils import pick_by_vector_similarity
                    
                    selected_chunk_ids = await pick_by_vector_similarity(
                        query=query,
                        text_chunks_storage=text_chunks_db,
                        chunks_vdb=chunks_vdb,
                        num_of_chunks=num_of_chunks,
                        entity_info=relations_with_chunks,
                        embedding_func=actual_embedding_func,
                        query_embedding=query_embedding,
                    )
                    
                    if selected_chunk_ids == []:
                        kg_chunk_pick_method = "WEIGHT"
                        logger.warning(
                            "No relation-related chunks selected by vector similarity, falling back to WEIGHT method"
                        )
                    else:
                        logger.info(
                            f"Selecting {len(selected_chunk_ids)} from {total_relation_chunks} relation-related chunks by vector similarity"
                        )
                except Exception as e:
                    logger.error(
                        f"Error in vector similarity sorting: {e}, falling back to WEIGHT method"
                    )
                    kg_chunk_pick_method = "WEIGHT"
        
        if kg_chunk_pick_method == "WEIGHT":
            from lightrag.utils import pick_by_weighted_polling
            
            selected_chunk_ids = pick_by_weighted_polling(
                relations_with_chunks, max_related_chunks, min_related_chunks=1
            )
            
            logger.info(
                f"Selecting {len(selected_chunk_ids)} from {total_relation_chunks} relation-related chunks by weighted polling"
            )
        
        if not selected_chunk_ids:
            return []
        
        unique_chunk_ids = list(dict.fromkeys(selected_chunk_ids))
        
        # ===== 关键修改：使用 Milvus fallback =====
        chunk_data_list = await get_chunks_with_milvus_fallback(
            unique_chunk_ids, text_chunks_db, chunks_vdb
        )
        # ==========================================
        
        result_chunks = []
        for i, (chunk_id, chunk_data) in enumerate(zip(unique_chunk_ids, chunk_data_list)):
            if chunk_data is not None and "content" in chunk_data:
                chunk_data_copy = chunk_data.copy()
                chunk_data_copy["source_type"] = "relation"
                chunk_data_copy["chunk_id"] = chunk_id
                result_chunks.append(chunk_data_copy)
                
                if chunk_tracking is not None:
                    chunk_tracking[chunk_id] = {
                        "source": "R",
                        "frequency": chunk_occurrence_count.get(chunk_id, 1),
                        "order": i + 1,
                    }
        
        return result_chunks
    
    operate_module._find_related_text_unit_from_relations = patched_func
    
    logger.info("✅ 已 patch _find_related_text_unit_from_relations 添加 Milvus fallback")


def apply_chunk_query_patch():
    """
    应用所有 chunk 查询 patch
    
    在 RAG 初始化时调用此函数
    """
    patch_find_related_text_unit_from_entities()
    patch_find_related_text_unit_from_relations()
    
    logger.info("✅ 所有 chunk 查询 patch 已应用")
