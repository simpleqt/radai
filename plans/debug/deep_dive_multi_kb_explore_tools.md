# 深度审查：multi_kb_search / explore_learning / tool_assembly

> 只读审查。

---

## 1. `multi_kb_search_tool.py`（1223 行 — God file）

### 1.1 结构

| 区块 | 代表函数 | 行量级 |
|------|----------|--------|
| 文档名/片段格式化 | `_normalize_doc_display_name`, `_format_fragment` | ~200 |
| 单库检索 | `_search_single_kb`, `_search_single_kb_with_timeout` | ~150 |
| 关键词/embedding | `_extract_shared_keywords_once`, `_prepare_shared_query_context` | ~200 |
| RRF 融合 | `_merge_results_rrf` | ~115 |
| 图片白名单 | `_build_image_whitelist_section` | ~60 |
| 对外入口 | `multi_kb_search_tool`, `execute_multi_kb_search`, `_execute_multi_kb_search_once` | ~400 |

### 1.2 关键问题

| ID | 严重度 | 问题 |
|----|--------|------|
| MK-1 | high | 1223 行单文件，测试与变更困难 |
| MK-2 | high | `rag_service._get_rag_instance` 私有 API |
| MK-3 | medium | 硬编码 KB 列表在工具/Agent 多处 |
| MK-4 | medium | Path-A fast keywords、RRF、profile 交织 |
| MK-5 | low | 同步/异步 httpx 混用（rerank 在 filter 层） |

### 1.3 与 ToolAssembly 关系

`ToolAssemblyPipeline._create_multi_kb_search_tool` 动态 `@tool` 包装 `execute_multi_kb_search`：

- 注入 `kb_ids`、`max_result_chars`（full agent 5000 字限制）
- skill gate：`nuclear-electronics` 未加载则拦截

---

## 2. `tool_assembly.py`（~250 行）

| 方法 | 职责 |
|------|------|
| `__init__` | 保存 kb/skills/thread 等 |
| `_load_declared_skill_tool` | **硬编码**工具名 → import |
| `_create_multi_kb_search_tool` | 闭包工具 |
| `_create_web_search_tool` 等 | 类似 |
| `build` | 汇总工具列表 |

**问题**：

- 新 skill 工具必须改 `_load_declared_skill_tool` if-else  
- `acdemic_search` 拼写错误继承  
- excel 工具返回 **list** 与其他单工具不一致  

---

## 3. `explore_learning.py`（676 行）

### 3.1 API 映射（`explore.py`）

| 路由 | 服务函数 | 响应 |
|------|----------|------|
| POST `/topics` | `generate_topics` | JSON |
| POST `/map` | `generate_map` | JSON |
| POST `/expand` | `expand_node` | JSON |
| POST `/detail` | **SSE** — 调 Agent lightweight | stream |
| POST `/resources` | `gather_resources` | JSON |
| POST `/quiz` | `generate_quiz` | JSON |
| POST `/quiz/grade` | `grade_answer` | JSON |

### 3.2 服务层函数

| 函数 | 职责 |
|------|------|
| `resolve_subject` | ne / csh 领域 |
| `_retrieve_context` | 调 RAG retrieve |
| `_llm_json` | 通用 JSON LLM |
| `generate_topics/map/expand/...` | 各场景 prompt |
| `download_kb_resource` | MinIO `_get_minio_client` |
| `_fallback_*` | LLM 失败模板 |

### 3.3 质量问题

| ID | 严重度 | 问题 |
|----|--------|------|
| EX-1 | medium | 676 行服务 + 271 行路由，无单测 |
| EX-2 | medium | detail SSE 用 `status` 键非 `content`（前端需兼容） |
| EX-3 | low | `logging.getLogger` 非 get_logger |
| EX-4 | low | 访客/登录「不做持久化」— 仅靠前端 localStorage |
| EX-5 | medium | BLE001 宽泛 except on detail stream |

### 3.4 detail 流与主对话差异

- `thread_id = explore__{uuid}` — 与主会话隔离 ✅  
- `thinking_enabled=False` — 无 reasoning SSE  
- 无 `follow_up_suggestions`  

---

## 4. 交叉依赖图

```
ToolAssemblyPipeline
    → multi_kb_search_tool → rag_service / retrieve_context
    → html_ppt_tool / excel_* / academic_search

explore_learning
    → retrieve_context / _get_minio_client
    → create_chat_model

LightweightQAAgent
    → _run_kb_search → multi_kb 或 retrieve（需读 1171 行）
```

---

## 5. 拆分建议

| 模块 | 建议 |
|------|------|
| multi_kb | `search/executor.py`, `search/rrf.py`, `search/format.py` |
| tool_assembly | `skill_tool_registry.yaml` + 注册表 |
| explore | `explore/prompts.py` + 分场景 service |
