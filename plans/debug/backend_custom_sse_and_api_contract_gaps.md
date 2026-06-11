# V2 Agent SSE / API 契约与实现差距

**对照**：[sse_events_module.md](../../docs/docs_backend/sse_events_module.md)、`backend/custom/api/v2/*`  
**日期**：2026-05-30

---

## 1. 端点实现矩阵

| 端点 | 鉴权 | is_disconnected | 思考标签过滤(chunk) | follow_up | 深研草案/后台 |
|------|------|-----------------|---------------------|-----------|---------------|
| `/api/v2/agent/query` | 是 | ✅ | ❌ 未过滤 | ✅ | ✅ |
| `/api/v2/csh_agent/query` | 是 | ✅ | ❌ | ✅ | ✅ |
| `/api/v2/guest/query` | 否(设备ID) | ❌ | ✅ | ✅(可关 LLM) | ❌ 拒绝 |
| `/api/v2/explore/detail` | 否 | ✅ | 经 Agent 流，同主站 | ❌ | ❌ |
| `/api/v2/explore/topics` 等 | 否 | N/A | N/A | ❌ | ❌ |

**文档缺口**：`sse_events_module.md` L10 写 explore `*/stream`，实际路由为 `POST /explore/detail`（非 `*/stream`）。

---

## 2. 事件类型实现差距

| type | 文档 | 实现差异 |
|------|------|----------|
| `chunk` | 回答正文 | 主站可能含 thinking 标签文本（**契约违反风险**） |
| `thinking` | 推理流 | guest 从 chunk 拆分；主站主要靠 `stream_handler` 原生 reasoning |
| `status` | content 为主 | 兼容 `status` 字段（文档 L74 已写） |
| `draft_chunk` | 深研大纲 | 仅 agent/csh `query` |
| `follow_up_suggestions` | 流结束 | `FOLLOW_UP_SUGGESTIONS_ENABLED`；guest 另受 `follow_up_guest_enabled` |
| `ping` | comment 行 | agent 用 `: ping {ts}`；后台任务 SSE 用 `TaskEvent kind=ping` |

---

## 3. 请求体字段差距

| 字段 | sse_events_module | 代码 `QueryRequest` | 差距 |
|------|-------------------|---------------------|------|
| `thinking_enabled` | L172 | `thinking: bool` | **命名不一致** |
| `attachments` | L173 | `file_ids: list[str]` | **命名不一致** |
| `intent_mode` | 未写 | 存在但仅日志 | 文档应标废弃或实现 |
| `mode` / `task_type` | 部分 | 完整 | OK |

---

## 4. 后台任务 SSE（跨进程）

| 项 | 文档 deep_research_module | 代码 |
|----|---------------------------|------|
| 主 SSE 断开后订阅 | 应有 `task/{id}/stream` | ✅ agent/csh |
| 跨进程 | 文档应说明 thread_id 必填 | ✅ `thread_id` query 400 |
| 事件上限 | 未写 | `MAX_EVENTS=10000` / metadata 膨胀（P1） |

---

## 5. 建议文档补丁（不改代码）

1. `sse_events_module.md` §1 端点表：修正 explore 路径；增加 csh_agent 行（若与 agent 协议相同可写「同 agent」）。  
2. 新增 §「思考内容」：列出 guest vs agent/csh 差异，或写「修复后统一」。  
3. §4 请求参数：`thinking` / `file_ids` 与 Swagger 对齐。  
4. 探索式学习：注明 topics/map/expand **无登录** 与成本风险（链到认证/限流方案）。

---

## 6. 相关审查

- [backend_custom_p0_fix_checklist.md](./backend_custom_p0_fix_checklist.md) — P0-3  
- [backend_custom_vs_architecture_redesign.md](./backend_custom_vs_architecture_redesign.md) — §7
