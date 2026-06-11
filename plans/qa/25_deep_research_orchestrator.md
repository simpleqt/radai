# 25 · 深度研究 Orchestrator 专章

> 18 道题：大纲 → 确认 → 后台 TaskManager → Orchestrator 并行委派 → 报告归档。  
> 文档：[`deep_research_module.md`](../../docs/docs_backend/deep_research_module.md) · 源码：`CoordinatedAgentWorkflow` · `task_tool.py` · `registry.yaml`

---

## 工作流总览

```text
┌──────────────────┐
│ draft            │
└──────────────────┘
    ▲  POST task_type=deep_research
    │
    ▼

  (CoordinatedAgentWorkflow 流式大纲)
    │
    ▼
┌──────────────────┐
│ awaiting         │
└──────────────────┘

  (confirmation_status=revised)
    │
    ▼
┌──────────────────┐
│ revised          │
└──────────────────┘

  (重新生成)
    │
    ▼
┌──────────────────┐
│ draft            │
└──────────────────┘

  (cancelled)
    │
    ▼
┌──────────────────┐
│ cancelled        │
└──────────────────┘

  (confirmed)
    │
    ▼
┌──────────────────┐
│ running          │
└──────────────────┘

  (save_deep_research_report)
    │
    ▼
┌──────────────────┐
│ completed        │
└──────────────────┘

  (POST cancel)
    │
    ▼
┌──────────────────┐
│ cancelled        │
└──────────────────┘


```

```text
┌────────────────────────────┐ ┌────────────────────────────┐ ┌────────────────────────────┐ ┌────────────────────────────┐ ┌────────────────────────────┐ ┌────────────────────────────┐ ┌────────────────────────────┐
│           用户/前端            │ │     agent_runtime.py     │ │ CoordinatedAgentWorkflow │ │        TaskManager         │ │run_deep_research_background│ │    并行子 Agent              │
└────────────────────────────┘ └────────────────────────────┘ └────────────────────────────┘ └────────────────────────────┘ └────────────────────────────┘ └────────────────────────────┘ └────────────────────────────┘


```

| 阶段 | 组件 | 与 Full/Lightweight 关系 |
|------|------|--------------------------|
| 大纲 | CoordinatedAgentWorkflow 大纲阶段 | **独立编排**，非 LangGraph 主图 |
| 实时路由 | WorkflowRegistry → CoordinatedAgentWorkflow | **第三档**，优先于 full/lightweight |
| 后台执行 | runner + CoordinatedAgentWorkflow | **与主 query SSE 分离** |
| 检索 | deep-research 子 Agent | Coordinated 编排器**禁止**直接 KB/Web |

---

## L1 概念（Q1–Q6）

### Q1：深度研究如何触发？

**一句话结论：** Router `_resolve_mode` **最优先**判 `deep_research` — `task_type == "deep_research"` 或 `mode ∈ {deep-research, deep_research}`。

**在本项目中的实现：** `application/agent_workflows/registry.py`；API 层还可识别工作流 action。

**常见误区：** ❌ 有 Skill 才能深研 → ✅ 深研 **独立第三档**，与 `selected_skills` 无必然关系。

**延伸：** 访客 API **400 拒绝** deep_research。

---

### Q2：深研与 Full 模式的核心区别？

**一句话结论：** Full 是 **主 Agent 直接 tool loop**；深研是 **Orchestrator 编排 + 子 Agent 检索 + 后台长任务**。

| 维度 | Full | Deep Research |
|------|------|---------------|
| 入口 | StandardAgentWorkflow | CoordinatedAgentWorkflow |
| 主 Agent 检索 | 可调 multi_kb_search | **blocked**，必须 task 委派 |
| 用户确认 | 可选 Clarification | **必须** 大纲 confirmed |
| SSE | 单流 query | query 流 + **task stream** |
| 产物 | 对话消息 | **报告 artifact** (MinIO) |

---

### Q3：CoordinatedAgentWorkflow 大纲阶段做什么？

**一句话结论：** 根据用户问题 **流式生成研究大纲/清单**，通过 `draft_chunk` SSE 推前端 `ResearchDraftCard`，**不执行检索**。

**源码：** `application/agent_workflows/` — CoordinatedAgentWorkflow 大纲阶段

**走一遍例子：** 用户问「写核废物监测方案」→ 大纲含章节、检索维度、预期产出 → 用户改 plan → `revised` 重生成。

---

### Q4：`confirmation_status` 三值含义？

**一句话结论：**

