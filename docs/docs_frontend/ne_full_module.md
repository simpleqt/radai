# 专家模式模块 (NE-Full Module)

> **最后同步**：2026-07-14（对照 `frontend/src/views/ne-full/**`、`frontend/src/api/agent-v2.ts`、`frontend/src/api/conversations.ts`）
> **调试**：后端 Trace 接口 `GET /api/v2/agent-runtime/runs/{run_id}` 系列需登录；前端专家模式当前未内置 Trace 面板。

专家模式模块位于 **`/app/:threadId?`（桌面）** 与 **`/m/:threadId?`（移动）** 路由，旧路径 `/chat/:threadId?`、`/ne-full/:threadId?` 会按当前 UI 模式重定向到 `/app` 或 `/m`。

该模块是当前主对话界面：支持登录用户会话历史、访客 IndexedDB 历史、V2 Agent SSE、思考流、工作流/子智能体时间线、工件预览、追问建议、对话附件、个人文件库选择、语音输入、停止生成、编辑后重发和回答导出。

---

## 文件结构

```text
views/ne-full/
├── index.vue                         # 页面入口：NavRail + HistorySidebar + MessageList + ChatBox + ArtifactSidebar
├── MyFilesView.vue                   # 个人文件库
├── composables/
│   ├── useAgentChat.ts               # V2/Guest 对话状态、SSE、历史恢复、工件聚合
│   ├── useFileUpload.ts              # 对话附件上传与个人文件库附件状态
│   └── useVoiceInput.ts              # 按住说话 + ASR 转写
└── components/
    ├── ChatBox.vue                   # 输入框，内置模型/技能/KB/搜索/附件/语音控制
    ├── MessageList.vue               # 消息、空态建议、复制、编辑重发、导出、追问建议
    ├── MarkdownRenderer.vue          # Markdown + KaTeX + DOMPurify
    ├── ReasoningBlock.vue            # 思考流折叠面板
    ├── WorkflowTimeline.vue          # 主工作流时间线；聚合状态和 processEvents
    ├── SubagentWorkflowSwitcher.vue  # 子智能体分组切换
    ├── SubagentWorkflowTimeline.vue  # 子智能体详细步骤
    ├── ArtifactSidebar.vue           # 工件预览/下载
    ├── HistorySidebar.vue            # 登录/访客会话历史
    ├── FollowUpSuggestions.vue       # 回答后追问
    ├── FilePreviewChip.vue           # 附件预览芯片
    ├── MyFilesPickerDialog.vue       # 从个人文件库添加附件
    ├── ResearchDraftCard.vue         # 旧深研草案确认/修订兼容
    ├── KBSelector.vue                # 遗留/备用 KB 选择器，当前 ChatBox 内置 KB 菜单
    ├── ToolBadge.vue                 # 遗留/备用工具徽章，当前由 WorkflowTimeline 承接
    └── ProcessEventsPanel.vue        # 遗留/调试过程事件面板，当前页面未直接挂载
```

---

## 页面入口 (`index.vue`)

`index.vue` 负责组装完整对话工作台：

```text
ne-shell
├── Header：会话抽屉按钮、品牌标题、专家/访客标签、工件侧栏开关
├── mobile HistorySidebar drawer
├── Main
│   ├── NavRail
│   ├── HistorySidebar（>=1360px 桌面常驻）
│   ├── MessageList
│   ├── ChatBox（底部固定；桌面为主区底部）
│   └── ArtifactSidebar
└── MyFilesPickerDialog
```

关键状态来自 `useAgentChat()` 与 `useFileUpload()`：

| 状态/动作 | 说明 |
| :--- | :--- |
| `messages`、`isBusy`、`status` | 当前消息、生成状态和状态文案 |
| `agentInfo`、`selectedModel` | `/api/v2/agent-runtime/profiles/{agent_code}/info` 返回的模型/技能/KB 配置 |
| `selectedKbs`、`searchEnabled`、`selectedSkills`、`researchMode` | ChatBox 内置控制项；`deep-research` 技能会映射为深度研究模式 |
| `artifacts`、`showArtifacts`、`artifactResetKey` | 工件列表、侧栏显隐和新一轮会话工件选中状态重置 |
| `attachments`、`uploadParseMode` | 对话附件队列，解析模式为 `auto` 或 `mineru` |
| `localThreadId` | 新会话首次发送前由前端生成 UUID，随后进入 `/app/{threadId}` 或 `/m/{threadId}` |

