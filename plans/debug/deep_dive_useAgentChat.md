# 深度审查：`useAgentChat.ts` 状态机与恢复逻辑

> 文件：`frontend/src/views/ne-full/composables/useAgentChat.ts`（**~1209 行**）  
> CSH 版差异极小（见 §8）。只读审查。

---

## 1. 职责总览

| 职责 | 实现位置 |
|------|----------|
| 会话消息列表 `messages` | ref + reactive assistant |
| Agent 配置 `agentInfo` / 模型 / KB / skills | `loadAgentInfo` |
| 历史加载 | `loadHistory` → ConversationAPI / guest IndexedDB |
| 发送消息 + SSE 消费 | `sendMessage` → `AgentAPI.queryStream` |
| 深研草案 UI 状态 | `processEvents` + `draft_chunk` 合成 |
| 后台深研任务 | `subscribeToBackgroundTask` |
| 流中断恢复 | sessionStorage + `checkAndResumeInterruptedStream` |
| 产物 artifacts | `upsertArtifact` + ConversationAPI |

---

## 2. 模块级工具函数（L1–312）

| 符号 | 职责 | 质量 |
|------|------|------|
| `hashSuggestionId` | 追问建议稳定 id | ok |
| `mapFollowUpSuggestions` | string/object → FollowUpSuggestion | 比 agent-v2 更完整 |
| `hidePreviousAssistantSuggestions` | 新轮隐藏上轮建议 | ok |
| `looksLikeIntermediateContext` | 识别历史污染正文 | 启发式，可能误判 |
| `extractWorkflowStatusFromIntermediate` | 从污染正文猜 status | 仅恢复路径 |
| `read/write/clearPendingStreamState` | sessionStorage 10min TTL | 仅登录用户 |
| `normalizeSelectedSkills` | 过滤默认 skill | NE 滤 `nuclear-electronics` |
| `normalizeHistoryMessages` 等 | 历史格式归一 | 需与后端 role 对齐 |

---

## 3. `sendMessage` 主流程（L741–1136）

```text
┌──────────────────┐
│ PushUser         │
└──────────────────┘
    ▲  appendUserMessage
    │
    ▼

    │
    ▼
┌───────────────────────┐
│ FindOrCreateAssistant │
└───────────────────────┘

  (AgentAPI.queryStream)
    │
    ▼
┌──────────────────┐
│ QueryStream      │
└──────────────────┘

  (backgroundTaskId)
    │
    ▼
┌──────────────────┐
│ Background       │
└──────────────────┘

  (result.aborted)
    │
    ▼
┌──────────────────┐
│ Aborted          │
└──────────────────┘

  (result.interrupted)
    │
    ▼
┌──────────────────┐
│ Interrupted      │
└──────────────────┘

  (正常结束)
    │
    ▼
┌──────────────────┐
│ Done             │
└──────────────────┘

  (subscribeToBackgroundTask)
    │
    ▼
┌──────────────────┐
│ SubscribeTask    │
└──────────────────┘

  (recoverAssistantContentFromHistory)
    │
    ▼
┌──────────────────┐
│ RecoverHistory   │
└──────────────────┘

  (suggestionsLoading)
    │
    ▼
┌──────────────────┐
│ FollowUp         │
└──────────────────┘


```

### 3.1 草案动作复用消息（L760–787）

`confirmationStatus ∈ {revised, confirmed, cancelled}` 时：

- 从后往前找含 `research_draft_created/updated` 且 `metadata.draft_id` 匹配的 assistant 消息
- **清空** content/reasoning/processEvents 后复用同一消息 — 符合产品「同卡片编辑草案」

**风险**：若 draft_id 不匹配或历史被 rollback，会新建消息，可能出现双卡片。

### 3.2 SSE 批处理（L805–873）

| 缓冲 | 刷新策略 |
|------|----------|
| `pendingChunk` / `pendingThinking` / `pendingStatus` / `pendingEvents` | `flushImmediately` 每条 SSE 即刷 |
| `pendingDraftChunk` | 累积到 `research_draft_streaming` 伪 event |

**设计**：去掉打字机延迟，降低 UI 卡顿 — 与注释一致。

### 3.3 深研转后台（L1014–1043）

