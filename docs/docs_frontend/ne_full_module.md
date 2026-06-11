# 专家模式模块 (NE-Full Module)

> **最后同步**：2026-05-31（SSE 协议见 [agent_platform_standardization_plan.md](../agent_platform_standardization_plan.md) §11；后端 Agent 见 [agent_engine_module.md](../docs_backend/agent_engine_module.md)）

专家模式模块位于 **`/app/:threadId?`（桌面）** 与 **`/m/:threadId?`（移动）** 路由，是对接后端 V2 Agent 的全功能对话界面。旧路径 `/ne-full` 会自动重定向至上述路由。

支持推理过程展示、工具调用状态追踪、SSE 流式响应、会话历史、工件预览、追问建议与对话附件上传。

---

## 📂 文件结构

```text
views/ne-full/
├── index.vue                     # 页面入口（HistorySidebar + 主区 + ArtifactSidebar）
├── MyFilesView.vue               # 个人文件库
├── composables/
│   ├── useAgentChat.ts           # V2 对话逻辑
│   ├── useFileUpload.ts          # 对话附件上传
│   └── useVoiceInput.ts          # 语音输入 + ASR 转写
└── components/
    ├── ChatBox.vue
    ├── MessageList.vue
    ├── MarkdownRenderer.vue
    ├── ReasoningBlock.vue
    ├── ToolBadge.vue
    ├── KBSelector.vue            # 知识库双选
    ├── HistorySidebar.vue        # 会话历史
    ├── ArtifactSidebar.vue       # 工件预览
    ├── FollowUpSuggestions.vue   # 追问建议
    ├── FilePreviewChip.vue       # 附件预览
    ├── ResearchDraftCard.vue     # 深度研究大纲确认
    ├── ProcessEventsPanel.vue    # 过程事件
    ├── WorkflowTimeline.vue      # 工作流时间线
    └── SubagentWorkflowTimeline.vue
```

---

## 1. 页面入口 (`index.vue`)

### 组件结构
```
index.vue
├── <div> 全屏容器 (bg-[#f9fafb] / dark:bg-[#0b0f1a])
│   ├── <div> 背景渐变装饰 (radial-gradient)
│   ├── <header> 顶栏
│   │   ├── Agent 状态指示灯 (emerald-500)
│   │   ├── 标题 "核电子学智能助手 v2"
│   │   ├── 模型名称 + 专家模式标签
│   │   └── 清空对话按钮
│   └── <main> 对话区域
│       ├── MessageList 消息列表
│       └── ChatBox 输入框 (底部固定)
```

### 关键逻辑

| 功能 | 实现 |
| :--- | :--- |
| Agent 信息加载 | `loadAgentInfo()` → `AgentAPI.getInfo()` → 填充 header |
| 消息发送 | `handleSend(text)` → `useAgentChat.sendMessage(text, threadId)` |
| 线程 ID | 从 `route.params.threadId` 获取，配合 `HistorySidebar` 切换会话 |
| 清空对话 | `clearMessages()` → 重置 `messages` 和 `status` |

### Header 信息展示
```html
<span>{{ agentInfo?.model || '默认模型' }}</span>
<span v-if="agentInfo?.subagent_enabled" class="专家模式标签">专家模式</span>
```

---