发送前会先上传 pending 附件；从个人文件库选择的文件会通过 `POST /api/v1/sessions/{threadId}/files/attach` 绑定到会话。随后 `sendMessage()` 将 ready 文件 ID 放入 Agent 请求 metadata。

---

## 对话状态 (`useAgentChat.ts`)

### 导出接口

```typescript
export function useAgentChat() {
  return {
    messages,
    isBusy,
    status,
    agentInfo,
    selectedModel,
    selectedKbs,
    searchEnabled,
    selectedSkills,
    researchMode,
    setResearchMode,
    toggleResearchMode,
    artifacts,
    isGuestMode,
    activeBackgroundTask,
    backgroundTaskStreaming,
    loadAgentInfo,
    loadHistory,
    sendMessage,
    clearMessages,
    rollbackConversation,
    cancelStream,
    cancelBackgroundTask,
    checkAndResumeBackgroundTask,
    checkAndResumeInterruptedStream,
    getPendingInterruptedThreadId,
  }
}
```

### ChatMessage 类型

```typescript
interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  reasoning?: string;
  statusHistory?: string[];
  processEvents?: AgentProcessEvent[];
  isStreaming?: boolean;
  followUpSuggestions?: FollowUpSuggestion[];
  suggestionsLoading?: boolean;
  suggestionsHidden?: boolean;
}
```

### AgentInfo 类型

```typescript
interface AgentInfo {
  name: string;
  model: string | null;
  default_model?: string | null;
  available_models?: ModelOption[];
  kb_ids: string[];
  available_kbs?: KbOption[];
  subagent_enabled: boolean;
  search_enabled?: boolean;
  max_concurrent_subagents?: number;
  selected_skills?: string[] | null;
  required_skills?: string[];
  available_skills?: SkillOption[];
  features: string[];
}
```

知识库名称由后端动态返回。前端只传 `kb_ids`，不直接调用 RAG 检索接口；检索由 Agent 工具 `multi_kb_search` 在后端完成。

### 登录用户与访客模式

| 模式 | 数据来源 | 请求端点 | 限制 |
| :--- | :--- | :--- | :--- |
| 登录用户 | `/api/v2/conversations`、`/api/v2/files` | `/api/v2/agent-runtime/query` | 可用技能、深度研究、会话工件和个人文件库 |
| 访客 | `services/storage/guestIndexedDb.ts` | `/api/v2/guest/query` | 固定轻量问答：`selected_skills=[]`、`thinking=false`、`mode=lightweight` |

`loadHistory(threadId)` 会先清空旧 UI 状态，再加载消息与工件；加载完成后会尝试恢复后台任务或页面刷新导致的未完成流。未完成流状态保存在 `sessionStorage.agent_v2_pending_stream`，TTL 为 10 分钟，最多自动续接 2 次。

---

## Agent API (`api/agent-v2.ts`)

### `AgentAPI.getInfo()`

```http
GET /api/v2/agent-runtime/profiles/{agent_code}/info
Authorization: Bearer <token>   # 可选
```

`agent_code` 默认来自 `VITE_AGENT_CODE`，RadAI 默认为 `ne`，CSH-AI 默认为 `csh`。

### `AgentAPI.queryStream()`

