# 深度审查：`stream_handler.py` 与 SSE 协议一致性

> 文件：`backend/custom/agents/streaming/stream_handler.py`（**987 行**）  
> 规范：[sse_events_module.md](../../docs/docs_backend/sse_events_module.md)

---

## 1. 数据流

```
LangGraph astream_events
    → NEAgentStreamHandler.handle(event)
    → dict: {type: chunk|thinking|status|event|...}
    → NEFullFeatureAgent 循环 yield
    → agent.py / csh_agent.py 转 SSE 字符串
    → 前端 agent-v2.ts 解析
```

轻量模式 **不经过** StreamHandler，直连 `LightweightQAAgent`。

---

## 2. 类与模块函数

| 符号 | 行约 | 职责 |
|------|------|------|
| `_extract_ai_message_from_output` | 34 | 从 on_chat_model_end output 取 AIMessage |
| `_extract_finish_reason` | 63 | finish_reason |
| `_extract_tool_call_names` | 101 | 工具名列表 |
| **`NEAgentStreamHandler`** | 117–987 | 主类 |

### 2.1 实例状态（诊断用字段多）

`answer_fragments`, `chunk_count`, `_tool_started_at`, `_model_generation_round`, `_token_resolver`, `sent_artifacts`, `deep_research_active_task_ids` 等 **20+ 字段** — 状态机复杂，宜文档化状态图。

### 2.2 公开方法

| 方法 | 输出 type | 说明 |
|------|-----------|------|
| `handle` | 多种 | 入口：按 LangGraph `event` 名分发 |
| `finalize` | chunk, event(answer) | 流结束补尾、html-ppt 引导 |
| `log_session_diagnostic` | — | 仅日志 |
| `build_live_markdown_*` | — | 深研实时 markdown 工件 |

### 2.3 内部分发（`handle`）

| LangGraph event | 处理函数 | 典型 SSE 输出 |
|-----------------|----------|----------------|
| summarization + model start/end | 内联 | status + event(tool_start/end) `context_compression` |
| `on_chat_model_stream` | `_on_model_stream` | thinking, chunk, status |
| `on_chat_model_start` | 内联 | 重置 round 计数 |
| `on_chat_model_end` | `_on_model_end` | chunk 补尾、截断 status |
| `on_tool_start` | `_on_tool_start` | status, event(tool_start) |
| `on_tool_end` | `_on_tool_end` | event(tool_end/result/artifact), 偶发 chunk |
| `on_chain_start` | `_on_chain_start` | status（规划/委派） |
| `on_chain_end` | `_on_chain_end` | event |
| `on_custom_event` / `on_chain_stream` | `_on_task_runtime` | event(task_*) |

---

## 3. Handler 产出 vs 协议文档对照

### 3.1 顶层 `type`（API 层转发后）

| type | Handler 是否产出 | agent.py 转发 | 文档 |
|------|------------------|---------------|------|
| `chunk` | ✅ | ✅ 原样 data JSON | ✅ |
| `thinking` | ✅ `_on_model_stream` | ✅ | ✅ |
| `status` | ✅ 字段 **`content`** | ⚠️ 见下 | 文档写 content 为主 |
| `event` | ✅ 嵌套 `event` 对象 | ✅ | ✅ |
| `ping` | ❌（由 full agent 空闲循环发） | 转为 `: ping\n\n` | ✅ comment 心跳 |
| `draft_chunk` | ❌ | **agent.py 草案流** 产生 | ✅ 非 handler |
| `follow_up_suggestions` | ❌ | agent.py 流结束后 | ✅ |
| `error` | ❌ 罕见 | 全局 except | 文档有 |

### 3.2 `status` 字段名不一致（P1）

| 来源 | JSON 形状 |
|------|-----------|
| **stream_handler** | `{"type":"status","content":"正在检索..."}` |
| **agent.py 深研/转发** | `{"type":"status","status": event['content']}` — 键名为 **status** |
| **explore.py** | `{"type":"status","status": ...}` |
| **guest.py** | `{"type":"status","status": event.get('content')}` |
| **TaskEvent.to_sse** | `{"type":"status","status":"..."}` |

文档 §3.3 已说明前端兼容 `content` / `status`。  
**Handler 与 API 层不一致**，建议统一为 `content`（或同时写两个字段）。

### 3.3 嵌套 `event.kind`（Handler 产出）

| kind | 产出位置 | 文档 | 前端 |
|------|----------|------|------|
| `tool_start` / `tool_end` | `_on_tool_*` | ✅ | WorkflowTimeline |
| `tool_result` | tool_end | ✅ | ✅ |
| `task_start` / `task_progress` / `task_end` | `_on_task_runtime` | ✅ | SubagentWorkflowTimeline |
| `artifact` | html_ppt 等 | ✅ | useAgentChat upsert |
| `answer` | finalize | ✅ | 少见 UI |
| `thinking` | 少见 event 形式 | 文档：与顶层 thinking 并存 | 一般走顶层 |
| `error` / `metrics` | 部分路径 | ✅ | 部分 |

**文档有、Handler 无（由 agent.py 发）**：

- `research_draft_created` / `research_confirmed` / `background_task_started`

---

## 4. 与轻量路径差异

| 项目 | StreamHandler 路径 | LightweightQAAgent |
|------|-------------------|----------------------|
| tool event | LangGraph 真实 tool | 模拟 tool_start/end |
| context_compression event | ✅ | ❌ |
| task_* / artifact | ✅ | ❌ |
| max_output_chars 截断 | ✅ | ❌ |
| ping | full agent 循环 | 内置 15s JSON ping |

访客 `guest.py` 对 **chunk 做 think-tag 拆分**，主站 `agent.py` **不拆分 chunk**（仅转发原生 thinking）— 轻量模型若把推理写入 content，主站可能泄漏到正文。

---

## 5. 命名与复用问题

| 问题 | 说明 |
|------|------|
| 类名 `NEAgentStreamHandler` | CSH `csh_full_feature_agent` 同样使用 — **命名误导** |
| `make_process_event` | Handler 用 `stream_utils`，Lightweight 自带副本 — UUID5 相同逻辑 |
| CSH 复用 NE handler | 行为绑定 NE 日志前缀与诊断文案 |

---

## 6. 协议一致性评分

| 维度 | 评分 | 说明 |
|------|------|------|
| chunk/thinking/event | **绿** | 主路径一致 |
| status 字段名 | **黄** | content vs status 混用 |
| ping 形态 | **绿** | comment 行，前端忽略 |
| draft/follow_up | **绿** | 由 API 层补足，文档清晰 |
| 轻量 vs 全功能 | **黄** | event 种类不一致，UI 需兼容空 timeline |
| guest think 拆分 | **黄** | 与主站不一致 |

---

## 7. 整改建议（仅设计）

1. **统一 status**：Handler 与 agent 均输出 `content`（可选冗余 `status` 同值）。  
2. **抽取 `AgentSSEEncoder`**：`dict → SSE bytes`（含 ping comment、[DONE]）。  
3. **Rename** → `AgentStreamHandler`，CSH/NE 共用。  
4. **主站可选**：对 chunk 复用 guest 的 `_split_thinking_segments` 或删除 agent.py 死代码。  
5. **文档**：补充「轻量模式 process event 子集」表。
