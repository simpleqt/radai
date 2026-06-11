# 24 · agent_engine_module 文档逐节对照刷题

> 按 [`docs/docs_backend/agent_engine_module.md`](../../docs/docs_backend/agent_engine_module.md) **章节顺序** 18 题：文档说了什么 → 源码在哪 → 面试怎么答。  
> 配合 [19](./19_agent_architecture_implementation.md) · [22 走读](./22_agent_source_walkthrough.md) 使用。

---

## 使用方式

```text
┌────────────────────────┐
│ agent_engine_module.md │
└────────────────────────┘

  agent_engine_module.md ──────────────► Q

  Q ──────────────► SRC

  SRC ──────────────► 口述 1 min

```

**模拟面试口令：** 「请对照你们 Agent Engine 文档，从请求进来到 SSE 出去讲一遍。」

---

## § 端到端请求路径（D1–D3）

### D1：文档 ASCII 总览与 02-Q1 的关系

**一句话结论：** 同构 — POST `/api/v2/agent-runtime/query` → AgentService → WorkflowRegistry → 三模式 → 执行 → SSE → SQLite checkpoint。

**在本项目中的实现：**

| 文档框 | 源码 |
|--------|------|
| `api/routers/agent_runtime.py` | HTTP 入口、StreamingResponse |
| `application/agent_workflows/registry.py` | `WorkflowRegistry.resolve_name()` |
| `application/agent_executors/` | ProfiledAgentExecutor / LightweightQAAgent |
| `checkpoints.db` | AsyncSqliteSaver |

**面试答法（60 秒）：** 「前端 POST 带 thread_id → AgentService 加载 Profile → WorkflowRegistry 解析工作流 → lightweight 走 LightweightQAAgent，full 走 ProfiledAgentExecutor + LangGraph，deep 走 CoordinatedAgentWorkflow → 统一 SSE 字段 → checkpoint 按 `u_user__t_thread` 隔离。」

**延伸：** 访客 `:8083` 与主 API checkpoint **物理分库**。

---

### D2：路由决策树三分支的精确条件

**一句话结论：** ① `deep_research` 显式工作流 → ② **无**有效 `selected_skills` → lightweight → ③ 否则 full。

**源码锚点：** `application/agent_workflows/registry.py`；文档 L66–86 决策树与代码一致。

**常见误区：** ❌ `use_search=false` 走 lightweight → ✅ 与 search 无关，看 **Skill** 或 **Profile `default_workflow`**。

**走一遍例子：** `{ "question": "你好", "selected_skills": [] }` → lightweight。

---

### D3：FullFeature 与 Lightweight 委托关系（文档 L88–102）

**一句话结论：** **不是** API 层直接 new Lightweight — 而是 `StandardAgentWorkflow(lightweight_mode=True)` 内部 **委托** `LightweightQAAgent`。

**对照图（文档已有）：**

```
StandardAgentWorkflow
  ├── lightweight_mode=False → ProfiledAgentExecutor → ProfiledGraphBuilder → LangGraph + ToolAssembly + ContextPruning + Handler
  └── lightweight_mode=True  → LightweightQAAgent（内联 KB + 单次 astream）
```

**源码：** `application/agent_executors/profiled_executor.py`、`application/agent_executors/lightweight_qa_agent.py`。

---

## § 核心引擎 architecture（D4–D7）

### D4：`create_agent_graph()` 的设计意图

**一句话结论：** **声明式建图** — 参数驱动、无 legacy 单例；支持多租户/多专家并行。

**文档章节：** 「Agent 状态图工厂」  
**源码：** `backend/core/agents/factory.py` — `create_agent_graph()`、`RuntimeFeatures`、`_assemble_from_features`。

**面试点：** 修改 Middleware 链应走 `extra_middleware` + `@Next/@Prev`，**非**直接改 factory 顺序（文档 IMPORTANT 提示）。

---

### D5：`ThreadState` 扩展字段

**一句话结论：** 在标准 `messages` 外增加 **`loaded_skills`**、**`retrieved_evidence`**，用 **Reducer** 做字典级去重合并，支撑 ContextPruning JIT。

**源码：** `backend/core/agents/state.py`。

**延伸：** 为何不用把 evidence 永远留在 ToolMessage？— permanent history 膨胀 + 针尖检索能力下降。

---

### D6：工厂 8 层 Middleware 顺序

**一句话结论：** Dangling → Guardrail → ToolError → Summarization → ViewImage → SubagentLimit → LoopDetection → **Clarification（最后）**。

**文档表格：** agent_engine_module.md L126–135  
**详题：** [21_middleware_chain_deep_dive.md](./21_middleware_chain_deep_dive.md) Q1–Q15

**Clarification 为何最后：** 拦截 `ask_clarification` 后 **跳出图** 等用户 — 必须在其他 wrap 完成之后。

---

### D7：`ContextPruningMiddleware` 不在工厂默认链

**一句话结论：** 由 **ProfiledGraphBuilder** 经 **`extra_middleware`** 注入；钩子 **`after_agent` + `before_model`**。

**文档亮点 §3.1 / §3.2：** State Elevation + JIT Assembly  
**源码：** `context_pruning.py`

```text
(empty sequence diagram)

```

---

## § 双通道路由（D8–D9）

### D8：域差异如何通过配置驱动？

**一句话结论：** **逻辑同构**（统一 AgentService）；差异在 **AgentProfile YAML**（`config/agents/ne.yaml` / `csh.yaml`）、**Prompt 模板**、**默认 kb_ids**、**深研工作流配置**。

**源码：** `config/agents/` YAML 配置；`application/agents/prompt_builder.py`；`application/agents/profile_loader.py`。