```typescript
queryStream(
  question: string,
  onChunk: (text: string, mode?: 'append' | 'replace') => void,
  onStatus: (status: string) => void,
  options?: {
    threadId?: string;
    researchMode?: 'normal' | 'deep_research';
    thinking?: boolean;
    kbIds?: string[];
    useSearch?: boolean;
    intentMode?: 'auto' | 'manual';
    draftId?: string;
    confirmationStatus?: 'pending' | 'confirmed' | 'revised' | 'cancelled';
    researchPlan?: ResearchPlanPayload;
    model?: string;
    token?: string;
    selectedSkills?: string[];
    endpoint?: '/api/v2/agent-runtime/query' | '/api/v2/guest/query';
    deviceId?: string;
    signal?: AbortSignal;
    fileIds?: string[];
    agentCode?: string;
  },
  onEvent?: (event: AgentProcessEvent) => void,
  onThinking?: (text: string) => void,
  onDraftChunk?: (text: string) => void,
  onFollowUpSuggestions?: (suggestions: string[], metadata?: Record<string, unknown>) => void,
): Promise<{ backgroundTaskId?: string; interrupted?: boolean; aborted?: boolean }>
```

请求体会按选项动态组装：

```json
{
  "agent_code": "ne",
  "question": "...",
  "thread_id": "...",
  "subagent_enabled": false,
  "workflow": "full",
  "thinking": true,
  "kb_ids": ["..."],
  "use_search": false,
  "model": "...",
  "selected_skills": ["academic-search"],
  "metadata": {
    "file_ids": ["..."],
    "intent_mode": "auto"
  }
}
```

深度研究时前端设置 `subagent_enabled=true`、`workflow='deep_research'`；普通问题若选择了技能或开启联网搜索，会设置 `workflow='full'`。访客端点会强制关闭技能、思考链和深度研究。

### SSE 事件处理

`queryStream()` 支持标准 envelope（`{ type, payload }`）和旧格式。核心事件如下：

| type | 前端处理 |
| :--- | :--- |
| `message.delta` / `chunk` | 追加回答正文 |
| `message.replace` | 替换回答正文 |
| `reasoning.delta` / `thinking` | 追加到 `reasoning`，由 `ReasoningBlock` 渲染 |
| `workflow.started` | 更新全局状态文案 |
| `workflow.step` + `payload.event` | 追加到 `processEvents`，由 `WorkflowTimeline` 渲染 |
| `workflow.step` / `status` | 追加到 `statusHistory` 并更新 `status` |
| `plan.created` / `plan.updated` | 转换为流程事件，展示研究流程/并行计划 |
| `coordination.unit.completed` / `coordination.unit.failed` | 转换为子智能体分支完成/失败事件 |
| `coordination.partial_handoff` | 转换为阶段性结论移交事件 |
| `coordination.completed` | 转换为深度研究完成事件 |
| `event` / `payload.event` | 兼容旧过程事件 |
| `artifact` / `artifact_update`（event.kind） | 更新 `artifacts`，自动打开可预览工件 |
| `research.draft.delta` / `draft_chunk` | 写入 `research_draft_streaming` 事件，供草案卡片兼容展示 |
| `follow_up.created` / `follow_up_suggestions` | 填充 `followUpSuggestions` |
| `done` / `[DONE]` | 标记流完成 |

`deep_research` 和 `plan_solve` 的 `POST /api/v2/agent-runtime/query` 会返回后台任务启动确认；前端随后通过 `GET /api/v2/agent-runtime/{agent_code}/task/{task_id}/stream` 订阅进度与最终答案，并用 `status`/`cancel` 查询或取消。`lightweight`、`full` 与 `multi_agent_*` 才是在同一条 `/query` SSE 中消费 `plan.*`、`workflow.step`、`coordination.*` 与工件事件。

---

## 消息发送流程

```text
ChatBox submit
  ├── index.vue 创建/确定 threadId
  ├── useFileUpload.uploadPendingFiles(threadId)
  │   └── POST /api/v2/chat-files/upload (FormData: thread_id, parse_mode, files[])
  ├── 个人文件库附件绑定
  │   └── POST /api/v1/sessions/{threadId}/files/attach
  └── useAgentChat.sendMessage(question, options)
      ├── 追加用户消息；创建/复用 assistant reactive 消息
      ├── sessionStorage 记录 pending stream（登录用户）
      ├── AgentAPI.queryStream(..., { signal, fileIds, kbIds, selectedSkills, model })
      ├── 即时刷入 chunk/status/thinking/event/draft/follow_up
      ├── 连接中断时尝试从 /api/v2/conversations/{threadId}/messages 恢复
      └── 完成后清理 busy/status/streamAbortController，访客消息落 IndexedDB
```

