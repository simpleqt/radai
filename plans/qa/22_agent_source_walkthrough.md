# 22 · 智能体源码走读题（W1–W40 · 按文件）

> 模拟「带面试官读代码」：每题给出**文件 + 行段 + 问题 + 参考答案**，适合结对复习。  
> 配合 [19_agent_architecture_implementation.md](./19_agent_architecture_implementation.md) 使用。

---

## 走读顺序图

```text
┌───────────────────┐
│ agent_runtime.py   │
└───────────────────┘

  agent_runtime.py ──────────────► AgentService

  AgentService ──────────────► WorkflowRegistry

  WorkflowRegistry ──────────────► tool_assembly_pipeline.py

  tool_assembly_pipeline.py ──────────────► lightweight_qa_agent.py

  lightweight_qa_agent.py ──────────────► streaming_runtime.py + stream_handler.py

  streaming_runtime.py ──────────────► task_tool.py

```

---

## 1. `api/routers/agent_runtime.py`

### W1：API 层如何将请求路由到 AgentService？

**看：** `AgentRuntimeQueryRequest` 模型

**答：** POST `/agent-runtime/query`，请求体含 `question`、`agent_code`、`thread_id`、`selected_skills`、`workflow`、`task_type` 等。直接调用 `AgentService.run_stream()`。

---

### W2：lightweight 工作流如何被解析？

**答：** `WorkflowRegistry.resolve_name()` — 无显式 `workflow`、无 Skill、Profile `default_workflow=lightweight` → 返回 `lightweight`。

---

### W3：deep_research 工作流如何被触发？

**答：** 显式 `request.workflow="deep_research"` 或 `task_type="deep_research"` → WorkflowRegistry 查找 `registry.yaml` → 返回 `CoordinatedAgentWorkflow`。

---

## 2. `application/agent_executors/profiled_executor.py` + `graph_builder.py`

### W4：`ProfiledAgentExecutor` 在 lightweight 分支做什么？

**答：**
1. `ProfiledGraphBuilder.build()` 组装模型 + 工具 + Middleware
2. `lightweight_mode=True` 时 `ToolAssemblyPipeline.assemble()` 返回空工具列表
3. 委托 `LightweightQAAgent.aquery_stream(...)`（不进入 full 图循环）

---

### W5：full 模式在进 `astream_events` 前重置了哪些请求级状态？

**答：**
- `reset_registry()` — 图片 URL
- `reset_turn_kb_context_budget()`
- `set_tool_budgets({multi_kb_search: N, web_search: 10, ...})` — deep 模式 budget 不同
- 创建 `StreamHandler(is_deep_research=...)`

---

### W6：`RuntimeFeatures` 在 `ProfiledGraphBuilder` 里 clarification 何时为 False？

**答：** `clarification=(workflow != "deep_research" and workflow != "lightweight")` — 深研和轻量都不启用澄清 Middleware。

---

### W7：`recursion_limit` 设多少？含义？

**答：** `100` — LangGraph 单轮对话最大图步数上限，防无限 tool loop（与 LoopDetection 互补）。

---

## 3. `core/agents/factory.py`

### W8：`middleware` 与 `features` 同时传入会怎样？

**看：** L64–67

**答：** `raise ValueError("Cannot specify both 'middleware' and 'features'.")`

---

### W9：guardrail feature 为 True 但未传实例会怎样？

**看：** L129–133

**答：** `raise ValueError("guardrail=True requires a custom AgentMiddleware instance")` — **无内置默认**，必须传 `ToolGuardrailMiddleware` 实例。

---

### W10：clarification feature 为 True 时 extra_tools 多了什么？

**看：** L159–165

**答：** 追加 `ask_clarification_tool` 到工具列表（除 ClarificationMiddleware 外）。

---

## 4. `application/tools/tool_assembly_pipeline.py`

### W11：`lightweight_mode=True` 时 `assemble()` 返回什么？

**看：** L123–126（语义）

**答：** **空列表 `[]`** — 工具不挂 LangGraph；检索由 LightweightQAAgent 内联。

---

### W12：`_create_multi_kb_search_tool` 做了什么特殊处理？

**看：** L72–80+

**答：** 创建**闭包工具**，内部调 `execute_multi_kb_search`，注入 `self._kb_ids`；full agent 可设 `max_result_chars=MAX_RESULT_CHARS_FULL_AGENT`。

---

### W13：Skill 声明的工具如何加载？

**看：** L50–70 `_load_declared_skill_tool`

