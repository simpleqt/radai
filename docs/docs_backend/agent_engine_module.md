# Agent Engine (智能体引擎) 模块详解

> **最后同步**：2026-06-10
> **现状说明**：统一入口为 `/api/v2/agent-runtime/*`。`deep_research` 已迁移到 `CoordinatedAgentWorkflow` 的 plan/solve 架构，旧草案确认、后台 research task 和领域 orchestrator 已移除。
> **历史参考**：若需查看旧 Router / FullFeatureAgent 演进，请参考 [../agent_architecture_redesign.md](../agent_architecture_redesign.md)。

智能体引擎是系统的“决策与编排大脑”。当前已经完成从“按领域复制 Agent 类”到“Profile + Workflow + Executor + Standard Event”平台模型的迁移。

### 端到端请求路径（ASCII 总览）

与 [plans/qa/02_agent_system.md](../../plans/qa/02_agent_system.md) Q1 同构，便于面试/排障时快速对照代码：

```text
┌─────────────┐  POST /api/v2/agent-runtime/query  ┌──────────────────────────┐
│ RadAI/CSH   │ ─────────────────────────────────► │ backend/api/routers/     │
│ 前端        │  AgentRuntimeQueryRequest          │ agent_runtime.py         │
└─────────────┘                                     └────────────┬─────────────┘
┌─────────────┐  POST /api/v2/guest/query                      │
│ Guest 前端  │ ───────────────────────────────────────────────┤
└─────────────┘                                                │
                          ┌─────────────────────────────────────┴──────────────────────┐
                          ▼                                                            ▼
                 AgentService.run_stream()                                  Guest/MCP edge adapter
                          │                                                            │
                          ▼                                                            │
                 RuntimeContextFactory                                                 │
                          │                                                            │
                          ▼                                                            │
                 WorkflowRegistry.resolve()                                            │
                          │                                                            │
         ┌────────────────┼─────────────────────────────────────────────────┐ │
         ▼                ▼                                                 ▼ │
   lightweight          full                    deep_research / plan_solve / multi_agent_* │
         │                │                                                 │ │
         ▼                ▼                                                 ▼ │
 StandardAgentWorkflow  StandardAgentWorkflow                    CoordinatedAgentWorkflow │
         │                │                                                 │
         ▼                ▼                                                 ▼
 ProfiledAgentExecutor  ProfiledAgentExecutor       planner -> evidence subagents -> writer
         │                │                                                 │
         └────────────────┴──────────────────┬──────────────────────────────┘
                                             ▼
                                  AgentStreamRuntime / report artifact
                                        │
                                        ▼
                             Standard AgentEvent SSE
                             message.delta | reasoning.delta
                             workflow.step | error
                             follow_up.created | plan.created
```

| 阶段 | 职责 | 典型耗时敏感点 |
|------|------|----------------|
| AgentService | 创建运行时上下文并分发 workflow | O(1)，纯编排 |
| WorkflowRegistry | 选 lightweight / full / deep_research / coordinated workflows | O(1)，纯逻辑 |
| ToolAssembly | 按 Profile + ToolPolicy + Skill 组装工具 | full / deep_research / coordinated 主要使用 |
| Coordinated workflow | 规划、检索、并行子智能体、汇总与报告归档 | 深研主要延迟来源 |
| StreamHandler | 标准事件提取、thinking 分流、artifact 事件发射 | 与 token 流绑定 |
| Checkpointer | 每步持久化 thread 状态 | SQLite / 后端存储写入 |

**工作流决策树**（`WorkflowRegistry`）：

```text
                    开始
                      │
                      ▼
        workflow 是否显式命中
        deep_research / plan_solve / multi_agent_* ?
                      │
            ┌─────────┴─────────┐
      YES(直接返回)            NO
            │                   │
            ▼                   ▼
    指定 workflow    selected_skills 中有非空字符串?
                              │
                    ┌─────────┴─────────┐
                   NO                  YES
                    │                   │
                    ▼                   ▼
              lightweight            full
```

