# 第七遍：知识库检索 vs 会话文件检索边界

> 防止将 `backend/services` 审查结论误用于 RAG 管线。  
> 关联：[无LightRAG精细化检索方案](../../无LightRAG精细化检索方案.md)、[关键词提取落地方案](../../关键词提取落地方案.md)

---

## 1. 两套系统对比

| 维度 | 知识库（RAG） | 会话文件（services） |
|------|---------------|---------------------|
| 入口 | `uploads` / `kb_docs` / ingest worker | `chat_files` / `my_files` attach |
| 存储 | Milvus chunks、图谱、MySQL 文档表 | `user_files.metadata_` + MinIO |
| 索引 | 持久化、按 kb_id | 进程内存 `ChatFileIndexManager` |
| 检索 API | `rag.service.retrieve_context` | `ChatFileIndex.search` |
| 关键词 | `PerKBKeywordExtractor` hybrid | LLM 提取 + 规则 `_is_generic_summary_query` |
| BM25 实现 | `rag/keywords/bm25_index.py`（Redis 等） | **同一类** `BM25Index`，**不同实例** |
| Agent 工具 | `multi_kb_search` | `chat_file_search` |
| 配置 | settings、Profile MMR_ONLY | 硬编码 800/5000/100 块 |

---

## 2. 代码依赖图（services 不应牵动 RAG）

```
backend/services/chat_file_index.py
  └── imports BM25Index from backend.rag.keywords.bm25_index

backend/rag/service.py
  └── 不 import chat_file_index

backend/services/startup_task_processor.py
  └── get_rag_service().process_multimodal  # KB 入库，非 ChatFileIndex
```

**修改 `BM25Index._tokenize` 会影响两处**，需回归：

1. 会话 `ChatFileIndex` 检索质量  
2. KB 侧 Redis BM25（若使用同一 tokenizer）

---

## 3. 审查结论归属

| 问题 | 归属 |
|------|------|
| chunk_overlap 未实现 | **仅会话** |
| func.replace 目录路径 | **仅 KB** directory_service |
| 三 ingest consumer | **仅 KB** PendingFile |
| export-docx 无鉴权 | **仅用户文件路由** |
| PerKB 关键词 50–200ms | **仅 KB**（已实现） |
| 轻量 Agent 内联 search | **Agent + 会话** |

---

## 4. 统一 tokenizer 的利弊（架构建议）

| 方案 | 优点 | 缺点 |
|------|------|------|
| 会话独立 tokenizer | 不动 RAG 生产 | 仍不一致 |
| 共享 `rag.keywords.tokenizer` | 一致、可测 | KB 回归范围大 |
| 会话改 embedding 检索 | 质量上限高 | 违背「无 embedding」设计 |

当前设计文档 [chat_file_upload_design.md](../../../docs/chat_file_upload_design.md) 明确会话索引 **纯 Python 无 embedding**——与 KB 向量路 **刻意分离**。

---

## 5. Agent 同时用两路的协调

`LightweightQAAgent` 并发：

- `multi_kb_search` → RAG  
- `_do_file_search` → ChatFileIndex  
- `web_search`

`_extract_search_query_v2` 用 `SKIP_KB` 减少「只问上传文档」时的 KB 浪费——**会话与 KB 协调点在此**，不在 services 层。

---

## 6. 测试隔离建议

| 套件 | 范围 |
|------|------|
| `tests/services/test_chat_file_*` | 仅内存索引 |
| `tests/rag/test_retrieve_*` | 仅 KB |
| `tests/integration/test_agent_file_*` | 端到端，两路都 mock |

---

*第七遍 — RAG 边界*