1. `background_task_started` → `result.backgroundTaskId`（在 agent-v2 解析）
2. **立即** `subscribeToBackgroundTask`（避免丢事件）✅
3. 异步 `getTaskStatus` 填 `activeBackgroundTask`

**缺口**：订阅时 `onFollowUpSuggestions` 未传入 subscribeTaskStream — 后台流结束无追问建议（可能符合产品）。

---

## 4. 后台任务恢复（L422–603）

### 4.1 `subscribeToBackgroundTask`

| 回调 | 行为 |
|------|------|
| onChunk | 直接 append content（无 pending 批处理） |
| onStatus | statusHistory + 全局 status |
| onEvent | 去重 id；artifact_update 旁路 |
| onDone | recoverAssistantContentFromHistory |
| onError | 5s 后 auto-recovery |

### 4.2 自动恢复逻辑（L495–538）

1. `getThreadActiveTask` 仍 active → 重新 subscribe  
2. 已结束 → recoverHistory + 替换警告文案  

**质量**：健壮；依赖服务端 task 状态与 DB 一致。

### 4.3 `checkAndResumeBackgroundTask`（L584–603）

切换会话时调用：若有 active task，对**最后一条** assistant 订阅。

**风险**：若最后一条不是深研那条 assistant，会订错消息。

---

## 5. 页面刷新恢复（L605–652）

| 步骤 | 说明 |
|------|------|
| `persistPendingStreamState` | send 开始时写入 sessionStorage |
| `checkAndResumeInterruptedStream` | loadHistory 后调用 |
| 校验 | 最后 user 消息与 pending.question 一致 |
| 上限 | `MAX_PENDING_RESUME_ATTEMPTS = 2` |
| 动作 | `rollbackConversation(keepUserTurns-1)` + **重发** `sendMessage` |

**问题**：

- 重发会再次打后端，可能重复检索/扣费  
- guest 模式不持久化 pending — 符合预期  
- 与 `result.interrupted` 历史恢复（L1062–1075）两条路径可能重复尝试

---

## 6. 中断与取消

| API | 行为 |
|-----|------|
| `cancelStream` | abort `streamAbortController` |
| `cancelBackgroundTask` | POST cancel + unsubscribe |

`sendMessage` 对 `AbortError` 与 `result.aborted` 分别处理 — 一致。

---

## 7. 硬编码与配置

| 项 | 值 | 风险 |
|----|-----|------|
| `selectedKbs` 默认 | 两个 NE KB UUID | 应与 agentInfo 同步；CSH 改为 `[]` |
| `PENDING_STREAM_TTL_MS` | 10 分钟 | 文档化 |
| `FOLLOW_UP_SUGGESTIONS_ENABLED` | env 默认 true | ok |
| `normalizeSelectedSkills` | 过滤 nuclear-electronics | CSH 另滤 csh-ai |

---

## 8. RadAI vs CSH `useAgentChat` diff

| 项 | RadAI | CSH-AI |
|----|-------|--------|
| 默认 KB | 两个固定 UUID | `[]`（靠 loadAgentInfo 填充） |
| endpoint | `/api/v2/agent/query` | `/api/v2/csh_agent/query` |
| skill 过滤 | 去掉 nuclear-electronics | 另去掉 csh-ai |

其余 **1200+ 行相同** — 建议抽 `@/composables/useAgentChatCore(basePath, defaultKbs, skillFilter)`。

---

## 9. 问题清单

| ID | 严重度 | 描述 |
|----|--------|------|
| UAC-1 | medium | `checkAndResumeBackgroundTask` 假定最后一条 assistant 为深研消息 |
| UAC-2 | low | pending 恢复与 interrupted 历史恢复可能双轨重复 |
| UAC-3 | low | 后台 subscribe 无 pending 批处理，与主流不一致 |
| UAC-4 | info | `looksLikeIntermediateContext` 硬编码英文特征串 |
| UAC-5 | low | RadAI 默认 KB 硬编码，与后端 binding 可能漂移 |

---

## 10. 建议测试场景（黄金路径）

1. 普通问答 → chunk + follow_up  
2. 深研：draft_chunk → research_draft_created → confirm → background_task_started → task SSE  
3. 刷新页面：pending 自动重发（≤2 次）  
4. 切换会话：后台任务恢复订阅  
5. cancelStream / cancelBackgroundTask  
6. guest 模式：无 pending、无后台任务  
