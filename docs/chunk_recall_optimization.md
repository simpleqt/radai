# Chunk 召回优化与硬编码截断移除说明

> **文档状态**：✅ 补丁已集成（`lightrag_chunk_patch.py` 等，见 §8）。  
> **现行检索主路径**：生产 Stage-1 为 **DirectRetriever 四路** + Profile 驱动 Stage-2（垂直库 **MMR_ONLY**），非下文所述 LightRAG 内置 `aquery_data` 单一路径。  
> **算法主文档**：[知识库算法和架构.md](./知识库算法和架构.md)

---

## 1. 问题描述

在 RAG 检索流程中，发现 **59 个召回实体仅能召回 16 个 chunks**，远低于预期（理论应召回更多）。

### 原始召回流程
1. 实体/关系向量检索 (top_k=60)
2. Token 截断 (max_entity_tokens=7000)
3. `pick_by_weighted_polling` 选择 chunks
4. round-robin 合并
5. `chunk_top_k` 截断 (chunk_top_k=30)
6. PPR 过滤
7. `chunk_top_k` 截断 (chunk_top_k=10)
8. **Token 截断 (max_total_tokens=50000)** ← 过于激进

## 2. 根本原因分析

### 2.1 Redis text_chunks_db 数据缺失
- **现象**：NebulaGraph entities 的 `source_id` 引用了 34,572 个 chunk IDs，但在 Redis `text_chunks_db` 中 **100% 不存在**。
- **影响**：`_find_related_text_unit_from_entities` 和 `_find_related_text_unit_from_relations` 无法从 Redis 获取 chunk 数据，导致 `entity_chunks` 和 `relation_chunks` 为空。
- **结果**：round-robin 只能合并 `vector_chunks`（30 个），最终被截断为 16 个。

### 2.2 Milvus chunks_vdb 数据存在
- **发现**：Milvus `chunks_vdb` 通过动态字段（`enable_dynamic_field=True`）存储了 `content`、`file_path` 等数据。
- **机会**：可以从 Milvus fallback 查询 chunk 数据，弥补 Redis 缺失。

### 2.3 LightRAG 硬编码截断过于激进
- **现象**：`_apply_token_truncation` 和 `_build_context_str` 中有硬编码的 token 截断逻辑。
- **影响**：
  - 实体/关系被截断：122 entities → 73, 332 relations → 173
  - chunks 被截断：404 merged_chunks → 16 final_chunks
- **问题**：已有 7 层过滤机制（向量检索、PPR、rerank、chunk_top_k 等），第 8 层 token 截断导致召回严重不足。

## 3. 解决方案

### 3.1 Milvus Fallback Patch
**文件**：`backend/rag/utils/lightrag_chunk_patch.py`

**功能**：
- Patch `_find_related_text_unit_from_entities` 和 `_find_related_text_unit_from_relations`。
- 优先从 Redis `text_chunks_db` 查询 chunk 数据。
- 如果 Redis 缺失，自动 fallback 到 Milvus `chunks_vdb` 查询（通过动态字段获取 `content`）。

**效果**：
- entity_chunks 从 0 增加到 354 个
- relation_chunks 从 0 增加到 191 个

### 3.2 禁用硬编码截断 Patch
**文件**：`backend/rag/utils/lightrag_no_truncation_patch.py`

**功能**：
- Patch `_apply_token_truncation`：完全跳过实体/关系的 token 截断，直接返回所有实体和关系。
- Patch `_build_context_str`：设置 `max_total_tokens=1000000`，有效禁用 chunk token 截断。
- 所有过滤交由外部算法控制（PPR、rerank、chunk_top_k）。

**效果**：
- entities 从 73 增加到 122 个（+67%）
- relations 从 173 增加到 332 个（+92%）
- merged_chunks 从 404 增加到 542 个（+34%）
- final_chunks 从 16 增加到 30 个（+87.5%）

### 3.3 集成到 RAG 初始化流程
**文件**：`backend/rag/knowledge_base/manager.py`

**修改**：
```python
from backend.rag.utils.lightrag_chunk_patch import apply_chunk_query_patch
from backend.rag.utils.lightrag_no_truncation_patch import apply_no_truncation_patch
apply_chunk_query_patch()
apply_no_truncation_patch()
```

## 4. 测试结果对比

| 指标 | 优化前 | 优化后 | 变化 |
|------|--------|--------|------|
| entities_after_truncation | 73 | **122** | **+67%** |
| relations_after_truncation | 173 | **332** | **+92%** |
| merged_chunks_count | 404 | **542** | **+34%** |
| final_chunks_count | 16 | **30** | **+87.5%** |

## 5. 优化后的过滤流程

```
1. 实体/关系向量检索 (top_k=60)
2. ✅ Token 截断 (已禁用)
3. pick_by_weighted_polling 选择 chunks
4. round-robin 合并 (542 chunks)
5. chunk_top_k 截断 (chunk_top_k=30)
6. PPR 过滤
7. chunk_top_k 截断 (chunk_top_k=10)
8. ✅ Token 截断 (已禁用，max_total_tokens=1000000)
```

## 6. 与 DirectRetriever 的关系（2026-05）

- **Path-C**（`chunk_searcher.py`）直接查询 Milvus `chunks_vdb`，不依赖 LightRAG round-robin，是生产 chunk 召回主路径。
- **实体链路拉 chunk** 仍可能走 `get_chunks_with_milvus_fallback`（`lightrag_chunk_patch.py`），用于弥补 Redis `text_chunks` 缺失。
- 垂直库 Stage-2 使用 **MMR_ONLY**，不再经过 PPR；本文 §5 中含 PPR 的步骤仅适用于 `FULL` Profile 或 LightRAG 回退路径。

## 7. 后续建议

### 7.1 参数调整
如需召回更多 chunks，可调整以下参数：
- `chunk_top_k`: 从 30 增加到 50-100
- `top_k`: 从 60 增加到 100-200（召回更多实体）
- `max_entity_tokens`: 从 7000 增加到 10000-15000

### 7.2 数据修复
- **Redis 数据恢复**：执行 `backend/scripts/restore_chunks_from_milvus.py` 从 Milvus 恢复 chunk 数据到 Redis。
- **实体名称归一化**：执行 `backend/scripts/refactor_entity_names.py` 合并实体名称变体（大小写、空格差异），预计可合并 47,855 个实体。

## 8. 相关文件

### 新增文件
- `backend/rag/utils/lightrag_chunk_patch.py` - Milvus Fallback Patch
- `backend/rag/utils/lightrag_no_truncation_patch.py` - 禁用所有硬编码截断 Patch
- `backend/rag/utils/chunk_query_fallback.py` - Fallback 工具函数
- `backend/scripts/restore_chunks_from_milvus.py` - 数据恢复脚本
- `backend/scripts/refactor_entity_names.py` - 实体名称重构脚本
- `backend/scripts/test_no_truncation.py` - 测试脚本

### 修改文件
- `backend/rag/knowledge_base/manager.py` - 集成 Patch 到 RAG 初始化流程