| 值 | 行为 |
|----|------|
| `revised` | 带 `research_plan` 重新走 Draft |
| `cancelled` | 终止工作流，写 `ResearchWorkflowState` |
| `confirmed` | 创建 TaskManager 任务 → `run_deep_research_background` |

**持久化：** `Conversation.metadata_.research_workflow`

**字段名注意：** 修订内容在 **`research_plan`**，非 `revision_feedback`（文档 §5）。

---

### Q5：为何深研要拆成「大纲 SSE + 后台 task SSE」？

**一句话结论：** **交互与长任务解耦** — 大纲需用户确认（秒级流）；研究执行可能 **数分钟**，主 HTTP 连接不可一直占满；确认后前端改订 **`GET task/{id}/stream`**。

**TaskManager API（对称 NE/CSH）：**

- `GET .../task/{id}/status`
- `GET .../task/{id}/stream`
- `POST .../task/{id}/cancel`
- `GET .../thread/{id}/task`

---

### Q6：CoordinatedAgentWorkflow 的实现方式

**一句话结论：** 深研工作流通过 `CoordinatedAgentWorkflow` 实现，内置大纲生成 → 用户确认 → 并行子 Agent 检索 → 报告生成；由 `registry.yaml` 配置驱动。

**源码：** `config/workflows/registry.yaml` 中 `deep_research` 条目，provider 为 `create_coordinated_workflow`

**设计意图：** 复用 Full 建图、checkpoint、StreamHandler；仅 override Prompt 与 `_get_tools` 过滤。

---

## L2 实现（Q7–Q13）

### Q7：主 Orchestrator 为何移除 multi_kb_search / web_search？

**一句话结论：** **职责分离** — 主控只做拆解、并行委派、汇总、归档；检索在 **deep-research 子 Agent** 内完成，避免主 Agent 单线程串行检索。

**源码：** `_get_tools` L49–59 — `blocked = {multi_kb_search, web_search, duckduckgo_search}`

```text
┌──────────────────────────────────────┐
│ O                                    │
└──────────────────────────────────────┘
         (task ×2 同轮)
           │
           ▼
         (task ×2 同轮)
           │
           ▼
           │
           ▼
┌───────────────────────────┐  ┌────────────────┐  ┌────────────────┐
│ save_deep_research_report │  │ S1             │  │ S2             │
└───────────────────────────┘  └────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
┌─────────────────┐  ┌────────────────┐
│ multi_kb_search │  │ web_search     │
└─────────────────┘  └────────────────┘

```

---

### Q8：Prompt 硬规则「同轮 2 个 task」为何重要？

**一句话结论：** 强制 **并行研究两维度**，避免 Orchestrator 串行等子 Agent → 降低总 wall-clock；违反则 Prompt 要求补发第二个 task。

**文档/Prompt 摘录：** `<deep_research_orchestrator_hard_rules>` — 禁止串行等待、两维度不能重复。

**面试延伸：** SubagentLimitMiddleware `max_concurrent_subagents=2` 与 Prompt 一致。

---

### Q9：`deep-research` 子 Agent 工具白名单

**一句话结论：** `_SUBAGENT_CONFIGS["deep-research"]` — **允许** multi_kb_search、web_search、Skill 读取、chat_file_search；**禁止** task（防嵌套）、ask_clarification、save_deep_research_report。

**源码：** `core/tools/task_tool.py` L41–70

**输出格式：** 子任务结论 + 证据摘要 + 局限 + 建议 + 参考来源（供主 Agent 汇总）。

---

### Q10：`run_deep_research_background` 做什么？

**一句话结论：** 后台 asyncio 任务 — 通过 AgentProfile 加载域配置 → CoordinatedAgentWorkflow 执行 → 事件写入 **TaskManager** + 更新 **ResearchWorkflowState**。

**源码：** `application/tasks/runner.py`

**域配置：** 通过 `agent_code` 加载对应 AgentProfile YAML，Profile 决定 prompt_profile、tool_policy 等。

---

### Q11：TaskManager 职责

**一句话结论：** 单例 **任务状态机 + 事件广播** — 供前端 task stream 订阅；支持 cancel 标志（含 DB 轮询 `check_cancel_flag_from_db`）。

**源码：** `application/tasks/manager.py` — `BackgroundTask`、`TaskEvent`、`get_task_manager()`

**与 checkpoint 关系：** Graph 状态仍走 SQLite checkpointer；TaskManager 管 **产品级进度**，非 LangGraph 内部 step。

