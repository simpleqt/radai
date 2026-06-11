# 第四遍：`lightweight_qa_agent` 与文件检索集成

> 文件：`backend/custom/agents/lightweight_qa_agent.py`  
> 依赖 services：`chat_file_index`（非直接调用 parser）

---

## 1. 检索流水线（文件相关部分）

```
用户问题
  → has_file_index? (get_index_manager.get(thread_id))
  → needs_model_query 含 has_file_index
  → _extract_file_search_terms (LLM) 或通用问句旁路
  → _do_file_search: file_idx.search(file_search_query, keywords)
  → 注入 file_index_context 到 prompt
  （并行）chat_file_search 工具可供模型再次调用
```

---

## 2. 发现的问题

| ID | 级别 | 描述 |
|----|------|------|
| AG-01 | P1 | import `_is_generic_summary_query`、读 `_chunks` 破坏封装 |
| AG-02 | P2 | 内联 search 与 `chat_file_search` 工具逻辑重复（header/label 两套） |
| AG-03 | P2 | 通用问句固定英文 QUERY，与中文文档问答产品语气可能不符 |
| AG-04 | P2 | `file_names = [c.file_name for c in file_idx._chunks[:1]]` 仅首块文件名作 hint |
| AG-05 | P3 | 预览前缀 `long_doc_prefix_length` 依赖 settings，默认 1000 字 |
| AG-06 | P1 | 索引被 LRU 驱逐后 `has_file_index=False`，无 DB 重建，用户无感知 |

---

## 3. 与 `chat_file_search_tool` 契约

| 字段 | Agent 内联 | Tool |
|------|------------|------|
| top_k | 5 固定 | 1–10 可调 |
| keywords | 来自 LLM | 模型工具参数 |
| clean_images | 未调用 | ✓ |

建议抽取 `format_search_results(results, index)` 共享。

---

## 4. 建议改动（任务级）

1. 新建 `backend/services/chat_file_search_facade.py`（或 index 模块公开 API）：  
   - `is_generic_summary_query`  
   - `search_and_format(thread_id, query, keywords, top_k)`  
2. Agent 与 Tool 均调用 facade。  
3. `has_file_index` 为 false 时尝试 `rebuild_index_from_session_files(thread_id)`（读 DB metadata）。

---

## 5. 测试建议

- Mock index 空 / 有块 / 驱逐后。  
- 通用中文问句「这篇论文讲了什么」是否旁路 LLM。  
- keywords 为空与 BM25 回退路径。
