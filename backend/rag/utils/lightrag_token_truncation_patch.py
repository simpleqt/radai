"""
Token 截断逻辑调整 Monkey Patch

功能：
1. 调整 _build_context_str 中的 token 截断逻辑
2. 增加 max_total_tokens 默认值或禁用 token 截断
3. 让 chunk_top_k 和 PPR 等过滤机制控制 chunk 数量

使用方法：
    在 RAG 初始化时调用 apply_token_truncation_patch()
"""

import logging

logger = logging.getLogger(__name__)


def patch_build_context_str():
    """
    Patch _build_context_str 函数调整 token 截断逻辑
    
    修改策略：
    - 大幅增加 max_total_tokens 默认值（从 50000 到 200000）
    - 或者完全禁用 token 截断，让 chunk_top_k 控制 chunk 数量
    """
    import lightrag.operate as operate_module
    
    original_func = operate_module._build_context_str
    
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
        调整 token 截断逻辑，增加 max_total_tokens 默认值
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
        
        # ===== 关键修改：禁用 token 截断 =====
        # 已经有 7 层过滤机制：
        # 1. 实体/关系向量检索 (top_k=60)
        # 2. Token 截断 (max_entity_tokens=7000)
        # 3. pick_by_weighted_polling 选择 chunks
        # 4. round-robin 合并
        # 5. chunk_top_k 截断 (chunk_top_k=30)
        # 6. PPR 过滤
        # 7. chunk_top_k 截断 (chunk_top_k=10)
        # 第 8 层 token 截断过于激进，应该禁用
        # 
        # 设置一个非常大的值，有效禁用 token 截断
        max_total_tokens = 1000000  # 1M tokens，足够容纳所有 chunks
        # ==============================================
        
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
        for i, chunk in enumerate(truncated_chunks):
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
    
    logger.info("✅ 已 patch _build_context_str 禁用 token 截断 (max_total_tokens: 50000 -> 1000000)")


def apply_token_truncation_patch():
    """
    应用 token 截断调整 patch
    
    在 RAG 初始化时调用此函数
    """
    patch_build_context_str()
    
    logger.info("✅ Token 截断调整 patch 已应用")