**面试一句：** 「域差异完全通过 YAML Profile 驱动，无需独立 Agent 子类。」

---

### D9：`prompt_profile` 影响范围

**一句话结论：** Lightweight 模板、文件检索前缀、系统 Prompt 构建（`PromptBuilder.build()` 按 `prompt_profile` 字段选择模板）。

**文档：** L211–212  
**例子：** CSH lightweight 用 `prompt_templates/csh_*` 模板。

---

## § 领域 FullFeature Agent（D10–D11）

### D10：NE AgentProfile 预设与工具

**一句话结论：** NE Profile（`config/agents/ne.yaml`）绑定核电子学 **双 KB**；工具经 ToolAssemblyPipeline — `multi_kb_search`、`web_search`、`task`（subagent 开启时）。

**文档 § 领域专家** L222–225  
**源码：** `config/agents/ne.yaml` + `application/tools/tool_assembly_pipeline.py`。

---

### D11：CSH 深研五步工作流

**一句话结论：** 大纲阶段 → 前端确认/修改 → `run_deep_research_background` → TaskManager 状态 → `deep_research_report_tool` 成报告；由 `CoordinatedAgentWorkflow` 实现。

**文档：** L231–236  
**延伸：** 与 Full 的 `task` 子 Agent **不同层** — 深研是 CoordinatedAgentWorkflow 内置流程。

---

## § 访客 / 工具 / 持久化（D12–D16）

### D12：访客端 Lightweight 强制策略

**一句话结论：** **强制** `lightweight_mode=True`、`thinking_enabled=False`；checkpoint `guest_checkpoints.db`；thread_key `g_{device}__t_{thread}`。

**文档 § 访客** L240–248  
**限制：** 拒绝 deep_research / subagent（文档排障表 L331）。

---

### D13：`multi_kb_search` RRF 融合（文档 §5）

**一句话结论：** 多库 `asyncio.gather` → 单库 Stage-1/2 → **RRF** `score = Σ 1/(k+rank)` → XML 上下文返回。

**源码：** `application/tools/multi_kb_search_tool.py`  
**环境变量：** `MULTI_KB_SEARCH_MAX_RESULT_CHARS`、`MULTI_KB_PATH_A_QUERY_ONLY` 等（文档 L270）。

**Lightweight：** 进程内直调 `retrieve_context()` / `execute_multi_kb_search`，不走 HTTP。

---

### D14：Lightweight 时 ToolAssembly 返回空

**一句话结论：** 文档 L290 — `assemble()` **return []**；检索由 LightweightQAAgent 内部完成。

**对照：** [23 Q6](./23_lightweight_vs_full.md)

---

### D15：Checkpoint 与 MySQL 会话分工

**一句话结论：** **SQLite** 存 Graph 全状态；**MySQL** `conversations` 存标题/元数据；二者通过 thread_id 关联。

**文档 §6** L305–309  
**延伸：** 生产多 worker 时 SQLite 瓶颈 — 文档演进表建议 Postgres/Redis。

---

### D16：MCP 与 Agent Engine 边界

**一句话结论：** MCP `/mcp/sse` 暴露 **`query_knowledge_base`** 等工具 — **独立入口**，非 v2 Agent 图内工具；可选独立进程 `mcp_server`。

**文档 §7** L313–318  
**面试：** MCP 检索 ≠ Agent 内 `multi_kb_search` tool loop，但可共用 RAG Service。

---

## § 排障与演进（D17–D18）

### D17：文档排障表 ↔ 19-Q25

**一句话结论：** 六条现象与源码入口一一对应 — 慢（mode）、无工具（kb_ids/lightweight）、SSE 无 chunk（流式/超时）、上下文爆（ContextPruning）、访客 400（设计限制）、串会话（thread_key）。

**文档表格：** L325–332  
**代码入口行：** `agent_runtime.py` · `agent_service.py` · `stream_handler.py`

**练习：** 给定现象「多轮后 token 爆」，口述查 **是否 full + ContextPruning 注入**。

---

### D18：文档「后续开发方向」如何答架构题

**一句话结论：** 六条演进 — Swarm 扩展、ReflectionMiddleware、OpenTelemetry、Skill 热更新、SSE Abort、**Postgres checkpointer**。

**文档表格：** L338–347  
**面试 L3：** 任选一行讲 **现状痛点 + 建议方案 + 迁移风险**。

**示例答法（Checkpointer）：** 「现 AsyncSqliteSaver 单文件写盘，多 uvicorn worker 会锁竞争；Agent 图有状态必须 sticky session 或外置 PG checkpointer，需评估 LangGraph 官方 Postgres 适配与 thread 迁移。」

---

## 附录：文档章节 → 题库交叉索引

| agent_engine 章节 | 本题 | 扩展模块 |
|-------------------|------|----------|
| 端到端 ASCII | D1–D3 | 02-Q1, 04-Q2 |
| core/agents 工厂 | D4–D7 | 19-Q1–Q6, 21 全文 |
| 双通道路由 | D8–D9 | 23 Q1–Q2 |
| 领域 Agent | D10–D11 | 19-Q20, 02-Q12 |
| 访客 | D12 | 16-Q*, 05-Q* |
| multi_kb / 工具 | D13–D14 | 03-Q*, 06-Q* |
| 持久化 / MCP | D15–D16 | 05-Q11, 17-Q* |
| 排障 / 演进 | D17–D18 | 19-Q24–Q25, 08-Q* |

**30 分钟刷法：** D1→D3 口述链路 → D6→D7 Middleware → D13→D14 工具差异 → D17 排障表背熟。
