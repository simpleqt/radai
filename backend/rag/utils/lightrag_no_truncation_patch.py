"""
移除 LightRAG 所有硬编码过滤和截断的 Monkey Patch

功能：
1. 禁用 _apply_token_truncation 中的实体/关系 token 截断
2. 禁用 _build_context_str 中的 chunk token 截断
3. 让所有过滤交由外部算法（PPR、rerank、chunk_top_k 等）控制

设计理念：
- LightRAG 内部不应有硬编码的截断逻辑
- 所有过滤应该由调用方通过参数控制
- 保留 chunk_top_k、rerank 等算法过滤

使用方法：
    在 RAG 初始化时调用 apply_no_truncation_patch()
"""

import logging

logger = logging.getLogger(__name__)


def patch_apply_token_truncation():
    """
    Patch _apply_token_truncation 函数，禁用实体/关系的 token 截断
    
    修改策略：
    - 完全跳过 token 截断逻辑
    - 直接返回所有实体和关系
    - 让外部算法（PPR、rerank）控制过滤
    """
    import lightrag.operate as operate_module
    import time
    
    async def patched_func(
        search_result: dict,
        query_param,
        global_config: dict[str, str],
    ) -> dict:
        """
        Patched version of _apply_token_truncation
        完全禁用 token 截断，直接返回所有实体和关系
        """
        final_entities = search_result["final_entities"]
        final_relations = search_result["final_relations"]
        
        entity_id_to_original = {}
        relation_id_to_original = {}
        
        entities_context = []
        for entity in final_entities:
            entity_name = entity["entity_name"]
            created_at = entity.get("created_at", "UNKNOWN")
            if isinstance(created_at, (int, float)):
                created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
            
            entity_id_to_original[entity_name] = entity
            
            entities_context.append(
                {
                    "entity": entity_name,
                    "type": entity.get("entity_type", "UNKNOWN"),
                    "description": entity.get("description", "UNKNOWN"),
                    "created_at": created_at,
                    "file_path": entity.get("file_path", "unknown_source"),
                }
            )
        
        relations_context = []
        for relation in final_relations:
            created_at = relation.get("created_at", "UNKNOWN")
            if isinstance(created_at, (int, float)):
                created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
            
            if "src_tgt" in relation:
                entity1, entity2 = relation["src_tgt"]
            else:
                entity1, entity2 = relation.get("src_id"), relation.get("tgt_id")
            
            relation_key = (entity1, entity2)
            relation_id_to_original[relation_key] = relation
            
            relations_context.append(
                {
                    "entity1": entity1,
                    "entity2": entity2,
                    "description": relation.get("description", "UNKNOWN"),
                    "created_at": created_at,
                    "file_path": relation.get("file_path", "unknown_source"),
                }
            )
        
        logger.info(
            f"Token truncation disabled: {len(entities_context)} entities, "
            f"{len(relations_context)} relations (no truncation applied)"
        )
        
        return {
            "entities_context": entities_context,
            "relations_context": relations_context,
            "filtered_entities": final_entities,
            "filtered_relations": final_relations,
            "entity_id_to_original": entity_id_to_original,
            "relation_id_to_original": relation_id_to_original,
        }
    
    operate_module._apply_token_truncation = patched_func
    
    logger.info("✅ 已 patch _apply_token_truncation 禁用实体/关系 token 截断")