**当前执行关系**：

```text
StandardAgentWorkflow
        │
        ├── workflow=full ─────────► ProfiledAgentExecutor
        │                              ├── ProfiledGraphBuilder
        │                              ├── ToolAssemblyPipeline
        │                              ├── PromptBuilder
        │                              └── AgentStreamRuntime
        │
        └── workflow=lightweight ──► ProfiledAgentExecutor
                                       └── 轻量模式配置化执行
```

---

## 🤖 核心智能体引擎架构 (`backend/core/agents/`)

### 1. 技术基础与设计模式

系统深度集成 **LangGraph**，将复杂的对话循环编排表达为状态图（State Graph），以在长程多步骤工具调用中保持逻辑一致性。

*   **Agent 状态图工厂** (`backend/core/agents/factory.py`):
    *   入口函数：`create_agent_graph()` 接受声明式的 Python 参数，基于不同的功能特性组装成统一的状态图。
    *   不支持 legacy 全局单例，采用纯参数驱动构造，从而支持多租户与多专家智能体的并行运行。
*   **运行时状态模型** (`backend/core/agents/state.py`):
    *   `ThreadState` 扩展了标准 `AgentState`，是全链路的数据总线。
    *   核心字段包括：`messages` (消息列表), `loaded_skills` (已加载技能), `retrieved_evidence` (已召回的RAG参考资料) 等。
    *   内置的 `loaded_skills` 和 `retrieved_evidence` 使用自定义的 Reducer 进行字典级去重和增量合并，为 JIT (Just-In-Time) 组装奠定了数据基石。

---

### 2. 核心中间件链 (Middleware Chain)

| 顺序 | 中间件 | 启用条件 | 职责 |
| :--- | :--- | :--- | :--- |
| **0** | `DanglingToolCallMiddleware` | 始终挂载 | 清理悬挂的工具调用消息，防止图状态不一致。 |
| **1** | `ToolGuardrailMiddleware` | 始终挂载 | 白名单校验工具调用，拦截未授权工具。 |
| **2** | `ToolErrorHandlingMiddleware` | 始终挂载 | 捕获工具执行过程中的所有异常（如网络超时、参数错误），格式化为友好报错后返回给 LLM。 |
| **3** | `SummarizationMiddleware` | `summarization` | 监控上下文长度，超过阈值时压缩历史消息。 |
| **4** | `ViewImageMiddleware` | `vision` | 多模态视觉支持，智能过滤并把图片/图纸注入大模型的 System/User Prompt。 |
| **5** | `SubagentLimitMiddleware` | `subagent` | 控制最大子智能体（Sub-agent）深研任务的并发与深度上限，防止 token 风暴。 |
| **6** | `LoopDetectionMiddleware` | 始终挂载 | 智能检测工具死循环（例如反复调用 `multi_kb_search` 却得不到期望结果），超限自动中断并要求澄清。 |
| **7** | `ClarificationMiddleware` | `clarification` | 预留能力，当前主干智能体默认未启用。 |

> **附加中间件**：`ContextPruningMiddleware` 由平台执行器构图阶段注入，用于 JIT 状态提升与动态组装，不再依赖旧 FullFeatureAgent 外壳。

```text
0 DanglingToolCall ──► 1 ToolGuardrail ──► 2 ToolErrorHandling
        ──► 3 Summarization ──► 4 ViewImage ──► 5 SubagentLimit
        ──► 6 LoopDetection ──► 7 Clarification ──► Agent 节点
                                              │
                                              └── extra: ContextPruning
                                                  (after_agent / before_model)
```

> Skills 与工具对照见 [agent_platform_standardization_plan.md](../agent_platform_standardization_plan.md) §8–§9。  
> SSE 事件协议见 [agent_platform_standardization_plan.md](../agent_platform_standardization_plan.md) §11。

---

### 3. ContextPruningMiddleware: 状态提升与 JIT 动态组装 (核心亮点 ✨)