**答：** 按 tool_name 硬编码 import（如 `generate_html_ppt` → html_ppt_tool；excel 系列 → get_excel_analysis_tools）— **尚未完全 registry 化**。

---

## 5. `application/agent_executors/lightweight_qa_agent.py`

### W14：`_SKIP_KB` / SKIP_KB 协议是什么？

**看：** L276–289

**答：** 模型在检索 query 生成阶段输出 `SKIP_KB` → 内联逻辑**跳过** multi_kb/联网，直接纯 LLM 答（内部协议，非用户检索词）。

---

### W15：Lightweight 如何做 history 压缩？

**看：** L23–28 模块级 `_compress_cache` / `_compress_tasks`

**答：** 按 thread_id 异步压缩历史，**下一轮生效** — 与 Full 的 SummarizationMiddleware 不同实现路径。

---

### W16：`_clean_question` 去掉哪些注入块？

**看：** L259–273

**答：** runtime 时间标签、上传长文档提示、`<uploaded_document>` 块、ConfirmedResearchPlan 等 — 保证检索 query 干净。

---

### W17：Lightweight 写 checkpoint 是否仍用 `self._graph`？

**答：** 是 — 通过传入的 `CompiledStateGraph` + checkpointer 读写 messages，但**不**走 graph 的 tool 节点循环。

---

## 6. `core/agents/middlewares/context_pruning.py`

### W18：哪两个 tool 名会触发 State Elevation？

**看：** L125–167

**答：** `read_local_skill_file` → `loaded_skills`；`multi_kb_search` → `retrieved_evidence`。

---

### W19：占位符 `[Context Isolation: ...]` 的作用？

**答：** 替换原 ToolMessage，让 checkpoint history 变短，但 LLM 仍知道「检索已成功」；真实内容在 state + JIT。

---

### W20：JIT 注入的 XML 标签名？

**看：** L219+ 附近

**答：** `<retrieved_evidence_database>`、`<loaded_skills_definitions>`（及块内 ### KNOWLEDGE EVIDENCE BLOCK n）。

---

## 7. `core/tools/task_tool.py`

### W21：`deep-research` 子 Agent 禁止调用哪些工具？

**看：** L59–67 `_SUBAGENT_CONFIGS`

**答：** `disallowed_tools={"task", "ask_clarification", "save_deep_research_report"}` — 禁止嵌套、澄清、最终报告（主 Agent 负责）。

---

### W22：子 Agent 如何向主流程推事件？

**答：** `get_stream_writer()` 自定义 stream 事件（非 HTTP SSE，图内 custom 事件）。

---

## 8. `application/agent_executors/streaming_runtime.py` + `stream_handler.py`

### W23：StreamHandler 与 `AgentService` 的分界？

**答：**
- **AgentService**：Profile 加载、工作流解析、生命周期管理
- **StreamingRuntime**：config、budget、astream_events 循环、超时
- **StreamHandler**：LangGraph event → SSE JSON、thinking 剥离、tool 事件格式化

---

### W24：deep_research 模式 StreamHandler 构造参数有何不同？

**答：** `is_deep_research=True` → 影响超时预算、输出字符上限、事件映射策略。

---

## 9. `application/agent_workflows/` — CoordinatedAgentWorkflow

### W25：`CoordinatedAgentWorkflow` 如何实现深研编排？

**答：** 内置大纲生成 → 用户确认 → 并行子 Agent 检索 + 报告生成；由 WorkflowRegistry 解析 `deep_research` 时创建。不再使用独立的 `DeepResearchOrchestratorAgent` 类。

---

## 10. `api/routers/agent_runtime.py`（Agent API 层）

### W26：流结束前如何生成 follow_up_suggestions？

**答：** `FollowUpSuggestionService.generate()` ← `build_tool_summary()` ← 流中 `note_process_event(tool_end)`；yield `type: follow_up_suggestions`。

---

### W27：`confirmation_status` 与深研的关系？

**答：** 用户 confirmed / revised / cancelled 驱动 ResearchWorkflowState；confirmed 后 `run_deep_research_background`。

---

## 综合走读题

### W28：从 POST query 到第一次 multi_kb_search，full 模式经过哪些层？

**参考答案（简）：**

```
agent_runtime.py → AgentService → WorkflowRegistry → full
→ ProfiledAgentExecutor → ProfiledGraphBuilder → create_agent_graph
→ graph.astream_events → LLM 输出 tool_call
→ DanglingToolCall → ToolGuardrail allow
→ ToolErrorHandling 包裹执行 → multi_kb_search 闭包
→ execute_multi_kb_search → ToolMessage
→ ContextPruning after_agent 提升 evidence
→ 下一轮 before_model JIT → LLM
```

