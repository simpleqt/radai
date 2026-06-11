"""
Chunk 查询 Fallback 工具

功能：
1. 优先从 Redis text_chunks_db 查询 chunk 数据
2. 如果 Redis 缺失，自动 fallback 到 Milvus chunks_vdb 查询
3. 解决 text_chunks_db 为空导致 entity_chunks 召回失败的问题
"""

import asyncio
from typing import Dict, List, Any, Optional
from collections import defaultdict


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
    
    # 1. 从 Redis text_chunks_db 查询
    redis_results = await text_chunks_db.get_by_ids(chunk_ids)
    
    # 2. 检查哪些 chunk 在 Redis 中缺失
    missing_indices = []
    missing_chunk_ids = []
    
    for i, (chunk_id, chunk_data) in enumerate(zip(chunk_ids, redis_results)):
        if chunk_data is None or "content" not in chunk_data:
            missing_indices.append(i)
            missing_chunk_ids.append(chunk_id)
    
    # 3. 如果有缺失且 Milvus 可用，从 Milvus 查询
    if missing_chunk_ids and chunks_vdb:
        try:
            milvus_results = await _query_chunks_from_milvus(
                missing_chunk_ids, chunks_vdb, batch_size
            )
            
            # 4. 将 Milvus 结果填充到对应位置
            for idx, chunk_id in zip(missing_indices, missing_chunk_ids):
                if chunk_id in milvus_results:
                    redis_results[idx] = milvus_results[chunk_id]
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Milvus fallback 查询失败: {e}")
    
    return redis_results


async def _query_chunks_from_milvus(
    chunk_ids: List[str],
    chunks_vdb,
    batch_size: int = 100,
) -> Dict[str, Dict[str, Any]]:
    """
    从 Milvus 查询 chunk 数据
    
    Args:
        chunk_ids: chunk ID 列表
        chunks_vdb: Milvus chunks_vdb 存储
        batch_size: 批量查询大小
        
    Returns:
        {chunk_id: chunk_data} 字典
    """
    milvus_client = chunks_vdb._client
    collection_name = chunks_vdb.final_namespace
    
    # 查询字段：包含动态字段 content
    query_fields = ["id", "content", "file_path", "full_doc_id", "created_at"]
    
    results = {}
    
    # 分批查询
    for i in range(0, len(chunk_ids), batch_size):
        batch_chunk_ids = chunk_ids[i:i + batch_size]
        
        try:
            # 构建查询表达式
            if len(batch_chunk_ids) == 1:
                expr = f'id == "{batch_chunk_ids[0]}"'
            else:
                chunk_ids_str = '", "'.join(batch_chunk_ids)
                expr = f'id in ["{chunk_ids_str}"]'
            
            # 查询 Milvus
            milvus_results = milvus_client.query(
                collection_name=collection_name,
                filter=expr,
                output_fields=query_fields,
                limit=len(batch_chunk_ids)
            )
            
            if milvus_results:
                for record in milvus_results:
                    chunk_id = record.get('id')
                    if chunk_id and 'content' in record:
                        results[chunk_id] = record
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Milvus 查询批次 {i//batch_size + 1} 失败: {e}")
            continue
    
    return results