随着多轮对话与频繁的 RAG/Skill 工具调用，传统的 `messages` 列表会急速膨胀，导致上下文超出 LLM 限制或因“针尖检索”能力下降产生幻觉。

为此，系统引入了专门的 **`ContextPruningMiddleware`** (`backend/core/agents/middlewares/context_pruning.py`)，它实现了两个划时代的机制：

#### 3.1 状态提升 (State Elevation)
*   当智能体调用 `multi_kb_search`（知识库检索）或 `read_local_skill_file`（技能规则加载）等重型工具时，中间件在 **`after_agent`** 阶段进行拦截。
*   它提取 ToolMessage 中的冗长输出，分别提升存储至 Graph State 的 `retrieved_evidence`（召回资料库）和 `loaded_skills`（已加载技能定义）中。
*   随后，中间件将原始 ToolMessage 的 `content` 缩减为极简的摘要或 UUID 标识（如 `[RAG Evidence id: xxx loaded]`），彻底洗净了 permanent 消息历史。

#### 3.2 即时组装 (Just-In-Time JIT Assembly)
*   在下一次大模型调用（**`before_model`**）时，中间件在内存中实时提取当前的 `loaded_skills` 和 `retrieved_evidence`。
*   以标准 XML 格式组装为 `<loaded_skills_definitions>` 与 `<retrieved_evidence_database>` 段落。
*   将该段落动态贴合在主 System Prompt 之后，生成一个一次性的临时 SystemMessage 传给 LLM。
*   模型调用结束后，该临时消息直接销毁，不计入数据库。**这使多轮对话的 Token 消耗暴降 70% 以上，极大地提高了长文本下的模型注意力和回答精度！**

```text
用户消息 ──► Agent 图 ──► 重型工具 multi_kb_search
                              │
                              ▼
                    ContextPruning after_agent
                    截断 ToolMessage → retrieved_evidence
                              │
                              ▼
                    ContextPruning before_model
                    JIT 注入 evidence + skills
                              │
                              ▼
                         大模型 ──► 回答
```

---

## 🧭 工作流分流机制

在实际业务中，不同难度的提问如果全部交给重型专家图，会导致巨大的响应延迟与计算开销。系统在 `/api/v2` 下设计了双通道智能路由：

```text
                    ┌─────────────────┐
                    │ 用户输入 Query   │
                    └────────┬────────┘
                             ▼
                    ┌─────────────────┐
                    │ WorkflowRegistry │
                    └─┬───────┬───────┬┘
                      │       │       │
         无 skills 轻量│  有 selected_│ deep_research
                      │  skills     │
                      ▼       ▼       ▼
              lightweight   full   DeepResearch
                      │       │      Workflow
                      ▼       ▼         │
             ProfiledAgentExecutor      ▼
             (轻量配置)        ProfiledAgentExecutor
                      │       │         │
                      └───────┴─────────┘
                              │
                              ▼
                      Standard AgentEvent
```

### 1. 标准运行入口 (`backend/application/agent_service.py`)
*   `AgentService` 负责接收 `AgentRequest`，创建 `AgentRuntimeContext`，并交由 `WorkflowRegistry` 选择 `lightweight` / `full` / `deep_research` / `plan_solve` / `multi_agent_*`。
*   轻量和全功能都通过同一套 `StandardAgentWorkflow + ProfiledAgentExecutor` 执行，只是 profile/runtime features 不同。
*   Prompt 按领域由 `PromptBuilder` 组装，工具由 `ToolAssemblyPipeline` 按 Profile/ToolPolicy/Skill 注入。

### 2. 深度研究工作流 (`backend/application/agent_workflows/deep_research_workflow.py`)
*   深研模式由 `DeepResearchWorkflow` 统一处理，不再通过领域专属 orchestrator 类继承旧全功能 Agent。
*   草案、确认、后台任务、状态查询、取消和 SSE 都收敛到 `ResearchWorkflowService`、`ResearchTaskService` 与 `ResearchTaskRuntime`。

