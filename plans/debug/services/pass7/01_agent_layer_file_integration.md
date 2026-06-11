# 第七遍：Agent 层与 `backend/services` 集成审查

> 范围：`custom/agents`、`custom/api/v2/agent.py`、`tool_assembly`、`chat_file_search_tool`  
> 不改代码。

---

## 1. 两条 Agent 路径对比

```text
┌────────────────┐  ┌─────────────────────┐
│ API            │  │ chat_file_search 工具 │
└────────────────┘  └─────────────────────┘
           │
           ▼
           │
           ▼
┌──────────────────────────────────────┐
│ Inject                               │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ NE                                   │
└──────────────────────────────────────┘
         (lightweight_mode)
           │
           ▼
         (else)
           │
           ▼
┌────────────────┐  ┌────────────────┐
│ Graph          │  │ LQA            │
└────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
┌────────────────┐  ┌─────────────────────┐  ┌────────────────────┐
│ 模型可再调工具        │  │ chat_file_search 工具 │  │ 内联 file_idx.search │
└────────────────┘  └─────────────────────┘  └────────────────────┘

```

| 维度 | Lightweight 模式 | 完整 Graph 模式 |
|------|------------------|-----------------|
| `chat_file_search` 工具 | **无**（提前 return，不走 ToolAssembly） | 有（`has_thread_file_index`） |
| 文档检索 | 请求前 **内联** `search()` | 依赖模型调工具 + 可选内联无 |
| 短文档 | 经 `agent.py` 注入 `runtime_question` | 同左 |
| 长文档前缀 | 内存 index 拼 `permanent_file_prefix` + 内联检索结果进 System | `metadata.content` 前缀 + 工具提示 |
| KB 关键词 | `_extract_search_query_v2` LLM | 图内 `multi_kb_search` 等 |

**结论**：轻量模式与完整模式行为**不一致**；审查 services 缺陷时需标明路径。

---

## 2. `agent.py` 文件注入（V2 API）

**位置**：`backend/custom/api/v2/agent.py` ~819–899

| 行为 | 依赖 services | 问题 |
|------|---------------|------|
| `get_files_by_thread` 补全 `file_ids` | `chat_files` 模块函数 | ✓ JOIN 正确 |
| `get_file_meta(fid)` 同步读 DB | 非 services | 阻塞 event loop（P2） |
| short_doc 全文进 `runtime_question` | `metadata.content` | 每轮重复 token（P2） |
| long_doc 前 1000 字进 question | `metadata.content` | 索引被驱逐后**仍可从 DB 读前缀**（P2） |
| long_doc 提示用 `chat_file_search` | 工具仅 Graph 模式存在 | **Lightweight 下提示与能力不符**（P1） |
| multimodal base64 | metadata | 体积/内存（P1） |

### P1-AG-01 Lightweight 路径提示矛盾

`agent.py` 在长文档名后追加：

```text
如需从文档中查找信息请使用 chat_file_search 工具

```

但 `lightweight_mode=True` 时 **未注入** 该工具，实际靠 `LightweightQAAgent` 内联检索。模型可能幻觉「调用工具」或漏检。

**建议**：按 `lightweight_mode` 分支文案，或统一注入 facade API。

---

## 3. `LightweightQAAgent` 与 services

### 3.1 内联检索（主路径）

```python
results = file_idx.search(file_search_query, top_k=5, keywords=file_keywords)
```

- 与 `chat_file_search_tool` 逻辑重复（格式化/header 不同）。
- 使用私有 `_is_generic_summary_query`、`_chunks`（P1 封装，见 pass3）。

### 3.2 检索词双 LLM 调用

| 步骤 | 函数 | 用途 |
|------|------|------|
| 1 | `_extract_file_search_terms` | 文档检索 query + keywords |
| 2 | `_extract_search_query_v2` | KB/Web query + skip_kb |

有文件索引时可能 **连续两次** 小模型调用（+ 主模型），延迟叠加（P2）。

