# 第八轮审查：`chat_file_search` + `explore_learning` + `explore` API

**日期**：2026-05-30 · **只读**

---

## Part A — `chat_file_search_tool.py`（102 行）

### 设计

- 工厂 `create_scoped_chat_file_search_tool(thread_id)` 闭包绑定会话。
- 检索：`backend.services.chat_file_index`（BM25 + 关键词子串 + fallback 文档开头）。
- `top_k` clamp 1–10；`clean_document_images` 避免无效图片 Markdown。

### thread_id 一致性（重要）

| 环节 | 使用的 thread_id |
|------|------------------|
| 上传 `chat_files/upload` Form | **外部** `request_thread_id`（L200 `get_or_create(thread_id)`） |
| `NEAgentRouter(..., thread_id=request_thread_id)` | **外部**（`agent.py` L799） |
| `ToolAssemblyPipeline(thread_id=self._thread_id)` | **外部** |
| LangGraph checkpoint | **内部** `effective_thread_id`（`build_internal_thread_key`） |

**结论**：索引键与工具注入键均为 **用户可见 thread_id**，与 checkpoint **分离** — 设计 intentional，文档需写清，避免误用 internal id 建索引。

### 问题

| 优先级 | 问题 |
|--------|------|
| P2 | 索引在 **进程内存**（`ChatFileIndexManager` LRU）；多 worker 时上传在 worker A、问答在 worker B → **无索引**。 |
| P3 | 工具无 `consume_tool_budget`；模型可无限次 `chat_file_search`（成本低于 multi_kb 但仍可刷）。 |
| P3 | fallback 返回文档开头（score=0），模型可能误当「命中」— 文案已提示，可接受。 |

---

## Part B — `explore_learning.py` 检索路径

### `_retrieve_context`（L177–191）

```177:191:backend/custom/services/explore_learning.py
async def _retrieve_context(query: str, kb_id: str | None, *, chunk_top_k: int = 8) -> tuple[str, list[dict[str, Any]]]:
    try:
        from backend.api.routers.rag import retrieve_context
        filtered_data, context_str, _images, _stats = await retrieve_context(...)
```

- **调用方**：`gather_resources`（L486）、`generate_quiz`（L510）。
- **不调用方**：`generate_map`、`expand_node`（注释明确 LLM-only，L434–435、L465–466）、`generate_topics`（预制列表）。

### 与文档差距

| 能力 | 文档常见描述 | 代码 |
|------|--------------|------|
| 脑图 map | 可能写「结合 KB」 | **仅 LLM**，`context=""` |
| 展开 expand | 可能写检索 | **仅 LLM** |
| 关联资源 resources | 检索 | ✅ `_retrieve_context` |
| 习题 quiz | 检索出题 | ✅ |

→ 维护 `explore_module.md` 时以本表为准（见 `backend_custom_docs_vs_code_gaps.md`）。

### P2：失败静默降级

- `except Exception` → `return "", []`；前端得到 **空资源/无出题依据** 但不报错，难以排障。
- **建议**：响应中带 `grounded: false` + `retrieval_error: true`（部分已有 `grounded` 字段）。

### P2：资源下载路径解析（L218+）

- 支持 local / minio / `PendingFile` 多路径回退，逻辑复杂。
- `HTTPException(404, detail=str(exc))`（`explore.py` L170）可能 **外泄** 内部路径信息。

---

## Part C — `api/v2/explore.py` 鉴权与 detail SSE

### 鉴权现状（P1-3 细化）

| 路由 | `Depends(get_current_user)` |
|------|----------------------------|
| `POST /topics` | ❌ 无 |
| `POST /map` | ❌ |
| `POST /expand` | ❌ |
| `POST /resources` | ❌ |
| `POST /quiz` | ❌ |
| `POST /quiz/grade` | ❌ |
| `POST /detail` | ❌ |
| `GET /resources/download` | ✅ L157 |

- 模块 docstring 写「登录可选」，但 **POST 全系无鉴权**，仅下载需登录。
- 任意人可刷 LLM（map/expand/quiz/detail）与 `retrieve_context`（resources/quiz）→ **成本与滥用**。

### `explore/detail` 与 P0-2

```234:242:backend/custom/api/v2/explore.py
            agent = AgentClass(
                ...
                lightweight_mode=True,
                kb_ids=[kb_id] if kb_id else None,
```

- 走 `LightweightQAAgent` → **内置** `execute_multi_kb_search(self._kb_ids)`（可带 CSH `kb_id`）。
- 但系统 Prompt 仍为 **`LIGHTWEIGHT_SYSTEM_PROMPT` 核电子学**（`lightweight_qa_agent.py` L39+）— **CSH domain 详解仍用 NE 人设**（P0-2 在 Explore 场景的延伸）。
- Docstring L214–216 写「自动完成知识库检索」— **与实现一致**；「按 domain 选智能体」— **仅 KB ID 不同，Prompt 未切换**。

### P2：`explore/detail` SSE

- L250–252：chunk **原样** 输出，无 think-tag 过滤（同 P0-3）。
- L260：`{'error': str(exc)}` 外泄。

### P3：临时 thread_id

- `thread_id = f"explore__{uuid}"` — 无持久会话；合理。每次新建 agent/graph 实例，**冷启动成本**。

---

## Part D — 修复建议摘要

| 优先级 | 项 |
|--------|-----|
| P1 | Explore POST 鉴权或 IP 限流 |
| P0-2 | Explore detail + CSH：`csh_prompts` 轻量 Prompt |
| P2 | chat_file 索引跨 worker（Redis/DB 或 sticky session） |
| P2 | `_retrieve_context` 失败可观测 |
| P2 | detail/download 异常脱敏 |

---

*关联：[deep_dive_multi_kb_explore_tools.md](./deep_dive_multi_kb_explore_tools.md) · [backend_custom_round7_tasks_streaming_misc.md](./backend_custom_round7_tasks_streaming_misc.md)*