### 3. 协同工作流引擎 (`backend/application/agent_workflows/coordinated_agent_workflow.py`)
*   `plan_solve` / `deep_research`、`multi_agent_parallel`、`multi_agent_alternating` 已全面落地为平台通用调度编排核心，统一由 `WorkflowRegistry` 和 `AgentService` 进行动态路由解析。
*   **多智能体并发调度模型**：
    *   在 `plan_solve` / `deep_research` 下，利用 `asyncio.gather` 并行执行各研究分支的预检索与分析，提升效率。
    *   在 `multi_agent_parallel` 中，并发拉起 `domain_analyst`、`evidence_researcher` 与 `risk_reviewer` 三方子任务，再通过 `synthesizer` 集中处理冲突收敛。
    *   在 `multi_agent_alternating` 中，按照 `analyst` -> `critic` -> `finalizer` 顺序推进，使用共享上下文传递跨轮次数据。
*   **FastAPI 事件循环非阻塞保护 (`backend/application/blocking.py`)**：
    *   通过 `run_blocking` 将包含密集型 I/O 与图谱重度 CPU 运算的 `_execute_kb_search_blocking` 和 Docx 编译等任务分发给全局 `ApplicationBlockingExecutor` 线程池（核心线程数由 `AGENT_BLOCKING_MAX_WORKERS`，默认 8 控制）。
    *   利用独立工作线程在子 Loop 中以 `asyncio.run` 形式运行，有效避免了 FastAPI 主事件循环挂起（Delay）所产生的并发响应卡顿。
    *   本地工件注册重构为 async `register_local_artifact_for_thread`，剔除老旧的同步阻塞逻辑。
*   **心跳、超时与排队保护控制**：
    *   在任务周期中，每隔 `DEEP_RESEARCH_HEARTBEAT_SECONDS`（默认 10s）通过 `asyncio.shield` 轮询检查迭代器状态，自动向客户端推送包含已等待秒数的心跳保活事件（`task_progress`/`status`），避免网关层超时断连。
    *   强行监控并触发 `DEEP_RESEARCH_UNIT_IDLE_TIMEOUT_SECONDS`（空闲超时，默认 120s）与 `DEEP_RESEARCH_UNIT_TOTAL_TIMEOUT_SECONDS`（任务总执行时间上限，默认 300s）。
    *   深研任务总体并发数受 `DEEP_RESEARCH_MAX_CONCURRENT` 信号量（Semaphore）硬限制，超限则发送 `research_queued` 状态排队并隔离运行。
*   **预检索屏障同步 (`_AsyncTaskBarrier`)**：
    *   引入屏障同步类，协调所有 Researcher 分支，在多路 Web/RAG 检索均准备完毕后，才同步释放各分支的大模型分析协程，保障证据输入的时效一致性。
*   **知识库检索超时与熔断器 (KB Circuit Breaker)**：
    *   多库检索限定最长 `DEEP_RESEARCH_KB_TIMEOUT_SECONDS`（默认 180s）。一旦某个分支发生超时，除捕获该错误外，还会全局设置 `kb_circuit_open` 事件。
    *   其他处于检索等待队列的分支经检测该熔断器激活后，直接快速失败（Skip/Fail-fast），并在流中提示 `"本次研究的知识库检索已因前序分支超时而停止"`，防止级联排队延迟雪崩。
*   **优雅降级与 Fallback**：
    *   任何子任务分支失败时，异常仅被封装在 `CoordinationUnitResult.error` 中，不中断主流程；若最终写作智能体发生故障或超时，触发 `_build_fallback_report` 算法，直接编译初始规划大纲和各分支阶段结论，平稳完成 SSE 输出与工件生成。



---

## 🎓 领域专家能力模型

### 1. AgentProfile (`backend/config/agents/*.yaml`)
*   智能体身份、默认模型、知识库策略、工具策略、前端展示信息都通过 profile 配置表达。
*   当前主干不再通过 `NEFullFeatureAgent` / `CSHFullFeatureAgent` 这类类名表达领域差异。

