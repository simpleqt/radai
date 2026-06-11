# 02 · 智能体系统

> 📊 **图表：** 完整 ASCII 图见 [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 4–8、**32**；正式文档 ASCII 见 [docs/docs_backend/agent_engine_module.md](../../docs/docs_backend/agent_engine_module.md)。  
> ⭐ **Agent 专题：** [19](./19_agent_architecture_implementation.md) · [20 模拟卷](./20_agent_mock_interview.md) · [21 Middleware](./21_middleware_chain_deep_dive.md) · [22 走读 W1–W40](./22_agent_source_walkthrough.md) · [23 Lightweight vs Full](./23_lightweight_vs_full.md) · [24 文档对照](./24_agent_engine_doc_mapping.md) · [25 深度研究](./25_deep_research_orchestrator.md)

---

## L1 初级 🟢

### Q1：用户发一条问答请求，Agent 侧大致走什么路径？

**一句话结论：** HTTP 请求经 agent-runtime API 进入 `AgentService.run_stream()`，加载 AgentProfile 后由 `WorkflowRegistry` 解析工作流类型，创建 `StandardAgentWorkflow`（lightweight/full）或 `CoordinatedAgentWorkflow`（deep_research/plan_solve/multi_agent_*），经 `ProfiledAgentExecutor` 构建 LangGraph 图，事件通过 `AgentEvent` + `SSEEncoder` 转成 SSE 推给前端。

**为什么问这个/背景：**
面试官想确认你是否理解「API 层 → AgentService → WorkflowRegistry → Executor → LangGraph → SSE」的端到端链路，而不是只知道某个 Agent 类名。这是排查延迟、断流、工具未触发等问题的基础地图。

**详细解释：**

```text
┌─────────────┐  POST /v2/agent-runtime/query   ┌──────────────────┐
│   前端/UI   │ ─────────────────────────────────► │ agent_runtime.py │
└─────────────┘  AgentRuntimeQueryRequest         └────────┬─────────┘
                                                            │
                                                            ▼
                                                   ┌─────────────────┐
                                                   │  AgentService   │
                                                   │  run_stream()   │
                                                   └────────┬────────┘
                                                            │
                                               ┌───────────┼───────────┐
                                               ▼           ▼           ▼
                                       StandardAgent  StandardAgent  CoordinatedAgent
                                       Workflow       Workflow       Workflow
                                       (lightweight)  (full)         (deep_research)
                                               │           │           │
                                               └───────────┼───────────┘
                                                            │
                                                            ▼
                                               ┌─────────────────────┐
                                               │ ProfiledAgentExecutor │
                                               │ ProfiledGraphBuilder  │
                                               └──────────┬──────────┘
                                                          │
                                                          ▼
                                               ┌─────────────────────┐
                                               │ create_agent_graph  │
                                               │ (core/agents)       │
                                               │ + Middleware 链   │
                                               └──────────┬──────────┘
                                                          │
                                                          ▼
                                               AgentEvent → SSEEncoder
                                               type: message.delta |
                                               workflow.step | error
```

| 阶段 | 职责 | 典型耗时敏感点 |
|------|------|----------------|
| AgentService | 加载 Profile、解析工作流、构建上下文 | O(1)，纯逻辑 + DB 查询 |
| WorkflowRegistry | 解析工作流类型、加载 provider | YAML 配置解析 |
| ProfiledGraphBuilder | 组装工具集、构建 LangGraph 图 | 模型初始化 + Skill 加载 |
| LangGraph | 多轮 tool-call 循环 | 主要延迟来源 |
| AgentEvent + SSEEncoder | 协议转换、事件标准化 | 与 token 流绑定 |
| Checkpointer | 每步持久化 thread 状态 | SQLite 写盘 |

**在本项目中的实现：**

- **API 入口：** `backend/api/routers/agent_runtime.py`，请求体含 `question`、`agent_code`、`thread_id`、`selected_skills`、`workflow`、`task_type` 等。
- **编排服务：** `backend/application/agent_service.py` — `AgentService.run_stream()`。
- **工作流注册：** `backend/config/workflows/registry.yaml` + `backend/application/agent_workflows/registry.py`。
- **Profile 加载：** `backend/application/agents/profile_loader.py` — `AgentProfileLoader`。
- **执行器：** `backend/application/agent_executors/profiled_executor.py` — `ProfiledAgentExecutor`。
- **图构建：** `backend/application/agent_executors/graph_builder.py` — `ProfiledGraphBuilder`。
- **建图：** `backend/core/agents/factory.py` — `create_agent_graph()`。
- **流式：** `backend/application/agent_executors/streaming_runtime.py` + `stream_handler.py`。
- **事件：** `backend/domain/agents/events.py` — `AgentEvent`。

**走一遍具体例子：**

用户在前端（未选任何 Skill）问：「电荷灵敏前置放大器的噪声来源有哪些？」

1. POST 到达 agent-runtime API，`agent_code=ne`，`selected_skills=[]`。
2. AgentService 加载 NE AgentProfile（`ne.yaml`）。
3. WorkflowRegistry 解析工作流：NE Profile 的 `default_workflow=lightweight`，无 Skill 选中，解析为 `lightweight`。
4. StandardAgentWorkflow 创建 ProfiledAgentExecutor（`lightweight_mode=True`）。
5. Lightweight 路径：LLM 生成检索词 → 并发 `execute_multi_kb_search` + 可选联网 → 将检索结果直接拼进 prompt → 单次流式生成回答。
6. 事件经 AgentEvent 标准化，SSEEncoder 转为 SSE：`workflow.step`（检索状态）→ `message.delta`（正文 token）。
7. Checkpointer 在 lightweight 路径仍可通过 graph 读写历史消息（多轮上下文）。

**常见误区：**

- ❌ 认为所有问答都走 LangGraph 多轮 tool-call 循环。  
  ✅ lightweight 路径绕过 Agent 图的工具循环，预检索后直接 LLM 生成。
- ❌ 认为每个域有独立的 Router 和 Agent 类。  
  ✅ 统一用 AgentService + AgentProfile + WorkflowRegistry，域差异通过 YAML 配置驱动。
- ❌ 以为 API 层直接调 RAG。  
  ✅ Agent 路径通过 `multi_kb_search` 工具或 Lightweight 内联调用 `execute_multi_kb_search`。

**延伸追问 (L2/L3)：**

- **L2：** lightweight 与 full 模式下 SSE 事件类型有何差异？thinking token 如何剥离？
- **L3：** 若要在 WorkflowRegistry 层增加第四种工作流（如「仅联网」），需改哪些文件且如何避免破坏 Checkpointer 协议？

---

### Q2：WorkflowRegistry 如何决定走 lightweight / full / deep_research？

**一句话结论：** `WorkflowRegistry.resolve_name()` 优先级为：显式 `request.workflow` > Skill `preferred_workflow` > 有 Skill 则 `full` > `profile.default_workflow` > `lightweight`；工作流定义在 `registry.yaml` 中。

**为什么问这个/背景：**
工作流解析规则是产品行为与工程性能的交界点。面试官考察你是否理解「显式/隐式工作流选择」的设计动机，以及它与前端 `selected_skills` 字段的耦合关系。

**详细解释：**

```text
WorkflowRegistry.resolve_name() 决策流程
        │
        ▼
   request.workflow 显式指定 ? ────是──► 使用该工作流
        │
       否
        ▼
 Skill preferred_workflow ? ────是──► 使用 Skill 声明的工作流
        │
       否
        ▼
 selected_skills 有非空项 ?
        │
   ┌────┴────┐
  否        是
   │         │
   ▼         ▼
profile.default    full
_workflow          (Skill 工具链)
        │
        ▼
registry.yaml 查找 provider:
  lightweight/full    → create_standard_workflow
  deep_research/plan_solve/multi_agent_* → create_coordinated_workflow
```



要点：

- WorkflowRegistry 层：`resolve_name()` 根据请求和 Profile 决定工作流
- YAML `registry.yaml` 定义 6 种工作流：lightweight、full、deep_research、plan_solve、multi_agent_parallel、multi_agent_alternating
- lightweight/full 使用 `StandardAgentWorkflow`，其余使用 `CoordinatedAgentWorkflow`
- `AgentProfile.allows_workflow()` 检查 Profile 是否允许该工作流

**在本项目中的实现：**

- 工作流注册表：`backend/config/workflows/registry.yaml`
- 工作流解析：`backend/application/agent_workflows/registry.py` — `WorkflowRegistry.resolve_name()`
- Profile 工作流权限：`backend/domain/agents/profile.py` — `AgentProfile.allows_workflow()`
- Skill 工作流偏好：`backend/application/skills/manifest_loader.py` — `SkillManifestLoader`

**走一遍具体例子：**

用户：`workflow=deep_research`，`agent_code=ne`

1. `AgentRuntimeQueryRequest.workflow="deep_research"` → `resolve_workflow()` 返回 `"deep_research"`。
2. WorkflowRegistry 查找 `registry.yaml`：`deep_research` 的 provider 是 `create_coordinated_workflow`。
3. CoordinatedAgentWorkflow 处理：大纲生成 → 用户确认 → 并行子 Agent 检索 + 报告生成。
4. SSE `process` 事件报告子任务进度，`save_deep_research_report_tool` 生成报告 artifact。

**常见误区：**

- ❌ 认为 deep_research 走 lightweight。  
  ✅ 是独立的 CoordinatedAgentWorkflow，能力最重。
- ❌ 认为工作流由 Router 硬编码决定。  
  ✅ 由 YAML `registry.yaml` 配置驱动，新增工作流只需加 YAML 条目。

**延伸追问 (L2/L3)：**

- **L2：** 用户取消 running 任务时，checkpoint 与 MySQL 任务表如何一致？
- **L3：** 如何设计 Skill manifest 的 `preferred_workflow` 字段并与 WorkflowRegistry 联动？

---

### Q8：子智能体是如何执行的？为什么用独立线程池？

**一句话结论：** 子 Agent 通过 `task_tool`（`core/tools/task_tool.py`）派发，在独立线程 + 持久 event loop 中跑完整 LangGraph 图，通过 `stream_writer` 向主流程推送事件；`SubagentLimitMiddleware` 限制并发数；隔离是为避免在主 asyncio loop 中嵌套阻塞另一张 Agent 图导致死锁。

**为什么问这个/背景：**
这是 LangGraph + asyncio 架构的经典坑。面试官想看你是否理解「为什么不能 await 子 graph 在主 loop」以及随之而来的 MySQL NullPool 约束。

**详细解释：**

```text
┌───────────────────────────────────────────────────────────────┐
│                   主 Agent — asyncio Loop A                    │
│ LangGraph 主图  │  stream_writer 收事件                            │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│              SubagentExecutor — Thread + Loop B               │
│ 子 LangGraph 完整图  │  (async MySQL NullPool)                    │
└───────────────────────────────────────────────────────────────┘

连接:
  LangGraph 主图 ──(task 工具)──► SubagentExecutor — Thread + Loop B
  子 LangGraph 完整图 ──(自定义 stream 事件)──► stream_writer 收事件
  子 LangGraph 完整图 ──────► (async MySQL NullPool)

```

```
主 Agent (asyncio loop A)
    │
    │ 调用 task 工具
    ▼
SubagentExecutor
    │
    ├── 新建/复用 Thread + Loop B
    ├── 在 Loop B 中 run 子 LangGraph
    └── stream_writer → 主 loop 收自定义事件
```

| 若不用独立 loop | 后果 |
|-----------------|------|
| 主 loop 同步等待子 graph | 阻塞其他请求处理 |
| 嵌套 run_until_complete | 死锁 / "This event loop is already running" |
| 共享 async DB 连接池 | 跨 loop 复用连接 → 运行时错误 |

**在本项目中的实现：**

- 执行器：`backend/core/tools/task_tool.py` — 主 Agent 通过 `task` 工具派发子 Agent，内部 `create_agent_graph` 构建子图
- `multi_kb_search_tool` 检测 `threading.current_thread().name.startswith("subagent-exec-")` 时使用更长超时（`SUBAGENT_KB_SEARCH_TIMEOUT`）
- MySQL 需 **NullPool**（见 05 模块 Q4）：子线程不能安全复用主 loop 创建的连接

**走一遍具体例子：**

主 Agent 收到：「分别调研 A、B 两种探测器并对比」

1. 主 Agent 调用 `task` 工具两次，SubagentLimitMiddleware 限制并发 ≤3。
2. 每个子 Agent 在 `subagent-exec-*` 线程检索 KB，`multi_kb_search` 去重逻辑（`_INFLIGHT_MULTI_KB_TASKS`）防止同线程重复检索。
3. 子 Agent 结果汇总回主 Agent messages。

**常见误区：**

- ❌ 认为子 Agent 是同一 graph 的子节点。  
  ✅ 是**独立编译图**，独立 thread/loop。
- ❌ 在子线程直接用主 loop 的 SQLAlchemy session。  
  ✅ 必须 NullPool 或线程本地连接。

**延伸追问 (L2/L3)：**

- **L2：** `stream_writer` 自定义事件如何被 `StreamingRuntime` / `SSEEncoder` 识别？
- **L3：** 若改为进程级子 Agent（multiprocessing），需重新设计哪些共享状态？

---

### Q9：`ToolAssemblyPipeline` 如何按 Skill 组装工具？

**一句话结论：** `ToolAssemblyPipeline.assemble()` 以 `get_available_tools` 为基底，按 `kb_ids`、`use_search`、`selected_skills`、`task_type` 动态增删工具，并通过 Skill 声明注入 `generate_html_ppt`、`academic_search`、Excel 工具等。

**为什么问这个/背景：**
Skill 驱动的工具注入是本项目「可扩展而不改 Agent 核心代码」的关键。考察你对**白名单、Skill 运行时状态、声明式 tool_names** 的理解。

**详细解释：**

装配流程：

```
get_available_tools(model, subagent_enabled)
        │
        ▼
± multi_kb_search（kb_ids 非空时，包装 execute_multi_kb_search）
± web_search（use_search）
± native_skill_tools（read_local_skill_file 等）
± declared skill tools（按 skill.tool_names 加载）
± chat_file_search（thread 有长文档索引时）
− sandbox tools（bash/write_file 等，生产移除）
− ask_clarification（deep_research 时）
        │
        ▼
ToolGuardrail Allowlist = 最终 tool names
```

关键输入：

| 参数 | 影响 |
|------|------|
| `selected_skills` | 决定 native tools + 声明工具；含 `nuclear-electronics` 时 multi_kb 需先 `has_loaded_skill` |
| `kb_ids` | 空则移除 multi_kb_search |
| `lightweight_mode` | 直接返回 `[]` |
| `task_type=deep_research` | 禁用 clarification |

**在本项目中的实现：**

- 源码：`backend/application/tools/tool_assembly_pipeline.py` — `ToolAssemblyPipeline.assemble()`
- 策略驱动：`PolicyToolAssembler` 根据 AgentProfile 的 `tool_policy` 字段选择工具集

```python
# application/tools/tool_assembly_pipeline.py
class ToolAssemblyPipeline:
    def assemble(self, profile: AgentProfile, request: AgentRequest, ...):
        # 以 get_available_tools 为基底
        # 按 kb_ids、use_search、selected_skills、task_type 动态增删
        # PolicyToolAssembler 按 tool_policy 策略过滤
        ...
```

**走一遍具体例子：**

`selected_skills=["html-ppt", "nuclear-electronics"]`, `kb_ids=[NE_KB, PAPER_KB]`, `use_search=True`：

1. 注入 `multi_kb_search`（kb_ids  scoped）、`web_search`。
2. 注入 `read_local_skill_file`、`generate_html_ppt`（Skill 声明）。
3. 首次调 multi_kb 被拦截 → Agent 必须先读 Skill 文件 → ContextPruning 提升 Skill 内容 → 再检索。

**常见误区：**

- ❌ 认为 Skill 会自动启动独立 workflow 图。  
  ✅ 当前架构是 **Prompt + 工具** 生效（规划中的 manifest 路由尚未落地）。
- ❌ 认为 ToolAssembly 在 WorkflowRegistry 层执行。  
  ✅ 在 `ProfiledGraphBuilder.build()` 构建图时执行。

**延伸追问 (L2/L3)：**

- **L2：** `_validate_skill_tool_declarations` 缺失工具时为何只 warn 不 fail？
- **L3：** 如何设计 Skill manifest 的 `preferred_workflow` 字段并与 WorkflowRegistry 联动？

---

## L3 高级 🔴

### Q10：WorkflowRegistry 的工作流解析策略相比旧 Router 隐式路由有什么优势？

**一句话结论：** 新架构通过 `WorkflowRegistry.resolve_name()` 实现显式、YAML 驱动的工作流选择，优先级链路清晰（显式 > Skill > Profile > 默认），相比旧 Router 的隐式「有无 Skill」判断，提升了可配置性、可审计性和新工作流接入速度。

**为什么问这个/背景：**
L3 题考察架构演进动机：从硬编码隐式路由到声明式工作流管理的权衡。

**详细解释：**

**新架构工作流解析（ASCII 架构图）：**

```text
WorkflowRegistry.resolve_name() 决策
        │
        ▼
 request.workflow 显式指定 ? ──是──► 使用该工作流
        │
       否
        ▼
 Skill preferred_workflow ? ──是──► 使用 Skill 声明的工作流
        │
       否
        ▼
 selected_skills 有非空项 ? ──是──► full
        │
       否
        ▼
 profile.default_workflow（YAML 配置）
        │
        ▼
 registry.yaml 查找 provider:
  lightweight/full    → StandardAgentWorkflow
  deep_research/...   → CoordinatedAgentWorkflow
```

**对比旧架构优势：**

| 维度 | 旧 Router 隐式路由 | 新 WorkflowRegistry |
|------|------|------|
| 可配置性 | 硬编码 `_resolve_mode()` | YAML `registry.yaml` 声明 |
| 新工作流接入 | 改 Python 代码 | 加 YAML 条目 + provider |
| 域差异 | 每个域独立 Router 类 | AgentProfile YAML 配置 |
| 可审计性 | 逻辑散在代码中 | 优先级链路清晰可读 |

**在本项目中的实现：**

- 解析器：`backend/application/agent_workflows/registry.py` — `WorkflowRegistry.resolve_name()`
- YAML 定义：`backend/config/workflows/registry.yaml` — 6 种工作流
- Profile 权限：`backend/domain/agents/profile.py` — `AgentProfile.allows_workflow()`

**走一遍具体例子：**

- NE 用户未选 Skill → `resolve_name()` 无显式/Skill → `ne.yaml` 的 `default_workflow=lightweight` → StandardAgentWorkflow(lightweight)。
- CSH 用户选「HTML PPT」Skill → `resolve_name()` 无显式 → Skill `preferred_workflow` 或 `selected_skills` 非空 → `full`。
- 用户显式传 `workflow=deep_research` → 直接命中最高优先级 → CoordinatedAgentWorkflow。

**常见误区：**

- ❌ 认为工作流解析仍依赖硬编码 Router。  
  ✅ 已改为 YAML 驱动的声明式解析，新增工作流只需加 `registry.yaml` 条目。
- ❌ 认为 lightweight 等于「无 KB」。  
  ✅ lightweight 仍可绑 `kb_ids` 并预检索。

**延伸追问 (L2/L3)：**

- **L2：** 前端如何引导「需要联网但不选 Skill」的用户？
- **L3：** 如何设计 Skill manifest 的 `preferred_workflow` 字段并与 WorkflowRegistry 联动？

---

### Q11：ContextPruning 的 JIT 注入会不会导致「模型看不到完整检索结果」？

**一句话结论：** **会**——这是刻意的 token 权衡；证据经 `_trim_retrieved_evidence` 和 RAG 层 MMR/PPR 预过滤后，只保留最近若干块且总字符 ≤8000，多跳推理可能丢失早期检索细节。

**为什么问这个/背景：**
连接 RAG、Agent、Middleware 三层的「信息损失」问题，考察 Lost in the Middle、JIT vs 全量 ToolMessage 的 engineering tradeoff。

**详细解释：**

**三层信息损失（ASCII 架构图）：**

```text
Layer 1: RAG Stage-2（MMR/PPR/Rerank）→ chunk 已过滤
        │
        ▼
Layer 2: multi_kb_search MAX_RESULT_CHARS（如 5000）→ 工具返回已截断
        │
        ▼
Layer 3: ContextPruning JIT（8000 chars, 2 blocks）→ 注入时再截断
        │
        ▼
模型可能看不到第 1 次检索的细节（刻意 token 权衡）

_trim_retrieved_evidence:
  ① 只保留最后 JIT_MAX_EVIDENCE_BLOCKS 条
  ② 从最新向前累加至 JIT_MAX_EVIDENCE_CHARS
  ③ 至少保留 1 块
```



损失发生在三层：

```
Layer 1: RAG Stage-2（MMR/PPR/Rerank）→ chunk 已过滤
Layer 2: multi_kb_search MAX_RESULT_CHARS（如 5000）→ 工具返回已截断
Layer 3: ContextPruning JIT（8000 chars, 2 blocks）→ 注入时再截断
```

裁剪算法（`_trim_retrieved_evidence`）：

1. 只保留最后 `JIT_MAX_EVIDENCE_BLOCKS` 条。
2. 从最新块向前累加，直到超过 `JIT_MAX_EVIDENCE_CHARS`。
3. 至少保留 1 块（避免全空）。

**缓解手段：**

| 手段 | 说明 |
|------|------|
| 调大 env | `JIT_MAX_EVIDENCE_CHARS` / `BLOCKS` |
| Agent Prompt | 要求合并检索后再回答；限制 multi_kb 调用次数 |
| Lightweight | 检索结果直接进 prompt，不经 ToolMessage 链（但仍受工具层字符预算约束） |
| 多轮检索 | Full Agent 可再次 `multi_kb_search`（受 `MULTI_KB_SEARCH_NORMAL_MAX_CALLS` 限制） |

**在本项目中的实现：**

```27:28:backend/core/agents/middlewares/context_pruning.py
JIT_MAX_EVIDENCE_BLOCKS = max(1, int(os.getenv("JIT_MAX_EVIDENCE_BLOCKS", "2")))
JIT_MAX_EVIDENCE_CHARS = max(2000, int(os.getenv("JIT_MAX_EVIDENCE_CHARS", "8000")))
```

- domain Profile 使用 `MMR_ONLY`，Stage-2 跳过重型 PPR，已在 RAG 层做相关性筛选
- `multi_kb_search` 另有 `TURN_MAX_EVIDENCE_CHARS` 轮级预算（`request_limits.py`）

**走一遍具体例子：**

Agent 连续 3 次检索各 4000 字：

1. `retrieved_evidence` 存 3 块；JIT 只取后 2 块。
2. 2 块共 8000 字刚好；第 1 次检索细节不在 JIT 中。
3. 若用户问题依赖第 1 次检索中的实体关系，模型可能幻觉或漏答 → 需 Agent 设计合并检索或用户追问。

**常见误区：**

- ❌ 认为 ToolMessage 占位符意味着证据丢失。  
  ✅ 证据在 state，JIT 重新注入（但可能被裁剪）。
- ❌ 认为调大 JIT 即可完美解决。  
  ✅ 与 model context window、成本线性 tradeoff。

**延伸追问 (L2/L3)：**

- **L2：** JIT 追加到 SystemMessage 与追加到 user message 对模型注意力有何差异？
- **L3：** 如何实现「证据摘要链」：每次检索后 LLM 压缩进 state，而非 raw chunk 堆叠？

---

### Q12：若要新增一个业务域（如「环境工程」），需要改哪些地方？

**一句话结论：** 最小集 = 新建 YAML AgentProfile（`config/agents/env.yaml`）+ Prompt 模板（`prompt_templates/`）+ KB 绑定 + `RetrievalProfile` 映射；AgentService、WorkflowRegistry、SSE 协议、Checkpointer 全部复用。

**为什么问这个/背景：**
L3 扩展性题，考察能否列出**触达面清单**而非「改一个文件就行」。

**详细解释：**

**新域接入清单（ASCII 架构图）：**

```text
新域「环境工程 env」接入清单
        │
        ├── AgentProfile   config/agents/env.yaml（domain/prompt_profile/tool_policy 等）
        ├── Prompt         application/agents/prompt_templates/env_*.py
        ├── KB 绑定        db/init_agent_bindings.py + retrieval_profile 映射
        ├── Skills         skills/public/env-*（可选）
        └── ToolPolicy     tool_policy 配置（可选 env_default 策略）

可复用（不必改）：AgentService · WorkflowRegistry · ProfiledAgentExecutor
                  create_agent_graph · Checkpointer · SSEEncoder · multi_kb_search
```

| 组件 | 是否必改 | 复用条件 |
|------|----------|----------|
| AgentService | 否 | 统一编排，Profile 驱动 |
| WorkflowRegistry | 否 | YAML registry.yaml 已有工作流 |
| SSEEncoder | 否 | 标准化事件协议 |
| multi_kb_search | 否* | *需配置新 kb_ids |
| ToolAssembly | 视 Skill | 新域专属 Skill 声明工具 |

**在本项目中的实现：**

- 现域范例：NE（`config/agents/ne.yaml`）、CSH（`config/agents/csh.yaml`）、Groundwater（`config/agents/groundwater.yaml`）
- Profile 加载：`backend/application/agents/profile_loader.py` — `AgentProfileLoader`
- Prompt 构建：`backend/application/agents/prompt_builder.py` — `PromptBuilder.build()` 按 `prompt_profile` 字段选择模板
- 域差异完全通过 YAML 配置驱动，无需新建 Agent 子类

**走一遍具体例子：**

新增「环境工程」KB，`kb_code=env_remediation_v1`：

1. 新建 `config/agents/env.yaml`：`domain: env-engineering, prompt_profile: env, tool_policy: env_default, default_workflow: lightweight`。
2. 新建 `prompt_templates/env_*.py`，编写 System Prompt。
3. `_KB_CODE_PREFIX_PROFILES` 加 `("env_remediation", "domain")`。
4. AgentService 自动加载 `env.yaml`，无需改编排代码。
5. 跑 recall@k + P95 验证 Profile 是否合适。

**常见误区：**

- ❌ 认为必须 fork 整个 Agent 服务。  
  ✅ 新 YAML Profile 即可，AgentService 统一编排。
- ❌ 忽略 guest 站与环境变量覆盖。  
  ✅ `RAG_RETRIEVAL_PROFILE` 可全局强制 Profile 做 A/B。

**延伸追问 (L2/L3)：**

- **L2：** YAML Profile 参数化 vs 独立 Agent 类，如何选？
- **L3：** 多域共享同一 WorkflowRegistry 时，如何避免 Skill 命名空间冲突？

---

### Q13：LangGraph 图是在哪里创建的？分层架构如何协作？

**一句话结论：** 生产路径统一走 `ProfiledGraphBuilder.build()` → `core/agents/factory.py` 的 `create_agent_graph()`；`ProfiledGraphBuilder` 负责组装模型、工具、Middleware，`create_agent_graph` 内部调用 `langchain.agents.create_agent` 构建 LangGraph 图。

**为什么问这个/背景：**
考察代码库分层与建图链路，避免面试者「改错层」或引入双轨建图。

**详细解释：**

```
┌───────────────────────────────────────┐
│  application/agent_executors/         │
│  ProfiledGraphBuilder.build()         │  ← 组装模型、工具、Middleware
└───────────────────┬───────────────────┘
                    │
                    ▼
┌───────────────────────────────────────┐
│  core/agents/factory.py               │
│  create_agent_graph()                 │  ← 统一建图入口
│  内部调 langchain.agents.create_agent │
└───────────────────┬───────────────────┘
                    │
          ┌───────┴───────┐
          ▼               ▼
  StandardAgent    CoordinatedAgent
  Workflow         Workflow
```

| 层级 | 职责 |
|------|------|
| **application/agent_executors** | `ProfiledGraphBuilder` 组装模型 + 工具 + Middleware + extra_middleware |
| **core/agents** | `create_agent_graph`、`RuntimeFeatures`、`ThreadState` |
| **domain/agents** | `AgentProfile`、`AgentRequest`、`AgentEvent` 协议定义 |

**在本项目中的实现：**

```python
# backend/core/agents/factory.py
def create_agent_graph(
    model: BaseChatModel,
    tools: list[BaseTool] | None = None,
    *,
    system_prompt: str | None = None,
    middleware: list[AgentMiddleware] | None = None,
    features: RuntimeFeatures | None = None,
    extra_middleware: list[AgentMiddleware] | None = None,
    ...
) -> CompiledStateGraph:
```

- `create_agent_graph` 内部调用 `langchain.agents.create_agent`，按 features 组装 middleware
- **无全局单例**：每请求/每 Agent 实例构建图，checkpointer 注入实现会话持久化
- ProfiledGraphBuilder：`backend/application/agent_executors/graph_builder.py` — `build()` 组装各组件

**走一遍具体例子：**

启动 Full Agent：

1. `ProfiledGraphBuilder.build(profile, request, context)`。
2. `create_chat_model` + `ToolAssemblyPipeline.assemble()`。
3. `create_agent_graph(model, tools, features, extra_middleware=[ContextPruningMiddleware], checkpointer=...)`。
4. 返回 `CompiledStateGraph`。

**常见误区：**

- ❌ 认为每个域有独立建图逻辑。  
  ✅ 统一由 `ProfiledGraphBuilder` + `create_agent_graph` 处理，域差异通过 Profile 配置。
- ❌ 认为图可全局复用无 checkpointer。  
  ✅ 多用户并发需 per-request config + thread_id + checkpointer。

**延伸追问 (L2/L3)：**

- **L2：** `RuntimeFeatures` 与 `middleware` 全量接管的互斥约束是什么？
- **L3：** 如何将建图逻辑抽成测试友好的 pure function 并 mock LLM/tools？

---

## L2/L3 补充 🟡🔴

### Q14：`ThreadState` 有哪些扩展字段？Reducer 做什么？

**一句话结论：** `ThreadState` 在标准 `AgentState` 上扩展 `loaded_skills`、`retrieved_evidence`、`artifacts` 等；**Reducer** 负责 checkpoint 合并时**增量去重**，是 ContextPruning 的数据基础。

#### 详细解释

```text
┌────────────┐
│ TS         │
└────────────┘

  TS ──────────────► messages

  messages ──────────────► loaded_skills dict

  loaded_skills dict ──────────────► retrieved_evidence list

  retrieved_evidence list ──────────────► artifacts list

  artifacts list ──────────────► ContextPruning

  ContextPruning ──────────────► before_model

  before_model ──────────────► Checkpointer merge

  ContextPruning ──(写入)──► loaded_skills dict
  ContextPruning ──(写入)──► retrieved_evidence list
  before_model ──(读取)──► loaded_skills dict
  before_model ──(读取)──► retrieved_evidence list
  Checkpointer merge ──(merge_loaded_skills)──► loaded_skills dict

```

| 字段 | Reducer | 含义 |
|------|---------|------|
| `loaded_skills` | dict 合并覆盖 | Skill 文件内容 |
| `retrieved_evidence` | list 追加 | RAG 检索块 |
| `artifacts` | 去重 list | 报告/PPT 等工件 ID |

#### 在本项目中的实现

- `backend/core/agents/state.py` — `merge_loaded_skills`、`merge_retrieved_evidence`
- 文档：`docs/docs_backend/agent_engine_module.md`

---

### Q15：`intent_mode` 意图识别还存在吗？

**一句话结论：** **已移除**。旧架构的 `intent_mode` 意图识别机制已被 `WorkflowRegistry.resolve_name()` 替代，工作流类型现在由显式参数、Skill 声明、Profile 默认值等优先级链路决定，不再依赖独立的意图分类步骤。

#### 旧架构回顾

旧版在 `backend/api/routers/agent_runtime.py` 中曾有 `_normalize_intent_payload()`，现已由 `WorkflowRegistry.resolve_name()` 替代。

#### 新架构替代方案

| 旧 intent | 新架构等价 |
|-----------|-------------|
| `normal_qa` | WorkflowRegistry → `lightweight` 或 `full` |
| `deep_research` | 显式 `request.workflow="deep_research"` |
| `clarification_needed` | ClarificationMiddleware 在 full 模式下自动触发 |

#### 常见误区

- ❌ 以为还需要意图分类步骤。  
  ✅ 工作流解析已替代意图识别，更简洁且可配置。

---

### Q16：`ask_clarification` / ClarificationMiddleware 何时触发？

**一句话结论：** Full 工作流且非 deep_research 时 `RuntimeFeatures(clarification=True)`，模型可调用澄清工具；SSE 会发 `tool_end` + `clarification` 元数据供前端展示追问 UI。

#### 条件（ProfiledGraphBuilder 构建时）

- `clarification=True` 当 `workflow != "deep_research"` 且 `workflow != "lightweight"` 时设置
- ClarificationMiddleware 由 `factory.py::_assemble_from_features()` 按 `features.clarification` 注入

#### 走一遍具体例子

用户问「帮我分析实验数据」但未上传文件 → Agent 调 `ask_clarification` → 前端展示「请上传 Excel 或说明数据来源」。

---

### Q17：Deep Research 的大纲生成阶段如何实现？

**一句话结论：** 深研**第一阶段**由 `CoordinatedAgentWorkflow` 内置大纲生成流程：先流式生成研究大纲（SSE `draft_chunk` 事件），用户确认后才交后台执行，避免直接跑长任务。

```text
┌──────────┐ ┌─────────────────────┐ ┌──────────────────┐ ┌──────────────┐
│   用户    │ │ CoordinatedAgent    │ │  大纲生成阶段    │ │  并行执行    │
└──────────┘ │ Workflow              │ │ (draft_chunk)    │ │ (子 Agent)  │
└─────────────────────┘ └──────────────────┘ └──────────────┘
```

#### 在本项目中的实现

- 工作流：`backend/application/agent_workflows/` — `CoordinatedAgentWorkflow`
- 深研配置：`backend/config/workflows/registry.yaml` 中 `deep_research` 条目
- SSE type：`draft_chunk`（见 `sse_events_module.md`）

---

### Q18：如何防止 Agent 工具调用死循环？

**一句话结论：** **LoopDetectionMiddleware** 检测重复 tool_call 模式；配合 **SubagentLimitMiddleware** 限制子 Agent 次数；**request_limits** 限制单工具频率。全部由 `core/agents/factory.py` 统一组装。

| 机制 | 层级 |
|------|------|
| LoopDetection | 相同 tool+args 重复 N 次 → 中断或警告 |
| SubagentLimit | 子任务并发/总数上限 |
| ToolGuardrail deny | 非法工具直接 error ToolMessage |

#### 延伸追问

- 模型反复调用被 deny 的 web_search 是否算 loop？如何 UX 提示用户开联网？
