# 深度审查：追问建议（Follow-up Suggestions）

> 代码：`custom/services/follow_up_suggestions.py`、`custom/agents/support/follow_up_prompts.py`  
> 调用：`custom/api/v2/agent.py`、`csh_agent.py`、`guest.py`  
> 前端：`agent-v2.ts`、`useAgentChat.ts`、`MessageList.vue`  
> 只读审查。

---

## 1. 数据流

```text
┌─────────────────────────┐ ┌─────────────────────────┐ ┌─────────────────────────┐ ┌─────────────────────────┐ ┌─────────────────────────┐
│      agent.py SSE       │ │FollowUpSuggestionService│ │    create_chat_model    │ │      useAgentChat       │ │      checkpointer       │
└─────────────────────────┘ └─────────────────────────┘ └─────────────────────────┘ └─────────────────────────┘ └─────────────────────────┘

  ┌─ LOOP: process events
  └─ END

```

---

## 2. 后端 `FollowUpSuggestionService`

| 能力 | 实现 |
|------|------|
| 工具摘要 | 从 `tool_end`/`tool_result` process event 收集 title/detail/metadata 列表（最多 12 条） |
| LLM 生成 | `build_follow_up_prompt` + `create_chat_model`（`thinking_enabled=False`，`temperature=0.4`） |
| 解析 | 正则抽 JSON 数组或 `{"suggestions":[...]}` |
| 回退 | 学术/KB/通用三套模板池 |
| 过滤 | `_post_filter`：去重、与答案重复、占位符、长度≤60 |

### 2.1 配置（`settings.py`）

| 字段 | 作用 |
|------|------|
| `follow_up_suggestions_enabled` | 总开关 |
| `follow_up_suggestion_count` | 2–4 条 |
| `follow_up_suggestion_model` | 可选专用模型 |
| `follow_up_suggestion_timeout_ms` | 默认 3000ms |

---

## 3. 触发条件（`agent.py`）

仅在同时满足时生成：

- `follow_up_suggestions_enabled`
- **非** `runtime_deep_research_mode`
- workflow ≠ `AWAITING_CLARIFICATION`
- `accumulated_answer` 非空

**未触发**：深研模式、澄清等待、空回答、开关关闭。

`guest.py` 有独立简化分支（无 tool_summary 完整链路时需单独核对）。

---

## 4. 持久化 `persist_follow_up_to_checkpoint`

- 找最后一条 AI 消息，写入 `additional_kwargs.follow_up_suggestions`
- 通过 **`NEFullFeatureAgent`** + `aupdate_state(Overwrite(messages))` — 与 rollback 相同 **CSH 图不一致风险（FU-1）**

- `asyncio.create_task` 后台执行 — 客户端断开时可能未完成

---

## 5. 全局单例（FU-2）

`get_follow_up_service()` 为进程级单例，`_tool_titles` 在并发多请求间 **共享**。

| 风险 | 说明 |
|------|------|
| **high** | 请求 A 与 B 同时流式时，tool_summary 可能串线 |
| 缓解 | 每请求应 `reset_tool_summary()` — agent 流入口已调用，但 **异常短路** 或 **guest 路径** 需逐条确认 |

---

## 6. 前端对齐

| 项 | 状态 |
|----|------|
| SSE `type: follow_up_suggestions` | ✅ `agent-v2.ts` L332 |
| `onFollowUpSuggestions` 回调 | ✅ `useAgentChat` |
| 历史恢复 | ✅ `metadata.follow_up_suggestions` / camelCase |
| 开关 | `VITE_FOLLOW_UP_SUGGESTIONS !== 'false'`（与后端 settings **独立**） |
| 超时 UI | 12s 取消 loading（后端默认 3s LLM timeout — **不一致但可接受**） |

### 6.1 `mapFollowUpSuggestions`

支持 `string[]` 或 `{id,text}` 对象数组，与 `suggestions_to_metadata_items` 对齐。

---

## 7. 问题清单

| ID | 严重度 | 问题 |
|----|--------|------|
| FU-1 | medium | checkpoint 持久化硬编码 `NEFullFeatureAgent` |
| FU-2 | **high** | 单例 service 并发请求 tool_summary 污染 |
| FU-3 | low | `create_task` 持久化无 await，刷新历史可能短暂看不到建议 |
| FU-4 | low | LLM 失败静默 fallback，metadata.reason 未传到前端 |
| FU-5 | info | 轻量模式若走同一 agent 路由，仍会生成（除非 answer 空） |

---

## 8. 整改建议

1. 每请求实例化 `FollowUpSuggestionService` 或改为 contextvar 存储 `_tool_titles`。  
2. `persist_follow_up` 抽象 `get_agent_for_thread(product)`。  
3. 前端可选展示 `metadata.source`（llm/fallback）便于调试。  
4. 并发压测：双线程同时 query，断言 suggestions 不交叉。

---

## 9. 测试

- 样本见 [fixtures/sse/07_follow_up_suggestions.sse.txt](./fixtures/sse/07_follow_up_suggestions.sse.txt)  
- 契约：`sse_golden_samples.md` §07