### 2. ProfiledAgentExecutor (`backend/application/agent_executors/profiled_executor.py`)
*   根据 profile 构建 graph、Prompt、工具和运行时特性。
*   `ToolAssemblyPipeline` 统一注入 `multi_kb_search`、`web_search`、表格分析、HTML PPT 等工具。
*   不同领域差异收敛到 profile、Prompt 模板、SkillManifest 和 ToolPolicy，而不是复制执行器类。

---

## 👥 独立访客免登录端点

访客能力详见 [guest_module.md](./guest_module.md)。要点：

1. **双通道**：主 API `/api/v2/guest/query` 与独立服务 `:8083`。
2. **IndexedDB 主存**：会话历史由前端本地持久化，零 MySQL 写入。
3. **SQLite Checkpointer**：`backend/data/guest_checkpoints.db`，与主 API 的 `checkpoints.db` 物理隔离。
4. **隔离键**：`g_{device_id}__t_{thread_id}`。
5. **轻量模式**：走 lightweight workflow，并默认关闭 thinking 输出。

---

## 🛠️ 4. 知识库多库融合检索工具 (`multi_kb_search_tool`)

由于系统支持**双知识库（如核电子学教科书库与学术期刊库）**并行检索，智能体需要一个能统一调度且高效合并结果的原子工具。这就是 `multi_kb_search_tool` 的核心职责。

**核心算法**: RRF (Reciprocal Rank Fusion) 倒数排名融合。

```python
@tool("multi_kb_search")
async def multi_kb_search_tool(
    query: str,
    kb_ids: list[str] | None = None,
    top_k: int = 10,           # 默认 10，硬顶 MULTI_KB_SEARCH_MAX_TOP_K=12
    chunk_top_k: int = 6,      # 默认 6，硬顶 MULTI_KB_SEARCH_MAX_CHUNK_TOP_K=6
    merge_strategy: str = "rrf",
    # 可选：high_level_keywords / low_level_keywords / query_type
) -> str:
```

**性能相关环境变量**（`multi_kb_search_tool.py`）：`MULTI_KB_PATH_A_QUERY_ONLY=1`（每库 Path-A 仅 1 次整句向量）、`MULTI_KB_SEARCH_MAX_RESULT_CHARS=5000`、`MULTI_KB_SEARCH_NORMAL_MAX_CALLS=2`（NE）/ `5`（CSH）。进程内直调 `retrieve_context()`，不走 HTTP。

算法与 Profile 详见 [知识库算法和架构.md](../知识库算法和架构.md) §4、§8。

**执行流程**:
1.  **并发检索**: 接收用户提问与目标 `kb_ids`。使用 `asyncio.gather` 并发向所有选中的知识库实例发起 RAG 检索请求。
2.  **单库过滤**: 每个库独立 Stage-1 召回与 Stage-2 过滤。生产 Profile（`domain`/`lightweight`）使用 **MMR_ONLY**，跳过 PPR（详见 [rag_module.md](./rag_module.md)）。
3.  **RRF 融合算法**: 
    *   为了在不直接比较不同模型原始分数的情况下融合成对结果，系统采用倒数排名融合（RRF）。
    *   融合公式：`score = Σ(1 / (k + rank_i))`（默认常数 `k=60`）。
    *   通过此公式对所有来源的召回项进行打分并重排，保留最核心的全局 Top-N 切片。
4.  **多模态资产提取**: 自动提取并合并切片关联的图片与表格，保证多模态信息的连贯表达。
5.  **结构化 Prompt 构建**: 将融合后的事实依据封装为标准的 XML 结构上下文，返回给智能体作为推理支撑。

---

### 5. 工具组装与沙箱

**工具注入**：`ToolAssemblyPipeline`（`backend/application/tools/tool_assembly_pipeline.py`）按 profile、selected_skills、kb_ids、ToolPolicy 动态组装工具集。详见 [agent_platform_standardization_plan.md](../agent_platform_standardization_plan.md) §8。

