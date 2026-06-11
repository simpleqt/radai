# SSE 黄金样本与契约测试清单（建议）

> 只读规格，供后续 `pytest` / Vitest 录制回放。不修改代码。

---

## 1. 目的

- 锁定 [sse_events_module.md](../../docs/docs_backend/sse_events_module.md) 与实现对齐  
- 防止 `agent.py` / `csh_agent.py` / `guest` / `task` / `explore` 漂移  
- 覆盖 [audit_frontend_backend_sse.md](./audit_frontend_backend_sse.md) 中 FE-1~FE-7

---

## 2. 样本文件命名

```
plans/debug/fixtures/sse/
  01_normal_chunk_only.sse.txt
  02_thinking_and_chunk.sse.txt
  03_status_content_vs_status.sse.txt
  04_process_event_tool.sse.txt
  05_draft_chunk_and_research_draft_created.sse.txt
  06_background_task_started.sse.txt
  07_follow_up_suggestions.sse.txt
  08_ping_comment.sse.txt
  09_error_and_done.sse.txt
  10_task_stream_status_key.sse.txt
  11_explore_detail_minimal.sse.txt
```

---

## 3. 各样本最小内容

### 01 普通 chunk

```
data: {"type": "chunk", "content": "你好"}

data: [DONE]

```

**断言**：`queryStream` onChunk 收到「你好」；`doneReceived=true`。

### 02 thinking + chunk

```
data: {"type": "thinking", "content": "推理片段"}

data: {"type": "chunk", "content": "答案"}

data: [DONE]

```

**断言**：`onThinking` + `onChunk` 均被调用。

### 03 status 字段双形态

```
data: {"type": "status", "content": "检索中"}

data: {"type": "status", "status": "草案已生成"}

data: [DONE]

```

**断言**：`onStatus` 两次非空。

### 04 process event

```
data: {"type": "event", "event": {"id": "e1", "kind": "tool_start", "title": "知识库检索", "detail": "q=test", "tool": "multi_kb_search", "timestamp": 1710000000000}}

data: [DONE]

```

**断言**：`onEvent.kind === 'tool_start'`；`useAgentChat` 入 processEvents。

### 05 草案流

```
data: {"type": "draft_chunk", "content": "## 维度1\n"}

data: {"type": "event", "event": {"kind": "research_draft_created", "metadata": {"draft_id": "d1", "plan_content": {"outline_text": "## 维度1\n"}}}}

data: [DONE]

```

**断言**：`onDraftChunk`；`research_draft_streaming` 或 `research_draft_created` UI。

### 06 后台任务

```
data: {"type": "event", "event": {"kind": "background_task_started", "metadata": {"task_id": "t-123"}}}

data: [DONE]

```

**断言**：`result.backgroundTaskId === 't-123'`。

### 07 追问建议

```
data: {"type": "follow_up_suggestions", "suggestions": ["问题A", "问题B"], "metadata": {"source": "llm"}}

data: [DONE]

```

**断言**：`mapFollowUpSuggestions` 长度 2。

### 08 ping（comment）

```
: ping 1710000000000

data: {"type": "chunk", "content": "x"}

data: [DONE]

```

**断言**：解析器忽略 comment；chunk 正常。

### 09 错误

```
data: {"error": "内部错误"}

data: [DONE]

```

**断言**：`onStatus` 含 Error 或控制台 error。

### 10 任务流 status 键

```
data: {"type": "status", "status": "深度研究已启动"}

data: {"type": "chunk", "content": "段落"}

data: [DONE]

```

**断言**：`subscribeTaskStream` onStatus 收到文案。

### 11 explore detail

```
data: {"type": "status", "status": "轻量问答..."}

data: {"type": "chunk", "content": "详解正文"}

data: [DONE]

```

**断言**：探索页 SSE 客户端解析（若独立客户端需单测）。

---

## 4. 后端集成录制命令（人工）

```bash
# 需有效 token 与运行中 API
curl -N -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"测试","thread_id":"test-thread","thinking":false}' \
  https://host/api/v2/agent/query 2>/dev/null | head -50 > plans/debug/fixtures/sse/01_recorded.sse.txt
```

---

## 5. 前端 Vitest 骨架（建议）

```typescript
// parseAgentSSELines(buffer: string): ParsedEvent[]
// 对每 fixture 断言回调调用次数与 payload
```

共享实现：`parseAgentSSELine` 从 `agent-v2.ts` 抽出后测试。

---

## 6. 回归关注 FE-1

录制 **主站** 与 **guest** 各一份 chunk 样本：

- 若主站 chunk 含 `...` 而 guest 已拆分 → 记录为已知差异直至修复。
