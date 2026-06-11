# 前后端 SSE 对齐审计：`agent-v2.ts` ↔ Backend

> 对照文件：  
> - `frontend/src/api/agent-v2.ts`（RadAI）  
> - `frontend_csh_ai/src/api/agent-v2.ts`（CSH，**仅 URL 前缀不同**）  
> - `frontend/src/views/ne-full/composables/useAgentChat.ts`  
> - [sse_events_module.md](../../docs/docs_backend/sse_events_module.md)

---

## 1. 前端双端差异

`diff frontend/src/api/agent-v2.ts frontend_csh_ai/src/api/agent-v2.ts`：

| 项 | RadAI | CSH-AI |
|----|-------|--------|
| `/info` | `/api/v2/agent/info` | `/api/v2/csh_agent/info` |
| 默认 query | `/api/v2/agent/query` | `/api/v2/csh_agent/query` |
| task/status/cancel/stream | `/api/v2/agent/task/...` | `/api/v2/csh_agent/task/...` |

**SSE 解析逻辑 100% 相同** — 可抽 `packages/shared/agent-v2-sse.ts` 避免漂移。

---

## 2. 事件类型矩阵

| type | 后端产出 | queryStream 解析 | useAgentChat 消费 | 一致 |
|------|----------|------------------|-------------------|------|
| `chunk` | agent/csh/guest/runner | `onChunk(content)` | `assistantMsg.content +=` | ✅ |
| `thinking` | handler/lightweight/guest | `onThinking` | `assistantMsg.reasoning +=` | ✅ |
| `status` | 见下 | `onStatus(content\|status)` | `statusHistory` | ✅ |
| `event` | handler + agent 深研 | `onEvent(event)` | `processEvents` + artifact | ✅ |
| `draft_chunk` | agent.py 草案流 | `onDraftChunk` | `research_draft_streaming` 合成 | ✅ |
| `follow_up_suggestions` | agent/guest 流末 | `onFollowUpSuggestions` | `mapFollowUpSuggestions` | ⚠️ 见 §4 |
| `ping` (comment) | agent 转 `: ping` | **不解析**（非 data 行） | — | ✅ |
| `ping` (JSON) | lightweight 内部 | **不解析**（若未转 comment） | — | ⚠️ 轻量直通路 rare |
| `[DONE]` | agent/guest/task | 设 `doneReceived` | 结束流 | ✅ |
| `error` 顶层 | guest except | `onStatus('Error:...')` | status 条 | ⚠️ 非 type:error |
| `type: error` | 文档/RAG | queryStream 走 `data.error` | 同上 | 部分路径 |

---

## 3. `AgentAPI.queryStream` 行为详解

### 3.1 请求体 vs 后端 `QueryRequest`

| 前端 payload | 后端字段 | 对齐 |
|--------------|----------|------|
| `question` | `question` | ✅ |
| `thread_id` | `thread_id` | ✅ |
| `task_type` / `mode` deep_research | `subagent_enabled`, `task_type`, `mode` | ✅ |
| `thinking` | `thinking`（后端 Field 同名） | ✅ 默认 true |
| `kb_ids` | `kb_ids` | ✅ |
| `use_search` | `use_search` | ✅ |
| `selected_skills` | `selected_skills` | ✅ |
| `file_ids` | `file_ids` | ✅ |
| guest: `mode=lightweight` | router lightweight | ✅ |
| guest: `thinking=false` | 关闭推理 | ✅ |

**未传但后端支持**：`intent_mode`, `confirmation_status`, `draft_id`, `research_plan` — 由 `useAgentChat` 在深研流程传入 ✅

### 3.2 解析顺序（`agent-v2.ts` L296–355）

1. `thinking` → 2. `status`（含 `data.status` 无 type）→ 3. `event` → 4. `chunk` → 5. `draft_chunk` → 6. `follow_up_suggestions` → 7. fallback 纯 content → 8. `data.error`

**风险**：fallback 纯 content 可能误吞畸形 JSON。

### 3.3 中断与完成

| 场景 | 行为 |
|------|------|
| `AbortSignal` | `userAborted=true`，不抛错 |
| 无 `[DONE]` 断开 | `interrupted: true` |
| `background_task_started` | 从 `event.metadata.task_id` 取 `backgroundTaskId` |

---

## 4. 追问建议 `follow_up_suggestions`