> **lightweight workflow**：通常不挂载重型工具集，检索与回答走轻量执行路径；是否注入工具由 workflow + runtime feature 决定，而不是旧 `lightweight_mode=True` 外壳。

**内置工具**（`core/tools/builtin_tools.py`）:
-   `task`: 子智能体委派（`subagent_enabled` 时）

> `ask_clarification` 的实现仍被保留，但当前主干智能体默认不注入该工具。

**社区工具**（按需加载）:
-   `web_search`: 网络搜索
-   `academic_search`: 学术文献检索
-   `multi_kb_search`: 多库 RRF 融合检索

**沙箱**：生产主 Agent 默认剥离 bash/read_file 等沙箱工具；若未来新增依赖代码执行的 Skill，需显式启用 sandbox 特性。

---

### 6. 对话状态持久化

-   **Checkpointer**：`AsyncSqliteSaver`（主 API → `checkpoints.db`，访客 → `guest_checkpoints.db`）。
-   **隔离键**：登录用户 `u_{user_id}__t_{thread_id}`；访客 `g_{device_id}__t_{thread_id}`。
-   **MySQL 会话元数据**：`conversations` 表存标题、时间戳；checkpoint 存完整 Graph 状态。

---

### 7. MCP 服务

-   **主 API 挂载**：`/mcp/sse`（端口 8082，SSE 传输）。
-   **可选独立进程**：`python -m backend.api.mcp_server`。
-   暴露工具：`query_knowledge_base` 等。

详见 [MCP服务访问指南.md](../MCP服务访问指南.md)。

---

## 🔧 常见问题（排障）

| 现象 | 可能原因 | 排查 |
| :--- | :--- | :--- |
| 无 Skill 时回答很慢 | 走了 full workflow 而非轻量 | 确认请求未带 `selected_skills`；检查 workflow 解析日志 |
| 选了 Skill 但无 `multi_kb_search` | `kb_ids` 为空或 lightweight | 检查 `/info` 返回的 `kb_ids`、绑定表 `agent_kb_bindings` |
| SSE 只有 `status` 无 `chunk` | 模型未流式 / 连接中断 | Swagger 同参复现；查 `api.log` 与 LLM 超时 |
| 多轮后上下文爆掉 | ContextPruning 未生效 | 确认 graph builder 注入了 `ContextPruningMiddleware`，并检查 `context_pruning.py` 日志 |
| 访客深研 400 | 设计限制 | `guest.py` 拒绝 `deep_research` / `subagent_enabled` |
| checkpoint 串会话 | thread_id 或隔离键错误 | 登录用户须 `u_{user_id}__t_{thread_id}`；勿复用他人 thread_id |

代码入口：`agent_service.py` · `standard_agent_workflow.py` · `profiled_executor.py` · `stream_handler.py`。

---

## 🚀 后续开发方向

| 方向 | 现状 | 建议 |
| :--- | :--- | :--- |
| 多智能体 Swarm | `task` 子 Agent 已可用 | 扩展领域专家 Specialist |
| 自省循环 | 未实现 | `@Prev(ClarificationMiddleware)` 注入 ReflectionMiddleware |
| 对话追踪 | 结构化日志已有 | 接入 OpenTelemetry |
| 动态 Skill 热更新 | `ScopedSkillsProvider` 已实现 | 管理 UI 上传 Skill |
| 停止生成 | 部分实现 | 前端 `AbortController` 断开 SSE；深研 `POST /api/v2/agent/task/{id}/cancel`；运行中 LangGraph 尚无通用 cancel API |
| Checkpointer 生产化 | SQLite | 评估 Redis/Postgres 多 Worker |

---
> [!IMPORTANT]
> 在修改智能体 Prompt 时，优先调整 `PromptBuilder`、profile 配置和 prompt templates；在扩展运行时行为时，优先通过 workflow / executor / middleware 组合完成，而不是恢复领域专属 FullFeatureAgent 基类。
