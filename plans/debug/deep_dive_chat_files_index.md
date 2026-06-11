# 深度审查：对话文件上传 + ChatFileIndex

> 文件：`api/routers/chat_files.py`、`services/chat_file_parser.py`、`services/chat_file_index.py`  
> 对照：[个人文件夹文件管理与上传复用设计方案.md](../个人文件夹文件管理与上传复用设计方案.md)

---

## 1. 架构

```
POST /api/v2/chat-files/upload
    → ChatFileParser（分类 multimodal/short/long）
    → UserFile + SessionFile（MySQL）
    → MinIO chat_files/{user_id}/{file_id}/
    → long_doc: ChatFileIndexManager.get_or_create(thread_id)

Agent query
    → get_file_meta / get_files_by_thread
    → 注入 short 全文 / long 前缀 + chat_file_search 提示
    → LightweightQAAgent._do_file_search → index.search()
```

---

## 2. `chat_files.py` 端点

| 端点 | 职责 |
|------|------|
| POST `/upload` | 最多 10 文件、50MB、扩展名白名单 |
| GET `/{thread_id}` | 列出会话绑定文件（校验 user_id） |
| DELETE `/{file_id}` | 解绑 SessionFile；long_doc 删索引块 |

### 2.1 与「个人文件夹」方案

| 方案目标 | 代码现状 |
|----------|----------|
| 上传进个人文件夹 | upload 写 `UserFile` + MinIO — **部分落地** |
| 从个人文件夹选文件进对话 | `my_files.py` + 前端 picker — 见 personal_files 文档 |
| 长文档索引联动 | 索引键为 **会话 thread_id**，非 user 级 |

---

## 3. 安全问题

| ID | 严重度 | 问题 |
|----|--------|------|
| CF-1 | **high** | `get_file_meta(file_id)` **不校验 owner_user_id** — 知 file_id 可越权读他人文件内容进 Agent |
| CF-2 | medium | `agent.py` 遍历 `payload.file_ids` 时无 user 归属校验 |
| CF-3 | low | 失败上传仍生成随机 file_id 写 failed 结果 — 无持久化，可接受 |

**建议**：`get_file_meta(file_id, user_id)` 强制 `owner_user_id == user_id`。

---

## 4. `ChatFileParser`（`chat_file_parser.py` ~399 行）

| 能力 | 说明 |
|------|------|
| 分类 | multimodal / short_doc / long_doc / failed |
| 阈值 | short ≤5000 字（与方案一致） |
| 解析 | pdf/docx/xlsx 等委托子解析器 |

**质量**：多处 `except Exception`；与 upload 路由耦合紧。

---

## 5. `ChatFileIndex`（`chat_file_index.py`）

### 5.1 类结构

| 类/函数 | 行约 | 职责 |
|---------|------|------|
| `ChunkMeta` / `SearchResult` | dataclass | 数据结构 |
| `_tokenize_simple` | 44 | 中文 bigram + 英文词 |
| `split_text` | 74 | 段落/句子感知分块 |
| **`ChatFileIndex`** | 110 | BM25 + TF-IDF 混合检索 |
| **`ChatFileIndexManager`** | 444 | LRU 100 会话 |
| `_is_generic_summary_query` | 509 | 轻量 QA 旁路 LLM 检索词 |

### 5.2 `ChatFileIndex` 方法

| 方法 | 说明 |
|------|------|
| `add_document` | 分块 + BM25 add + rebuild TF-IDF |
| `remove_document` | 全量 rebuild BM25（O(n)） |
| `search` | 关键词优先 + BM25 + TF-IDF + 顺序回退 |
| `chunk_count` | property |

### 5.3 设计优点

- 零 embedding API，延迟低（文档自称 60 块检索 <5ms）
- 与会话绑定，符合「上传文档仅本对话」产品逻辑

### 5.4 问题

| ID | 严重度 | 问题 |
|----|--------|------|
| IX-1 | medium | **进程内 LRU** — 多 worker 索引不共享；驱逐后需从 DB 重建（**无**自动重建路径） |
| IX-2 | medium | `remove_document` 全量 rebuild — 大文档删除贵 |
| IX-3 | low | 分词与 `rag/keywords/bm25_index` 两套中文分词 — 不一致 |
| IX-4 | low | 索引仅内存 — 服务重启丢失，直至用户重传或重触发索引构建 |
| IX-5 | info | `metadata_` 存 `content`/`image_base64` — 大行 JSON 在 MySQL |

---

## 6. Agent 集成点

| 组件 | 使用方式 |
|------|----------|
| `lightweight_qa_agent` | `get_index_manager().get(thread_id)` |
| `agent.py` | `get_files_by_thread` + `get_file_meta` 注入 |
| `tool_assembly` | `chat_file_search` 工具（若启用） |

**thread_id 来源**：`NEFullFeatureAgent` 构造时 `self._thread_id` 来自 API — 须与 upload 时 Form thread_id 一致。

---

## 7. 扩展名白名单

与 `my_files.py`、`uploads.py` **第三套** 列表 — 见 [cross_cutting_duplication.md](./cross_cutting_duplication.md)。

`chat_files` 含 `.py`、`.html` 等 — 安全上注意上传可执行内容类型（仅作文本解析则风险较低）。

---

## 8. 测试建议

1. 上传 long_doc → search 命中  
2. 删除绑定 → 索引块减少  
3. **越权**：用户 A 的 file_id 传给用户 B 的 query（应在修复后失败）  
4. LRU 驱逐后会话再问文档 — 行为预期（应文档化：可能检索不到）  
5. 通用总结问句走 `_is_generic_summary_query` 旁路  
