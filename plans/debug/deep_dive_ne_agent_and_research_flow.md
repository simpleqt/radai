# 深度审查：NEFullFeatureAgent + 深研全流程

> 文件：`ne_full_feature_agent.py`（1040 行）、`research_draft_agent.py`、`custom/workflow/research_state.py`、`agent.py` 深研分支

---

## 1. NEFullFeatureAgent 函数地图

| 方法 | 行约 | 职责 |
|------|------|------|
| `__init__` | 76–132 | lightweight/deep_research 标志；KB/skills；超时常量 |
| `_normalize_selected_skills` | 134 | 去重排序 |
| `_create_summarization_middleware` | 143 | Context 压缩中间件 |
| `_get_selected_skill_defs` | 154 | ScopedSkillsProvider |
| `_build_agent` | 161 | LangGraph compile + middleware 链 |
| `_build_system_prompt` | 224 | 调 shared prompts |
| `_get_tools` | 309 | ToolAssemblyPipeline |
| `_emit_graph_artifacts` | 324 | 从 state.artifacts 补发 SSE |
| `_resolve_history_image_tokens` | 392 | 历史消息图片 token |
| `_finalize_deep_research_report` | 465 | 超时兜底报告 |
| **`aquery_stream`** | 550–978 | **主管线** |
| `ainvoke` | 980 | 非流式聚合 chunk |
| `get_info` | 994 | /agent/info 元数据 |

### 1.1 `aquery_stream` 分支

```
aquery_stream
├── lightweight_mode → LightweightQAAgent（见 deep_dive_lightweight_qa_agent.md）
└── full
    ├── NEAgentStreamHandler
    ├── set_tool_budgets / skill_state
    ├── graph.astream_events / 超时 / 深研 grace
    ├── handler.handle → yield SSE dict
    ├── handler.finalize
    └── _finalize_deep_research_report（条件触发）
```

**行数**：full 路径约 **400+ 行** 仍在单方法内 — 建议拆 `DeepResearchStreamRunner` / `NormalStreamRunner`。

---

## 2. ToolAssemblyPipeline（关联）

| 能力 | 条件 |
|------|------|
| `multi_kb_search` | kb_ids 非空；skill gate nuclear-electronics |
| `web_search` / `academic_search` | use_search / selected_skills |
| `chat_file_search` | thread_id |
| `task` 子 agent | subagent_enabled |
| skill 声明工具 | `_load_declared_skill_tool` 硬编码 if-else |

见 `tool_assembly.py` — 与架构文档「Skill 驱动」仍有差距。

---

## 3. 深研状态机（后端）

### 3.1 `ResearchWorkflowState`（`research_state.py`）

| 状态 | 含义 |
|------|------|
| `draft_generated` | 草案已生成 |
| `awaiting_user_confirmation` | 等待确认 |
| `plan_revised_by_user` | 用户修订 |
| `confirmed_ready` | 已确认待跑 |
| `running` | 后台执行中 |
| `awaiting_clarification` | 澄清 |
| `completed` / `fallback_archived` / `cancelled` | 终态 |

持久化：`Conversation.metadata_.research_workflow`（`agent.py` `_persist_research_workflow_state`）。

### 3.2 API 层深研分支（`agent.py` L578–780 量级）

| 阶段 | 触发 | SSE |
|------|------|-----|
| 取消 | `confirmation_status=cancelled` | status + chunk + [DONE] |
| 草案生成 | deep_research 且未 confirmed | `draft_chunk` 流 |
| 草案事件 | 生成完 | `event.research_draft_created` |
| 确认启动 | `confirmed` | `background_task_started` + 启动 runner |
| 实时深研（未转后台） | 少见路径 | 同 full agent 流 |

**csh_agent.py**：逻辑镜像，需双份维护（91% 重复）。

---

## 4. ResearchDraftAgent

| 方法 | 职责 |
|------|------|
| `build_draft` | 一次性 ainvoke 大纲 |
| `build_draft_stream` | 流式草案（agent.py 使用） |
| `_sanitize_thinking_text` | 剥离 think 标签 |

**质量**：177 行，职责清晰；与主 Agent 解耦良好 ✅

---

## 5. 后台任务（`tasks/`）

| 组件 | 职责 |
|------|------|
| `TaskManager` | 进程内任务注册、订阅队列 |
| `BackgroundTask` | 双写内存 + `Conversation.metadata_.background_tasks` |
| `run_deep_research_background` | DeepResearchOrchestratorAgent 流式 + think 拆分 |
| DB helpers | cancel flag、read active task |

**持久化**：刷新后靠 `getThreadActiveTask` + 前端 subscribe 恢复。

**问题**：

- 内存 TaskManager 与多 worker 不共享 — 文档应注明单进程/粘性会话  
- DB 轮询 SSE（`stream_task_events`）1s 间隔 — 见 tasks/manager

---

## 6. 端到端序列（确认 → 后台）

```text
┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌─────────────────────────────┐
│        useAgentChat         │ │          agent.py           │ │     ResearchDraftAgent      │ │         TaskManager         │ │          runner.py          │ │DeepResearchOrchestratorAgent│
└─────────────────────────────┘ └─────────────────────────────┘ └─────────────────────────────┘ └─────────────────────────────┘ └─────────────────────────────┘ └─────────────────────────────┘


```

---

## 7. 与架构文档对照

| 文档目标 | 现状 |
|----------|------|
| ne agent 从 2241 瘦身 | ✅ ~1040，仍含 aquery_stream 大块 |
| Skill 驱动 workflow | ❌ 仍 task_type + confirmation_status |
| 子 agent 可扩展 | ⚠️ task_tool + SubagentLimit |
| 流式独立 | ⚠️ Handler 独立，Agent 仍厚 |

---

## 8. P1 整改建议（设计）

1. 抽 `DeepResearchWorkflowService`：草案/确认/启任务/状态持久化  
2. `NEFullFeatureAgent.aquery_stream` 拆文件  
3. NE/CSH 共享 workflow 模块，API 只换 Agent 类  
4. 文档注明 TaskManager 单进程假设  