停止生成通过 `AbortController` 中止当前 fetch stream；若存在可重连后台任务，则调用 `cancelBackgroundTask()` 取消 task registry 中的任务。

---

## 关键组件

### ChatBox

`ChatBox.vue` 不只是输入框，还承载当前对话的主要控制：

| 控制 | 实现 |
| :--- | :--- |
| 文本输入 | `textarea` 自适应高度；`Enter` 发送，`Shift+Enter` 换行 |
| 模型选择 | 使用 `agentInfo.available_models`，并根据 `supports_vision` 给图片附件提示 |
| 技能选择 | 使用 `agentInfo.available_skills`，无数据时回退到深度研究、PPT、学术搜索、实验数据分析等 fallback |
| KB 选择 | 使用 `agentInfo.available_kbs` 或 `agentInfo.kb_ids`，通过 `update:selected-kbs` 传出 |
| 联网搜索 | `searchEnabled` 开关，发送时映射为 `use_search` |
| 附件 | 支持本地上传、从我的文件添加、拖拽、粘贴图片；解析模式 `auto/mineru` |
| 语音 | `useVoiceInput()` 状态机：`idle → recording → transcribing → editing` |

主要 emits：`send`、`abort`、`update:search-enabled`、`update:selected-skills`、`update:selected-kbs`、`update:selected-model`、`files-selected`、`open-library-picker`、`update:upload-parse-mode`。

### MessageList

`MessageList.vue` 渲染消息和回答操作：

| 功能 | 当前实现 |
| :--- | :--- |
| 空态建议 | 内置核电子学问题胶囊，按视口数量裁剪 |
| 用户消息 | 复制、编辑后重发；重发前通过 `rollbackConversation()` 回滚后续轮次 |
| 助手消息 | `ResearchDraftCard`、`WorkflowTimeline`、`ReasoningBlock`、`MarkdownRenderer` |
| 导出 | PDF 使用隐藏 iframe 触发浏览器打印；Word 调用 `POST /api/v2/files/export-docx` |
| 追问建议 | `FollowUpSuggestions`；流式中、草案待确认或澄清 pending 时不展示 |

### WorkflowTimeline 与子智能体时间线

`WorkflowTimeline.vue` 是当前过程展示主入口，直接消费 `statusHistory` 与 `processEvents`。它会按事件 metadata 中的任务/角色信息进行分组，并使用：

- `SubagentWorkflowSwitcher.vue`：展示各子智能体分组、状态和事件数。
- `SubagentWorkflowTimeline.vue`：展示单个分支的工具、检索、文件、联网、artifact 等步骤。

因此文档或开发中不应再把 `ToolBadge` 当成当前主流程展示组件；它保留在目录中但页面未直接挂载。

### ArtifactSidebar

`ArtifactSidebar.vue` 接收 `artifacts` 并支持：

- 多工件列表选择，默认选择最新工件。
- 根据扩展名/MIME 推断 `markdown`、`html`、`code`、`text`、`file`。
- 对缺少 `content` 的可预览工件，使用带 Bearer 的 `GET url` 拉取文本内容并缓存。
- Markdown 使用 `MarkdownRenderer`，HTML 使用 sandbox iframe，代码和纯文本直接展示。
- `/api/*` 下载走带认证 fetch + Blob；外部链接直接新窗口打开。

`useAgentChat` 会把 `/api/v2/files?thread_id=...` 返回的会话文件映射为 file 工件，也会处理 SSE 里的 `artifact` / `artifact_update`。

### HistorySidebar

登录用户调用：

| 操作 | API |
| :--- | :--- |
| 列表 | `GET /api/v2/conversations` |
| 重命名 | `PATCH /api/v2/conversations/{threadId}` |
| 删除 | `DELETE /api/v2/conversations/{threadId}` |
| 消息恢复 | `GET /api/v2/conversations/{threadId}/messages` |
| 回滚 | `POST /api/v2/conversations/{threadId}/rollback` |

访客模式使用 `guestIndexedDb.ts` 的 `listGuestConversations()`、`renameGuestConversation()`、`deleteGuestConversation()`、`clearGuestConversations()`。