## 2. 对话逻辑 (`useAgentChat.ts`)

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
    selectedSkills,
    researchMode,
    artifacts,
    activeBackgroundTask,
    loadAgentInfo,
    loadHistory,
    sendMessage,
    clearMessages,
    cancelStream,           // AbortController 中止普通 SSE
    cancelBackgroundTask,   // POST /task/{id}/cancel
    checkAndResumeBackgroundTask,
    // ...
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
  followUpSuggestions?: FollowUpSuggestion[];
  attachments?: FileAttachment[];
  isStreaming?: boolean;
}
```

### AgentInfo 类型

```typescript
interface AgentInfo {
  name: string;
  model: string | null;
  default_model?: string | null;       // DB 默认模型
  available_models?: ModelOption[];    // 可选模型列表
  kb_ids: string[];
  available_kbs?: KbOption[];          // 后端动态 KB 名称（agent_kb_bindings）
  subagent_enabled: boolean;
  search_enabled?: boolean;
  selected_skills?: string[] | null;
  features: string[];
}
```

知识库名称由后端 `agent_kb_bindings` + `knowledge_bases` 表动态查询，前端不再硬编码映射。

**与后端 RAG 的关系**：用户勾选 KB 后，请求体 `configurable.kb_ids` 传入 V2 Agent；工具 `multi_kb_search` 对各库并发调用 `retrieve_context`（DirectRetriever 四路 + Profile 驱动 Stage-2，默认核电子学 + 期刊为 **domain/MMR_ONLY**），再 RRF 融合。前端不直接调用 `/api/v1/rag/retrieve`。详见 [知识库算法和架构.md](../知识库算法和架构.md) §8。

### SSE 事件处理

`AgentAPI.queryStream()` 解析 SSE，主要事件：

| type | 前端处理 |
| :--- | :--- |
| `chunk` | 追加回答正文 |
| `thinking` | `ReasoningBlock` |
| `status` | `ToolBadge` / statusHistory |
| `event` | `ProcessEventsPanel` / `WorkflowTimeline` |
| `follow_up_suggestions` | `FollowUpSuggestions` |
| `draft_chunk` | `ResearchDraftCard` 流式大纲片段 |
| `event`（深度研究） | 见下表 `event.kind` |

**深度研究 `event.kind`**（`ResearchDraftCard` / `MessageList` / `useAgentChat` 消费）：

| kind | 前端行为 |
| :--- | :--- |
| `research_draft_created` | 展示/更新研究清单卡片（`plan_content`、`draft_id`） |
| `research_draft_updated` | 同上（修订后刷新，后端若发出） |
| `research_confirmed` | 确认成功提示，含 `background_task_id` |
| `background_task_started` | 切换为 `AgentAPI.subscribeTaskStream(taskId)` 订阅后台任务 SSE |

后台任务流：`GET /api/v2/agent-runtime/{agent_code}/task/{task_id}/stream?thread_id=...`。

完整协议见 [agent_platform_standardization_plan.md](../agent_platform_standardization_plan.md) §11。

### 消息发送流程

```
sendMessage(question, threadId?)
  ├── 1. 前置检查: question.trim() && !isBusy
  ├── 2. 添加用户消息: { role: 'user', content: question }
  ├── 3. 创建助手 reactive 消息:
  │   └── { role: 'assistant', content: '', reasoning: '',
  │        statusHistory: [], isStreaming: true }
  ├── 4. 调用 AgentAPI.queryStream():
  │   ├── onChunk(textChunk):
  │   │   └── assistantMsg.content += textChunk
  │   ├── onStatus(statusMsg):
  │   │   ├── 去重追加到 statusHistory
  │   ├── onThinking(text): reasoning += text
  │   ├── onFollowUpSuggestions: 填充 followUpSuggestions
  │   └── threadId 从路由或 HistorySidebar 传入
  ├── 5. 异常: content += [发生错误: ...]
  └── 6. 最终: isBusy=false, isStreaming=false
```

> **关键设计**: 助手消息使用 `reactive()` 而非 `ref()` 创建，确保对 `content`、`reasoning`、`statusHistory` 的修改能触发视图更新。

---

## 3. Agent API (`api/agent-v2.ts`)

### AgentAPI.getInfo()

```typescript
GET /api/v2/agent/info
→ Response: AgentInfo
```

### AgentAPI.queryStream()

```typescript
queryStream(
  question: string,
  onChunk: (text: string) => void,
  onStatus: (status: string) => void,
  threadId?: string
): Promise<void>
```

**请求**:
```
POST /api/v2/agent/query
Content-Type: application/json
Body: { "question": "...", "thread_id?": "..." }
```

**SSE 解析**:
```
ReadableStream → TextDecoder
  → 按 "\n\n" 分割事件块
  → 每个事件以 "data: " 开头
  → JSON.parse(dataStr)
    ├── data.type === 'thinking'   → onThinking(data.content)
    ├── data.type === 'follow_up_suggestions' → onFollowUpSuggestions
    └── dataStr === '[DONE]'       → 流结束