---

### Q12：深研专用 SSE 事件

**一句话结论：**

| type / kind | 含义 |
|-------------|------|
| `draft_chunk` | 大纲流式片段 |
| `research_draft_created` | 草案完成，含 draft_id |
| `research_confirmed` | 用户确认，含 background_task_id |
| `background_task_started` | 可断开主 query，改订 task stream |
| `tool_end` + save_deep_research_report | 报告已归档 |

**通用 chunk/status** 仍可能出现（等待确认提示等）。

---

### Q13：`save_deep_research_report` 与工件路径

**一句话结论：** 工具将报告写入 **ScopedStorage/MinIO**：`users/{user_id}/threads/{thread_id}/artifacts/`；前端 `ArtifactSidebar` + GET artifacts API 预览。

**源码：** `application/tools/providers/deep_research_report_tool.py`

**延伸：** 与对话 messages 分离 — 长报告不进 permanent ToolMessage（避免 token 爆炸）。

---

## L3 权衡与排障（Q14–Q18）

### Q14：CSH 与 NE 深研差异

**一句话结论：** **流程同构** — Draft → confirm → background；差异在 **AgentProfile YAML**、**Prompt 域**、**默认 kb_ids**。

**工厂：** `registry.yaml` 中 `deep_research` 条目，通过 AgentProfile 配置域差异

**文档：** deep_research_module.md §6.1

---

### Q15：深研取消如何实现？

**一句话结论：** 前端 `POST .../task/{id}/cancel` → TaskManager/DB cancel flag → runner 循环内 `check_cancel_flag_from_db` → 写 `ResearchWorkflowState.cancelled`。

**已知局限：** LangGraph **无通用 cancel API**（agent_engine 演进表）— 取消粒度在 task runner 层。

---

### Q16：深研模式 StreamHandler 差异

**一句话结论：** `StreamHandler(is_deep_research=True)` — 更长 **超时预算**、不同 **max_output_chars**、深研事件映射策略。

**源码：** `application/agent_executors/streaming_runtime.py`

**误区：** ❌ 深研不走 Handler → ✅ 后台 runner 仍经 Orchestrator 的 aquery_stream + Handler 路径。

---

### Q17：排障表 — 深研常见现象

| 现象 | 查什么 | 期望 |
|------|--------|------|
| 大纲不出 | CoordinatedAgentWorkflow 日志 | draft_chunk SSE |
| 确认后无进度 | TaskManager task_id | background_task_started |
| 主 Agent 自己调 KB | Coordinated _get_tools | blocked 工具不在列表 |
| 只跑 1 个子任务 | Prompt + 模型 compliance | 同轮 2× task |
| 访客 400 | guest.py | 拒绝 deep_research |
| 报告找不到 | artifacts 路径 | save_deep_research_report tool_end |

---

### Q18：设计题 — 深研与 Full 共用 ContextPruning 是否合适？

**一句话结论（参考答法）：** Orchestrator **仍有** tool loop（task + save_report），子 Agent 返回大块 ToolMessage 会进主 thread — **应保留 ContextPruning** 或 **子 Agent 结果只保留摘要进主 state**；当前子 Agent 在 **独立线程/loop** 执行，主图 ToolMessage 体积取决于 task 工具返回策略。

**权衡：**

```text
┌────────────────────────┐
│ 子Agent 全量 evidence 回主图 │
└────────────────────────┘

  子Agent 全量 evidence 回主图 ──────────────► 主 thread token 爆

  主 thread token 爆 ──────────────► task 只回摘要+引用

  task 只回摘要+引用 ──────────────► 主 Orchestrator 可长研

  子Agent 全量 evidence 回主图 ──(风险)──► 主 thread token 爆
  task 只回摘要+引用 ──(收益)──► 主 Orchestrator 可长研

```

**延伸：** P1 增强 — 子 Agent Prompt/KB 由主 Agent kb_ids 传入（文档 §6.1）。

---

## 附录：与 Agent 专题交叉索引

| 本题 | 参见 |
|------|------|
| 路由第三档 | 19-Q26, 23-Q5, 24-D2 |
| task 子 Agent | 19-Q10, 22-W19 |
| SSE 协议 | 04-Q*, 22-W23 |
| Worker/后台 | 07-Q* |
| 会话 metadata | 16-Q* |

**30 分钟刷法：** Q1→Q5 口述流程 → Q7→Q9 Orchestrator/子Agent 分工 → Q12 SSE → Q17 排障表。