| 层 | 格式 |
|----|------|
| 后端 `follow_up_suggestions.py` | `list[str]` |
| SSE | `{type, suggestions: string[], metadata}` |
| `agent-v2.ts` | `String(item)` 映射 — **对象数组会变成 `[object Object]`** |
| `useAgentChat.mapFollowUpSuggestions` | 支持 `string \| {id,text}` |

**结论**：当前后端只发字符串，**无 bug**；若未来发 `{id,text}[]`，须改 `agent-v2.ts` 与 `subscribeTaskStream` 同步。

---

## 5. `subscribeTaskStream`（后台深研）差异

| type | queryStream | subscribeTaskStream |
|------|-------------|---------------------|
| `chunk` | ✅ | ✅ |
| `thinking` | ✅ | ✅ |
| `status` | content \|\| status | status \|\| content |
| `event` | ✅ | ✅ 无 background_task 提取 |
| `draft_chunk` | ✅ | ❌ **缺失** |
| `follow_up_suggestions` | ✅ | ✅ |
| `error` | data.error | type===error |
| 重连 | 无 | MAX_RETRIES=3 |

**gap**：任务流若未来推送 `draft_chunk`（当前 runner 无），前端不处理。

---

## 6. `useAgentChat.ts` 与 process `event.kind`

### 6.1 显式处理

| kind | 处理 |
|------|------|
| `artifact_update` | `upsertArtifact`，不进入 processEvents |
| `artifact` | upsert + 入 processEvents |
| `research_draft_created` / `updated` | MessageList / ResearchDraftCard |
| `research_draft_streaming` | **前端合成**（由 `draft_chunk` 累积） |
| `background_task_started` | 在 agent-v2 取 task_id；UI 切后台订阅 |

### 6.2 TypeScript 类型缺口

`AgentProcessEvent.kind` 联合类型 **未包含**：

- `task_progress`（运行时有，靠 `string` 兜底）
- `artifact_update`
- `research_draft_*`
- `background_task_started`

**建议**：扩展联合类型或 `kind: string` 文档化完整枚举（与 sse_events_module 同步）。

### 6.3 UI 组件映射

| 组件 | 依赖 |
|------|------|
| `WorkflowTimeline` | tool_* , task_* |
| `SubagentWorkflowTimeline` | task_progress |
| `ResearchDraftCard` | research_draft_* |
| `ReasoningBlock` | `message.reasoning` ← thinking SSE |

---

## 7. 不一致 / Bug 候选清单

| ID | 严重度 | 描述 |
|----|--------|------|
| FE-1 | **medium** | 主站 `agent.py` 未对 chunk 做 think-tag 拆分，guest 有 — 模型泄漏推理进正文 |
| FE-2 | **low** | `agent.py` 定义 `_split_thinking_segments` 未使用 — 死代码 |
| FE-3 | **low** | `queryStream` 与 `subscribeTaskStream` 解析分支重复，易漂移 |
| FE-4 | **info** | `follow_up` 仅 string；对象格式需同步两处解析 |
| FE-5 | **low** | 深研 `status` JSON 用 `status` 键，handler 用 `content` — 前端已兼容 |
| FE-6 | **low** | Task SSE `ping` 为 `{"type":"ping",...}`，主站为 comment — 前端均忽略，OK |
| FE-7 | **info** | `interrupted` 无 UI 区分 user abort vs 网络断开（仅有 console） |

---

## 8. 建议行动

| 优先级 | 行动 |
|--------|------|
| P1 | 共享 `parseAgentSSELine(line)` 供 queryStream + subscribeTaskStream |
| P1 | 决定主站是否在 API 层统一 think-tag 拆分（或删死代码） |
| P2 | 扩展 `AgentProcessEvent` kind 枚举与文档一致 |
| P2 | RadAI/CSH 共用单文件 agent-v2，仅 `basePath` 配置 |
| P3 | 黄金快照测试：录制 SSE 样本 → 解析 → 快照 |

---

## 9. 端到端序列（正常问答）

```text
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│   useAgentChat   │ │   agent-v2.ts    │ │     agent.py     │ │NEFullFeatureAgent│ │  StreamHandler   │
└──────────────────┘ └──────────────────┘ └──────────────────┘ └──────────────────┘ └──────────────────┘


```

轻量模式：AG 换为 LightweightQAAgent，跳过 H，event 种类减少。