def patch_build_context_str():
    """
    Patch _build_context_str 函数，禁用 chunk 的 token 截断
    
    修改策略：
    - 设置 max_total_tokens 为非常大的值（1M）
    - 有效禁用 chunk token 截断
    - 让 chunk_top_k 和 rerank 控制最终 chunk 数量
    """
    import lightrag.operate as operate_module
    
    async def patched_func(
        entities_context: list[dict],
        relations_context: list[dict],
        merged_chunks: list[dict],
        query: str,
        query_param,
        global_config: dict[str, str],
        chunk_tracking: dict = None,
        entity_id_to_original: dict = None,
        relation_id_to_original: dict = None,
    ):
        """
        Patched version of _build_context_str
        禁用 chunk token 截断
        """
        from lightrag.utils import (
            convert_to_user_format,
            process_chunks_unified,
            generate_reference_list_from_chunks,
        )
        from lightrag.prompt import PROMPTS
        import json
        
        tokenizer = global_config.get("tokenizer")
        if not tokenizer:
            logger.error("Missing tokenizer, cannot build LLM context")
            empty_raw_data = convert_to_user_format(
                [], [], [], [], query_param.mode,
            )
            empty_raw_data["status"] = "failure"
            empty_raw_data["message"] = "Missing tokenizer, cannot build LLM context."
            return "", empty_raw_data
        
        max_total_tokens = 1000000
        
        sys_prompt_template = global_config.get(
            "system_prompt_template", PROMPTS["rag_response"]
        )
        
        kg_context_template = PROMPTS["kg_query_context"]
        user_prompt = query_param.user_prompt if query_param.user_prompt else ""
        response_type = (
            query_param.response_type
            if query_param.response_type
            else "Multiple Paragraphs"
        )
        
        entities_str = "\n".join(
            json.dumps(entity, ensure_ascii=False) for entity in entities_context
        )
        relations_str = "\n".join(
            json.dumps(relation, ensure_ascii=False) for relation in relations_context
        )
        
        pre_kg_context = kg_context_template.format(
            entities_str=entities_str,
            relations_str=relations_str,
            text_chunks_str="",
            reference_list_str="",
        )
        kg_context_tokens = len(tokenizer.encode(pre_kg_context))
        
        pre_sys_prompt = sys_prompt_template.format(
            context_data="",
            response_type=response_type,
            user_prompt=user_prompt,
        )
        sys_prompt_tokens = len(tokenizer.encode(pre_sys_prompt))
        
        query_tokens = len(tokenizer.encode(query))
        buffer_tokens = 200
        available_chunk_tokens = max_total_tokens - (
            sys_prompt_tokens + kg_context_tokens + query_tokens + buffer_tokens
        )
        
        logger.info(
            f"Token allocation - Total: {max_total_tokens}, SysPrompt: {sys_prompt_tokens}, "
            f"Query: {query_tokens}, KG: {kg_context_tokens}, Buffer: {buffer_tokens}, "
            f"Available for chunks: {available_chunk_tokens}"
        )
        
        truncated_chunks = await process_chunks_unified(
            query=query,
            unique_chunks=merged_chunks,
            query_param=query_param,
            global_config=global_config,
            source_type=query_param.mode,
            chunk_token_limit=available_chunk_tokens,
        )
        
        reference_list, truncated_chunks = generate_reference_list_from_chunks(
            truncated_chunks
        )
        
        chunks_context = []
        for chunk in truncated_chunks:
            chunks_context.append(
                {
                    "reference_id": chunk["reference_id"],
                    "content": chunk["content"],
                }
            )
        
        text_units_str = "\n".join(
            json.dumps(text_unit, ensure_ascii=False) for text_unit in chunks_context
        )
        reference_list_str = "\n".join(
            f"[{ref['reference_id']}] {ref['file_path']}"
            for ref in reference_list
            if ref["reference_id"]
        )
        
        logger.info(
            f"Final context: {len(entities_context)} entities, "
            f"{len(relations_context)} relations, {len(chunks_context)} chunks"
        )
        
        if not entities_context and not relations_context and not chunks_context:
            empty_raw_data = convert_to_user_format(
                [], [], [], [], query_param.mode,
            )
            empty_raw_data["status"] = "failure"
            empty_raw_data["message"] = "Query returned empty dataset."
            return "", empty_raw_data
        
        if truncated_chunks and chunk_tracking:
            chunk_tracking_log = []
            for chunk in truncated_chunks:
                chunk_id = chunk.get("chunk_id")
                if chunk_id and chunk_id in chunk_tracking:
                    tracking_info = chunk_tracking[chunk_id]
                    source = tracking_info["source"]
                    frequency = tracking_info["frequency"]
                    order = tracking_info["order"]
                    chunk_tracking_log.append(f"{source}{frequency}/{order}")
                else:
                    chunk_tracking_log.append("?0/0")
            
            if chunk_tracking_log:
                logger.info(f"Final chunks S+F/O: {' '.join(chunk_tracking_log)}")
        
        result = kg_context_template.format(
            entities_str=entities_str,
            relations_str=relations_str,
            text_chunks_str=text_units_str,
            reference_list_str=reference_list_str,
        )
        
        logger.debug(
            f"[_build_context_str] Converting to user format: "
            f"{len(entities_context)} entities, {len(relations_context)} relations, "
            f"{len(truncated_chunks)} chunks"
        )
        final_data = convert_to_user_format(
            entities_context,
            relations_context,
            truncated_chunks,
            reference_list,
            query_param.mode,
            entity_id_to_original,
            relation_id_to_original,
        )
        logger.debug(
            f"[_build_context_str] Final data after conversion: "
            f"{len(final_data.get('entities', []))} entities, "
            f"{len(final_data.get('relationships', []))} relationships, "
            f"{len(final_data.get('chunks', []))} chunks"
        )
        return result, final_data
    
    operate_module._build_context_str = patched_func
    
    logger.info("✅ 已 patch _build_context_str 禁用 chunk token 截断")


def apply_no_truncation_patch():
    """
    应用所有禁用截断的 patch
    
    在 RAG 初始化时调用此函数
    """
    patch_apply_token_truncation()
    patch_build_context_str()
    
    logger.info("✅ 所有硬编码截断已禁用，过滤交由外部算法控制")