```

---

## 4. 组件详解

### 4.1 ChatBox (`ChatBox.vue`)

输入框组件，负责接收用户输入并触发发送。

| 特性 | 实现 |
| :--- | :--- |
| 自适应高度 | `textarea.style.height = scrollHeight + 'px'`，最大 300px |
| 发送快捷键 | `Enter` 发送，`Shift+Enter` 换行 |
| 状态指示 | 顶部显示当前 status（带 ping 动画） |
| 发送按钮 | 空闲时显示箭头图标，忙碌时显示旋转加载图标 |
| 禁用逻辑 | `isBusy || !question.trim()` 时禁用发送 |

**Props**:
```typescript
interface Props {
  isBusy: boolean;
  status: string;
}
```

**Emits**:
```typescript
emit('send', text: string)
```

### 4.2 MessageList (`MessageList.vue`)

消息列表组件，渲染对话消息并自动滚动。

| 特性 | 实现 |
| :--- | :--- |
| 自动滚动 | `watch(messages, scrollToBottom, { deep: true })` |
| 用户消息 | 右对齐，indigo-600 背景，圆角气泡 |
| 助手消息 | 左对齐，包含 ToolBadge + ReasoningBlock + MarkdownRenderer |
| 流式指示 | 三点弹跳动画 (`animate-bounce` + `animation-delay`) |
| 空状态 | 居中展示图标 + 标题 + 描述文字 |

**消息渲染结构**:
```
助手消息
├── ToolBadge × N       (statusHistory 中的每个状态)
├── ReasoningBlock      (reasoning 非空时显示)
├── MarkdownRenderer    (content 非空时显示，白色卡片)
└── 流式指示器          (isStreaming 时显示)
```

### 4.3 MarkdownRenderer (`MarkdownRenderer.vue`)

核心渲染组件，处理 Markdown + 数学公式 + 安全过滤。

**渲染管线** (5 步):
```
原始文本
  → 1. 处理 <think/> 块 (替换为 .think-block HTML)
  → 2. 提取并渲染数学公式 (KaTeX)
  │   ├── $$...$$ 和 \[...\] → 块级公式 (displayMode=true)
  │   └── $...$ 和 \(...\) → 行内公式 (displayMode=false)
  │   └── 使用占位符 [[[MATH_DISP_N]]] / [[[MATH_INLINE_N]]] 暂存
  → 3. Marked 解析 Markdown
  → 4. DOMPurify 安全过滤 (允许 KaTeX SVG 标签)
  → 5. 还原数学占位符为渲染后的 HTML
```

**数学公式处理策略**:
- 使用 `Map<string, string>` 存储占位符 → 渲染 HTML 映射
- 先提取数学公式，避免 Markdown 解析器破坏 LaTeX 语法
- KaTeX 配置: `throwOnError: false, strict: false`

**DOMPurify 白名单扩展**:
```typescript
ADD_TAGS: ['math', 'annotation', 'semantics', 'mrow', 'msub', 'msup',
  'mi', 'mn', 'mo', 'mtext', 'msqrt', 'mroot', 'mfrac', 'mfenced',
  'mtable', 'mtr', 'mtd', 'svg', 'path', ...]
ADD_ATTR: ['class', 'style', 'aria-hidden', 'viewBox', 'd', 'fill',
  'stroke', 'stroke-width', ...]