### 对话附件与个人文件

`useFileUpload.ts` 管理附件状态：

```typescript
type UploadParseMode = 'auto' | 'mineru';
type FileAttachmentStatus = 'pending' | 'uploading' | 'ready' | 'error';
type FileCategory = 'multimodal' | 'short_doc' | 'long_doc' | 'failed' | 'pending';
type Strategy = 'vision' | 'context' | 'indexed' | 'none';
```

上传流程：

```text
addFiles(files)
  └── status=pending，本地图片生成 previewUrl
uploadPendingFiles(threadId)
  └── POST /api/v2/chat-files/upload
      FormData: thread_id, parse_mode, files[]
      → file_id、file_category、parse_status、strategy、content_length
sendMessage()
  └── metadata.file_ids = getReadyFileIds()
```

个人文件库页面 `MyFilesView.vue` 使用 `/api/v1/my-files` 做列表/搜索/过滤，`/api/v1/files/upload` 上传单个文件，`PUT/DELETE /api/v1/my-files/{file_id}` 重命名和软删除。

### MarkdownRenderer

渲染管线：

```text
原始文本
  → 处理 <think> 块
  → 提取并渲染 $$...$$、\[...\]、$...$、\(...\) 数学公式
  → Marked 解析 Markdown
  → DOMPurify 过滤并扩展 KaTeX/SVG 白名单
  → 还原数学占位符
```

---

## 与后端 Agent 的映射

| 前端模块 | 后端/接口 | 数据流 |
| :--- | :--- | :--- |
| `AgentAPI.queryStream` | `POST /api/v2/agent-runtime/query` | 登录用户主 SSE |
| `AgentAPI.queryStream` | `POST /api/v2/guest/query` | 访客轻量 SSE |
| `ChatBox.selectedKbs` | `kb_ids` | 后端 Agent 工具 `multi_kb_search` 使用 |
| `ChatBox.selectedSkills` | `selected_skills` / `workflow` | 技能与深度研究编排 |
| `ReasoningBlock` | `reasoning.delta` / `thinking` | 思考流 |
| `WorkflowTimeline` | `workflow.step` / `plan.*` / `coordination.*` | 工作流与子智能体过程 |
| `ArtifactSidebar` | `/api/v2/files` + SSE artifact event | 工件恢复、预览、下载 |
| `FollowUpSuggestions` | `follow_up.created` / `follow_up_suggestions` | 追问建议 |
| `useFileUpload` | `/api/v2/chat-files/upload` | 对话附件解析并将 file_ids 传入 Agent |
| `MyFilesView` | `/api/v1/my-files`、`/api/v1/files/upload` | 个人文件库 |
| `useVoiceInput` | `/api/v1/asr/transcribe` | 语音转文本 |

---

## 开发状态与建议

- [x] **对话历史持久化**：登录用户通过 conversations API；访客通过 IndexedDB。
- [x] **多轮上下文**：通过 `thread_id` 与后端会话隔离实现。
- [x] **对话附件**：本地上传、粘贴图片、拖拽、个人文件库选择、`file_ids` 注入 Agent metadata 已落地。
- [x] **工件侧栏**：支持会话工件恢复、SSE artifact 更新、markdown/html/code/text/file 预览和下载。
- [x] **思考流与追问建议**：`reasoning.delta` / `thinking` 与 `follow_up.created` 已接入。
- [x] **子智能体过程展示**：`WorkflowTimeline` + `SubagentWorkflowSwitcher` + `SubagentWorkflowTimeline` 已接入。
- [x] **语音输入**：`useVoiceInput` + ASR 转写已落地。
- [x] **停止生成**：`cancelStream()` 使用 `AbortController` 中止当前 SSE。
- [x] **编辑后重发与导出**：用户消息可编辑重发；回答可复制、导出 PDF/Word。
- [ ] **反馈采集**：点赞/点踩、引用和结构化用户反馈仍待实现。
- [ ] **代码沙箱展示增强**：当 Agent 调度 Sandbox 执行 Python/Bash 时，可进一步在 Timeline 中展示代码与执行输出。