---

### W29：同一请求若 WorkflowRegistry 解析 lightweight，上述链路有何不同？

**答：** 无 graph tool 循环；LightweightQAAgent 内联 `execute_multi_kb_search`；无 ToolGuardrail/LoopDetection；可能仍用 graph 读写 history。

---

### W30：若要在 factory 链加一层「日志 Middleware」，推荐怎么做？

**答：** 写 `AgentMiddleware` 子类 + `@Next(ToolErrorHandlingMiddleware)` 或 unanchored extra + `create_agent_graph(..., extra_middleware=[LogMw()])` — **不要**直接改 `_assemble_from_features` 顺序。

---

## 自测：30 题完成标准

| 得分 | 水平 |
|------|------|
| 24+ | 可独立改 Agent 路径 |
| 18–23 | 可排查线上 Agent 问题 |
| 12–17 | 需对照源码 |
| <12 | 先读 19 模块 L1–L2 |

---

## 11. 深度研究源码（W31–W40）

> 文件：`CoordinatedAgentWorkflow` · `task_tool.py` · `agent_runtime.py` · `registry.yaml`

### W31：`CoordinatedAgentWorkflow` 如何初始化深研流程？

**答：** WorkflowRegistry 解析 `deep_research` → `create_coordinated_workflow` → CoordinatedAgentWorkflow 内置大纲生成 → 用户确认 → 并行子 Agent。

---

### W32：深研 Orchestrator 过滤掉哪些工具？为什么？

**答：** 移除 `multi_kb_search`、`web_search`、`duckduckgo_search` — 主控 **禁止直接检索**，必须 `task` 委派 `deep-research` 子 Agent。

---

### W33：Orchestrator Prompt 里「同轮 2 个 task」在哪定义？

**看：** `_build_system_prompt` L30–46 — `<deep_research_orchestrator_hard_rules>`

**答：** 硬编码 append 到 base system prompt；要求 **一条 AI 消息内两个 task tool_calls**，两维度并行。

---

### W34：`deep-research` 子 Agent 的 `disallowed_tools`？

**看：** `task_tool.py` L67

**答：** `task`（防嵌套）、`ask_clarification`、`save_deep_research_report` — 报告只能主 Orchestrator 归档。

---

### W35：`run_deep_research_background` 如何选择域配置？

**答：** 通过 `agent_code` 加载对应 AgentProfile（YAML），Profile 决定 prompt_profile、tool_policy 等域配置。

---

### W36：`agent_runtime.py` 里 `confirmation_status=confirmed` 之后发生什么？

**答：** `run_deep_research_background(...)` 后台启动 → TaskManager 注册 → yield `background_task_started` / `research_confirmed` 类事件 → 前端改订 `task/{id}/stream`。

---

### W37：大纲生成与 CoordinatedAgentWorkflow 谁先执行？

**答：** **大纲先** — 首次 deep_research query 走 CoordinatedAgentWorkflow 的大纲生成阶段；**confirmed 后** 才并行子 Agent 多步研究。大纲生成是 CoordinatedAgentWorkflow 内置阶段。

---

### W38：深研报告存在哪？API 如何取？

**答：** ScopedStorage `users/{uid}/threads/{tid}/artifacts/` — `save_deep_research_report` 工具写入；GET `/api/v2/threads/{thread_id}/artifacts/{path}`。

---

### W39：取消深研任务查什么 flag？

**看：** `runner.py` — `check_cancel_flag_from_db`

**答：** `POST .../task/{id}/cancel` 设 DB/TaskManager cancel → runner 循环检测 → `ResearchWorkflowState.cancelled`。

---

### W40：综合 — 从 confirmed 到第一个 multi_kb_search 谁调用？

**参考答案：**

```
run_deep_research_background
  → CoordinatedAgentWorkflow 后台执行
  → LLM 输出 2× task(subagent_type=deep-research)
  → task_tool 起独立 loop + ToolAssembly
  → 子 Agent graph → multi_kb_search（非主 Agent 直接调）
```

**对比 W28：** Full 模式 multi_kb_search 由 **主 Agent** tool loop 调用；深研由 **子 Agent** 调用。

---

## 自测：40 题完成标准

| 得分 | 水平 |
|------|------|
| 32+ | 可改深研/Orchestrator 路径 |
| 24–31 | 可排查深研 + Full 问题 |
| 16–23 | 需对照源码 |
| <16 | 先读 19 L1–L2 + 25 专章 |