### 3.3 上下文拼接上限

| 片段 | 上限 |
|------|------|
| `file_index_context` | 6000 字符 |
| `permanent_file_prefix` | 每文件 `long_doc_prefix_length`（默认 1000） |
| KB | `_compact_kb_context` |
| Web | 4000 |

内联检索结果 + 前缀 + short_doc（在 question 里）可能 **远超** 设计「长文档不进上下文」意图（P1）。

### 3.4 `skip_kb` 与文件问题

`_extract_search_query_v2` 可返回 `SKIP_KB`，避免文件问题误查 KB——设计良好。  
与 `_extract_file_search_terms` 旁路通用总结查询配合，逻辑分散在 agent 非 services。

---

## 4. `ToolAssemblyPipeline`

```python
if has_thread_file_index(self._thread_id):
    tools_by_name[file_search.name] = file_search
```

| 项 | 说明 |
|----|------|
| 条件 | 仅内存索引存在 |
| 索引被 LRU 驱逐 | **不注入工具**，但 `agent.py` 仍可能从 DB 注入 long 前缀（不一致） |
| 异常 | 捕获后 warning，不阻断其他工具 |

---

## 5. `chat_file_search_tool`

| 项 | 评估 |
|----|------|
| 作用域绑定 `thread_id` | ✓ |
| `top_k` clamp | ✓ |
| `clean_document_images` 二次清理 | ✓ 防御性 |
| 与 LQA 内联重复 | P2 维护成本 |

---

## 6. `artifact_utils` → `user_file_service`

- `register_minio_artifact_for_thread_sync` 在异步流中 `to_thread` 调用——合理。
- 登记失败时 `file_id` 仍为 artifact 路径，下载 404（pass3/04）。

与 `ChatFileIndex` / `ChatFileParser` **无直接关系**。

---

## 7. 架构文档对照

| 文档 | 与代码 |
|------|--------|
| [agent_architecture_redesign.md](../../../docs/agent_architecture_redesign.md) | 未单独描述「会话文件 BM25」；轻量通道已落地 |
| [chat_file_upload_design.md](../../../docs/chat_file_upload_design.md) | 写 MinerU「未接入」——**过时**（Parser 已接） |
| [关键词提取落地方案](../../关键词提取落地方案.md) | 指 KB `PerKBKeywordExtractor`，**非** `ChatFileIndex` 分词 |

**勿混淆**：KB 关键词（Milvus/Redis） vs 会话文件（内存 BM25+TF-IDF）。

---

## 8. 第七遍新增问题汇总

| ID | 级别 | 描述 |
|----|------|------|
| AG-P1-01 | P1 | Lightweight 模式提示使用 `chat_file_search` 但无此工具 |
| AG-P1-02 | P1 | 长文档：DB 前缀 + 内联检索 + 内存前缀，三重注入 |
| AG-P1-03 | P1 | 索引驱逐后 Graph 无工具、LQA 无内联，但 agent.py 仍注入 DB 前缀 |
| AG-P2-01 | P2 | `get_file_meta` 同步 DB |
| AG-P2-02 | P2 | 内联 search 与 tool 重复实现 |
| AG-P2-03 | P2 | 双 LLM 提取检索词延迟 |
| AG-P3-01 | P3 | `csh_agent.py` 与 `agent.py` 文件注入重复 |

---

## 9. 建议（文档/架构，非代码）

1. 在 `agent_engine_module.md` 增加「会话文件检索」小节，链接 `personal_files_module.md`。  
2. 更新 `chat_file_upload_design.md` MinerU 状态为「可选依赖」。  
3. 抽取 `FileContextFacade`：统一「是否有索引 / 重建 / 格式化检索 / 提示文案」。  
4. Lightweight 与 Graph 对 long_doc 的策略表写入 `EXECUTIVE_SUMMARY` 附录。

---

*第七遍 — Agent 层*
