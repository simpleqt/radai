# 第三遍补充：`chat_file_index.py`

## 1. 端到端调用链

```text
┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐
│         用户          │ │    chat_files 路由    │ │     my_files 路由     │ │   ChatFileParser    │ │ChatFileIndexManager │ │    ChatFileIndex    │ │ LightweightQAAgent  │ │chat_file_search_tool│
└─────────────────────┘ └─────────────────────┘ └─────────────────────┘ └─────────────────────┘ └─────────────────────┘ └─────────────────────┘ └─────────────────────┘ └─────────────────────┘

  [Note A: 或工具 chat_file_search 再次 search]

```

### 索引建立时机差异（跨路由）

| 入口 | 何时 `add_document` | 未建索引场景 |
|------|---------------------|--------------|
| `chat_files/upload` | 上传成功且 `long_doc` | 索引 try 失败仍 commit DB |
| `my_files/upload` | **不建索引** | 仅 DB+MinIO |
| `my_files/attach` | 绑定时读 `metadata.content` | content 缺失则跳过 |
| 服务重启 / LRU 驱逐 | 无自动重建 | 仅有 attach/upload 可恢复 |

---

## 2. 重复检索路径

同一会话可能**两次**检索同一索引：

1. `LightweightQAAgent._do_file_search()` 内联 `file_idx.search`
2. 动态注入的 `chat_file_search` 工具再次 `search`

若 Agent 既注入工具又走内联检索，存在重复逻辑与 token 浪费（需读 Agent 配置确认是否互斥）。

---

## 3. 内存粗算

假设：块大小 800 字符，中文为主，每块 ~1.2KB 原文 + BM25 结构 + TF-IDF 稀疏向量。

| 规模 | 块数 | 粗算内存/会话 |
|------|------|----------------|
| 1 份 50 页 PDF 长文 | ~200 | ~1–3 MB |
| 5 份长文 | ~1000 | ~5–15 MB |
| LRU 100 会话满载 | — | **0.5–1.5 GB** 量级 |

模块注释「100×200KB≈20MB」**偏乐观**，未计 BM25 `term_positions` 与中文 sliding window 词项爆炸。

---

## 4. `split_text` 边界用例（未测试）

| 输入 | 当前行为 | 期望风险 |
|------|----------|----------|
| `""` | `[]`，`add_document` 返回 0 | 上传路由应判 failed |
| 仅 `\n\n\n` | 可能 `[]` 或空块 | 同上 |
| 无 `\n\n` 的 10 万字单行 | 按句切，仍可能超大块 | 单块超 chunk_size 时按句循环，性能差 |
| 英文无空格长串 | 整段作 para | 单块过大 |

---

## 5. BM25 增量 `add_document` 与 TF-IDF 全量重建

每次 `add_document`：

- BM25：`add_document` 增量 ✓
- TF-IDF：`_rebuild_tfidf()` **全量** ✗

上传第 N 个文件时，前 N-1 个文件的 IDF 随文档频率变化被重算，**历史块向量会变**，无版本号；并发读 `search` 可能读到半更新状态（无锁）。

---

## 6. `search` 行为矩阵

| keywords | generic query | BM25 结果 | 最终路径 |
|----------|---------------|-----------|----------|
| 有，命中 | * | * | 关键词路，立即返回 |
| 有，未命中 | false | 有 | BM25+TF-IDF |
| 有，未命中 | false | 无 | `_head_chunks` |
| 无/空 | true | — | `_head_chunks`（跳过 BM25） |
| 无/空 | false | 有 | BM25+TF-IDF |
| 无/空 | false | 无 | `_head_chunks` |

**keywords 空列表** `[]`：`if keywords:` 为 false，不走关键词路——正确。

---

## 7. 与方案文档关系

`plans/无LightRAG精细化检索方案.md` 描述的是**知识库 Milvus/Redis** 检索，与会话级 `ChatFileIndex` **正交**：

- 勿混淆两套 BM25（`rag/keywords/bm25_index` 用于 KB 与 chat 共用类但实例独立）。
- `plans/关键词提取落地方案.md` 中 LLM 关键词 → 本模块 `_keyword_search` 已实现，Agent 侧 `_extract_file_search_terms` 为上游。

---

## 8. 第三遍新增问题

### P1-NEW 删除会话文件不清理 Manager

`chat_files.delete` 仅 `index.remove_document(file_id)`，**不**在会话块数为 0 时 `manager.remove(thread_id)`，空壳索引仍占 LRU 槽位。

### P2-NEW `my_files` 软删除不清理索引

`soft_delete_my_file` 未调用 `remove_document` / `manager.remove`，已 attach 的会话内存索引**仍可查已删文件**内容（直至 LRU 驱逐）。

### P2-NEW `chunk_index` 在 SearchResult 中的语义

返回的是**文件内**块序号，非全局 `_chunks` 下标；多文件时 Agent 展示可能误解。

### P3-NEW 工具层 `is_kw_match` 启发式

`any(r.score >= 0.5 for r in results)` 与关键词路 `hit_count/len(kws)` 归一化分数绑定，改关键词数量会改变 label 判定。

---

## 9. 建议监控指标（运维）

- `ChatFileIndexManager` 当前 size / eviction 次数
- 每会话 `chunk_count`、`file_count`
- `search` 三路命中比例（keyword / bm25 / head_fallback）
- `add_document` 耗时 P99

---

## 10. 第三遍优先修复项（本文件）

1. 实现 `chunk_overlap` 或删除参数  
2. `asyncio.Lock` 保护 Manager + Index 写操作  
3. `add_document` 同 `file_id` 先删后加（幂等）  
4. 跨路由：软删除/解绑时同步 `remove_document`  
5. 公开 `is_generic_summary_query`，停止引用 `_chunks`