```

**学术排版样式** (scoped CSS):
| 元素 | 样式特点 |
| :--- | :--- |
| 数学块 | 居中、圆角卡片、hover 阴影增强 |
| 表格 | 圆角溢出隐藏、hover 放大 1.01x、斑马纹 |
| 标题 | h1 底部 indigo 装饰线、h2 左侧 indigo 竖条 |
| 引用块 | indigo 左边框、衬线字体、大号斜体 |
| Think 块 | 灰色左边框、等宽字体、小号斜体 |

### 4.4 ReasoningBlock (`ReasoningBlock.vue`)

推理过程折叠面板，展示 Agent 的思考链。

| 特性 | 实现 |
| :--- | :--- |
| 默认展开 | `isExpanded = ref(true)` |
| 切换动画 | 箭头图标 `rotate-180` + `v-show` 展开/收起 |
| 内容渲染 | 按 `\n` 分割，每行前加左边框装饰 |
| 标题 | "智能推理过程" + ping 动画指示灯 |

**Props**:
```typescript
interface Props {
  reasoning: string;
}
```

### 4.5 ToolBadge (`ToolBadge.vue`)

工具调用状态徽章，显示 Agent 正在使用的工具。

**工具名称映射**:
| 状态关键词 | 显示名称 | 图标 |
| :--- | :--- | :--- |
| `multi_kb_search` | 知识库检索 | database |
| `web_search` | 联网搜索 | globe |
| `academic_search` | 学术检索 | book-open |
| `query_excel_data` | 实验数据分析 | bar-chart |
| `task` | 子智能体委派 | users |
| 其他 | 原始状态文本 | cpu |

**样式**: 圆角胶囊、emerald 状态灯、fade-in 入场动画。

### 4.6 语音输入 (`useVoiceInput.ts`)

状态机驱动的语音输入：`idle` → `recording` → `transcribing` → `editing`。

| 导出 | 说明 |
| :--- | :--- |
| `phase` | 当前阶段 |
| `pendingText` | 转写结果（editing 阶段可编辑后发送） |
| `recordingSeconds` | 录音时长，最长 60 秒 |
| `startRecording` / `stopRecording` | 按住说话交互 |

详见 `backend/api/routers/asr.py`。

---

## 5. 与后端 Agent 的映射

| 前端组件 | 后端中间件/组件 | 数据流 |
| :--- | :--- | :--- |
| ToolBadge `multi_kb_search` | `MultiKBSearchTool` | Agent 调用工具 → SSE status 事件 |
| ToolBadge `task` | `SubagentLimitMiddleware` | 子智能体委派 → SSE status 事件 |
| ReasoningBlock | `thinking` SSE 事件 | 推理链 → `ReasoningBlock` 折叠面板 |
| FollowUpSuggestions | `follow_up_suggestions` 事件 | 追问建议渲染 |
| ResearchDraftCard | `draft_chunk` + `research_draft_created` 等 | 深度研究大纲确认 |
| subscribeTaskStream | `background_task_started` | 后台深研进度与报告流 |
| KBSelector | `configurable.kb_ids` | 知识库双选 → 后端 RRF 检索 |
| useVoiceInput | `/api/v1/asr/transcribe` | 语音 → 文本 |
| MarkdownRenderer | LLM 输出 | 流式文本 → 实时 Markdown 渲染 |
| ChatBox | `ClarificationMiddleware` | 用户输入 → POST 请求 |

---

## 6. 开发状态与未来建议

- [x] **对话历史持久化**: 接入后端 `thread` + `checkpoint` API，支持刷新后恢复对话。（已完成：通过 `conversations` 会话 API、`HistorySidebar` 会话历史侧栏以及 Redis/SQLite checkpointer 协同实现）
- [x] **多轮对话上下文**: 传递 `thread_id` 实现多轮对话状态保持。（已完成：后端强制基于 `u_{user_id}__t_{thread_id}` 做用户和会话级完全逻辑隔离）
- [x] **文件上传与预览**: ChatBox 支持选择文件上传，与 V2 接口及 `useFileUpload` 对接。（已完成：基础文件流与前端 FilePreviewChip 预览芯片均已落地，会话级 RAG 深度索引正在收敛）
- [x] **图片渲染与工件侧栏**: 从检索结果或 Agent 动作中提取多模态图片和生成的长文本，展示在工件侧栏中。（已完成：核心工件侧栏 `ArtifactSidebar` 与 MarkdownRenderer 图片渲染均已闭环）
- [x] **思考流与追问建议**: SSE `thinking` + `follow_up_suggestions` 事件。（已完成）
- [x] **语音输入**: `useVoiceInput` + ASR 转写。（已完成，见 `backend/api/routers/asr.py`）
- [x] **停止生成**: `useAgentChat.cancelStream()` + `AgentAPI.queryStream({ signal })` 中止普通 SSE；深研后台任务用 `cancelBackgroundTask()`。（运行中 LangGraph 无通用服务端 cancel，见 [agent_engine_module.md](../docs_backend/agent_engine_module.md)）
- [ ] **消息引用/反馈**: 支持对回答进行点赞/点踩、复制与引用，收集用户反馈。
- [ ] **代码沙箱展示**: 当 Agent 调度 Sandbox 执行 Python/Bash 代码时，在 UI Timeline 中渲染代码及执行输出。
