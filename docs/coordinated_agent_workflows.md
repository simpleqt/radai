# 协同智能体工作流规范与运行时引擎设计详解

> 最后同步：2026-07-16
> 状态：✅ 已完全落地。多智能体并发调度、任务心跳进度保活、超时控制、知识库检索熔断与大对象异步归档已全面实现，并通过契约测试。子智能体角色由 `runtime_agents.yaml` 声明；Handoff V2 与 Trace 见 [codex_like_agent_runtime_optimization_plan.md](./codex_like_agent_runtime_optimization_plan.md)。

---

## 1. 定位与业务场景

在处理复杂、多步骤的学术或科研分析任务（如“场地污染水文地质调查报告”或“核电子学文献对比分析”）时，传统的单 Agent 循环不仅 Token 消耗大，而且容易陷入局部幻觉或工具死循环。为此，平台设计了**协同智能体工作流（Coordinated Agent Workflows）**。

平台通过统一的路由入口 `/api/v2/agent-runtime/query`，由 `AgentService` 进行调度分发，主要支持以下三类复杂协同工作流：

| Workflow | 主要实现 | 并发/顺序特征 | 典型应用与调度逻辑 |
| :--- | :--- | :--- | :--- |
| `plan_solve` | `backend/application/agent_workflows/coordinated_agent_workflow.py` + `DeepResearchSupervisor` 兼容入口 | 依赖感知并发 + 顺序收敛 | **有界任务求解**：`planner` 拆解问题 → 专业角色按依赖逐波执行 → `reviewer` 汇总。 |
| `deep_research` | `DeepResearchSupervisor` + `ResearchScheduler` + `ReportConvergenceBuilder` | 并发研究波次 + 滚动覆盖监督 + 证据收敛 | **证据驱动研究**：`Research Brief` 限定范围 → 并发专业研究 → `Coverage Ledger` 检查覆盖、新来源、冲突与缺口 → 按信息增益补研 → 去重 `Evidence Ledger` → `report_writer`。 |
| `multi_agent_parallel` | `CoordinatedAgentWorkflow._run_parallel()` | 并发分析 + 依赖收敛 | **多视角并发分析**：并发启动领域、数据、证据和风险单元并生成结构化 handoff → `solution_architect` 消费前置交接形成方案 → 聚合智能体解决冲突并输出最终答案。 |
| `multi_agent_alternating` | `CoordinatedAgentWorkflow._run_alternating()` | 顺序轮次推进 | **轮次交替推进**：按轮次顺序推进 (`analyst` → `critic` → `finalizer`)，通过结构化交接累积协同成果并收敛。 |

---

## 2. 总体架构与数据流

下图展示了协同智能体工作流引擎在执行 `plan_solve` 时的任务生命周期、多线程/协程边界以及事件分发队列：

```text
               [FastAPI 主协程 (Event Loop)]
                            │
               POST /api/v2/agent-runtime/query
                            │
                            ▼
              1. WorkflowRegistry.resolve()
                            │
                            ▼
              2. CoordinatedAgentWorkflow.run()
                            │
     ┌──────────────────────┴──────────────────────┐
     ▼                                             ▼
[asyncio.Queue] <─── 投递 Event                 创建隔离任务
(事件分发队列)                                      │
     │                                             ▼
     │                                  3. planner 规划智能体
     │                                 (生成 Research Tasks)
     │                                             │
     │                                             ▼
     │                                  4. 创建并发执行分支
     │                                     asyncio.gather(...)
     │                                             │
     │                 ┌───────────────────────────┼───────────────────────────┐
     │                 ▼                           ▼                           ▼
     │         [分支 1: domain_analyst]    [分支 2: evidence_researcher] [分支 N: risk_reviewer]
     │                 │                           │                           │
     │         5. 并发预检索                5. 并发预检索                5. 并发预检索
     │           Web 检索(并发)              Web 检索(并发)              Web 检索(并发)
     │           RAG 检索(异步工具)           RAG 检索(异步工具)           RAG 检索(异步工具)
     │                 │                           │                           │
     │         6. 自适应分析门控 ──┐       6. 自适应分析门控 ──┐       6. 自适应分析门控 ──┐
     │                       ▼                           ▼                           ▼
     │                     [释放/超时] ────────────────► [释放/超时] ────────────────► [释放/超时]
     │                       │                           │                           │
     │                  (释放执行)                  (释放执行)                  (释放执行)
     │                       ▼                           ▼                           ▼
     │                 7. 子智能体分析             7. 子智能体分析             7. 子智能体分析
     │                          (心跳及超时监控)            (心跳及超时监控)            (心跳及超时监控)
     │                             │                           │                           │
     │                             └───────────────────────────┼───────────────────────────┘
     │                                                         │
     │                                                         ▼
     │                                              8. deep_research 滚动证据监督
     │                                  (Coverage Ledger + 按需启动下一批定向补研)
     │                                                         │
     │                                                         ▼
     │                                              9. writer 汇总智能体
     │                                             (合并证据并输出长文报告)
     │                                                         │
     │                                                         ▼
     │                                              10. 策略化保存 .docx 文档
     │                                             (RuntimeToolInvoker + MinIO 注册)
     │                                                         │
     │                                                         ▼
     │                                              11. 释放研究信号与资源
     │                                                         │
     ▼                                                         ▼
[输出 SSE 事件] <──────────────────────────────────────────────┘
```

> **HTTP 生命周期说明**：上图展示的是协同工作流内部执行链。实际路由中，`deep_research` 与 `plan_solve` 会先由 `AgentTaskRegistry` 创建可重连后台任务；`POST /api/v2/agent-runtime/query` 只返回启动确认，任务的进度、最终答案和断线续接由 `GET /api/v2/agent-runtime/{agent_code}/task/{task_id}/stream` 负责，`status/cancel` 提供状态查询和取消。`multi_agent_*` 等非重连工作流仍直接占用当前 `/query` SSE。

---

## 3. 核心机制设计

为了保障在重度并发下的高性能与系统稳定性，平台在 `CoordinatedAgentWorkflow` 中实现了一整套健壮的底层机制：

### 3.1 运行时工具调用与事件循环保护
协同工作流不直接 import `execute_multi_kb_search`、`execute_web_search` 或报告保存 provider。所有编排层主动工具调用都通过 `backend/application/tools/runtime_invoker.py` 的 `RuntimeToolInvoker` 完成。

*   **策略化装配**：`RuntimeToolInvoker` 根据 `AgentRuntimeContext`、workflow、ToolPolicy、Skill、KB、`use_search` 和 `thread_id` 重新装配工具，保证预检索、子智能体和报告保存遵循同一套准入规则。
*   **运行时 Guardrail**：调用前会使用 `RuntimePolicyGuardrailProvider` 对实际 allowlist、权限和领域范围再校验一次。协同编排代码不得绕过该链路直接调用 provider。
*   **检索职责**：`deep_research` 不再由编排器把完整任务直接作为搜索词；专业子智能体自主调用 `web_search`，检查结果相关性并迭代查询。`plan_solve` 保留兼容性的编排层预检索。`multi_kb_search` 通过 `RuntimeToolInvoker.invoke_structured()` 在 `run_blocking()` 中执行，以隔离同步检索链路并保留超时、熔断和 evidence record 返回；`save_deep_research_report` 也走运行时工具调用链。
*   **Web 提供者**：所有通用 `web_search` 都固定调用本地开源 Firecrawl Search，并通过 `scrapeOptions` 将命中页面的主正文 Markdown 一并交给智能体；不再回退或切换到其他客户端搜索提供者。Firecrawl 通过本地 SearXNG 聚合 Bing、百度和夸克，部署入口见 `deploy/firecrawl/`，工具接口对智能体保持不变。`academic_search` 是面向 DOI、引用量等结构化字段的专业学术数据库工具，不属于通用网页搜索开关。
*   **工件注册**：报告工具内部完成 DOCX 生成、本地/MinIO 存储与异步工件注册。Workflow 只解析工具返回 payload 并发出 artifact 事件。

### 3.2 依赖感知调度与自适应分析门控
`plan_solve` 使用任务依赖逐波调度：依赖已满足的分支并发启动，后续分支在前置结果完成后获得结构化交接。`deep_research` 的证据分支保持独立并发。

`_AdaptiveAnalysisGate` 仅在首批分支之间提供有超时上限的轻量协调，避免大模型槽位被过早占满；它不会等待所有检索完成，也不会让快分支被慢分支永久阻塞。

### 3.3 研究契约、滚动覆盖监督与证据压缩
`deep_research` 的 Planner 首先输出结构化 `Research Brief`，明确目标、必要维度、排除范围、来源策略和成功条件；缺失该字段时由请求约束生成兼容性 Brief，不额外增加一次模型调用。

每个并行研究波次完成后，系统先用确定性代码生成 `Coverage Ledger`，记录计划/已覆盖/降级/未解决维度、逐项引用覆盖率、独立来源数、冲突、缺失项和本轮新增来源比例，再把 Research Brief、覆盖账本、引用预校验和证据矩阵交给无工具监督智能体。监督结果除 `complete/reason/tasks` 外，还必须返回覆盖率、来源多样性、冲突数、预期信息增益和停止原因。

引用预校验同时构建确定性的 Claim–Source 图谱：近似等价 Claim 会合并不同来源和研究分支，形成 `corroboration_rate`；来源按用户知识库、项目数据、政府/高校、学术索引、普通网页等类别统计；主题高度重合但数值不同或否定关系相反的 Claim 形成冲突边。冲突存在时，Scheduler 在补研预算内优先增加一个高风险 `risk_reviewer` 核验单元，用于检查版本、时间、测量条件和适用范围，而不是让 Writer 根据置信度自行选择一方。

若证据充分则直接进入 writer；若存在会影响最终答案且预计能获得新信息的缺口，监督智能体最多生成 `DEEP_RESEARCH_FOLLOWUP_TASKS` 个完整研究任务。Scheduler 对完全相同和中文轻微改写后的近似问题统一去重。连续补研新增来源比例不高于 0.1 且监督智能体给出的预期信息增益低于 0.35 时，系统按研究饱和终止，不再机械增加分支。结构化结果解析失败仍采用 fail-closed 策略；总波次数由 `DEEP_RESEARCH_REFLECTION_ROUNDS` 限制。

进入 writer 前，系统不再同时注入各 24,000 字符的证据矩阵和 Handoff Bundle，而是生成单一、最多 28,000 字符的 `Evidence Ledger`：只保留逐项绑定来源的 Claim，按置信度与来源数排序，合并 URL 查询参数/锚点造成的重复来源，排除无引用摘要和无来源结论，并携带交叉印证率、来源质量分布和冲突统计。Writer 先按 Research Brief 形成章节提纲，再完全依据该证据账本写作；最终答案继续经过确定性 Claim/引用核验，并在运行事件中分别记录确定支持、低词项重叠的语义复核候选、数值/否定/引用冲突和无支撑结论，避免把所有模糊匹配直接渲染成“待核验墙”。

### 3.4 分层模型路由

`deep_research` 将规划器和 `budget <= DEEP_RESEARCH_FAST_MODEL_MAX_BUDGET` 的低风险 `domain_analyst`、`evidence_researcher`、`risk_reviewer` 分支路由到 `DEEP_RESEARCH_FAST_MODEL_NAME`（默认探索扩展模型 `qwen3.6-35b`）。`data_analyst`、`solution_architect`、最终 `report_writer`、高风险任务及更高预算任务继续使用用户选择的主模型。`plan_solve` 不启用该自动分层，避免改变现有工作流语义。

选中的模型名、模型层级和任务预算会写入运行树 metadata 与 `task_start`/`coordination.unit.started` 事件，便于审计实际路由。将快速模型名留空或将最大预算设为 `0` 即可关闭。Manifest 中的 `model: fast` 是仅在深度研究中生效的符号层级，而不是供应商模型名。

### 3.5 知识库检索超时与熔断器 (KB Circuit Breaker)
在高负载的并发深研中，由于向量计算与图谱检索较慢，个别子分支的慢检索或超时可能会把整个研究任务拖垮。
*   **超时硬限**：每次 RAG 检索任务都受限于 `_KB_SEARCH_TIMEOUT_SECONDS`（默认 180s）。
*   **熔断触发**：若某一个分支的检索协程发生了 `TimeoutError`，除了该分支记录错误外，还会调用 `kb_circuit_open.set()` 激活全局熔断器。
*   **熔断检查与快速失败（Fail-Fast）**：
    ```python
    if kb_circuit_open.is_set():
        raise RuntimeError("本次研究的知识库检索已因前序分支超时而停止")
    try:
        kb_result = await run_blocking(
            self._invoke_runtime_tool_structured_blocking,
            context,
            self.name,
            "multi_kb_search",
            retrieval_policy.kb_tool_args(task.question),
            RuntimeToolInvoker,
            timeout_seconds=float(_KB_SEARCH_TIMEOUT_SECONDS),
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:
        kb_circuit_open.set()
        retrieval_request_cache.cancel_provider_requests()
        raise RuntimeError(
            f"知识库检索超过 {_KB_SEARCH_TIMEOUT_SECONDS} 秒，"
            "已停止本次研究的后续知识库检索"
        ) from exc
    ```
    处于排队状态的其他分支在检测到熔断器已开启后，将立即快速失败，快速释放被占用的并发资源。

### 3.6 子任务单元超时、心跳与流式保活 (`_run_unit`)
由于大模型生成较长内容且包含工具调用，Researcher 子任务的耗时往往不可控。为了避免 SSE 连接因长时间无事件输出而被网关或前端 Abort，平台对子智能体单元实现了心跳控制：
*   **排队资源限制**：通过 `ResourceLeaseManager` 控制最大并发的深研流程。当达到 `COORDINATED_RESEARCH_MAX_CONCURRENT` 限制时，获取租约的协程会等待前序任务释放资源；`AGENT_RESOURCE_LEASE_BACKEND=memory` 为单进程默认值，`redis` 可用于多 Worker，并通过 TTL 与 fencing token 防止过期租约继续持有资源。
*   **保活心跳**：单元在 `_run_unit` 的事件循环中，利用 `asyncio.shield` 包装了大模型流式迭代器，并在此循环中执行超时轮询。如果等待时间超过 `_UNIT_HEARTBEAT_SECONDS`（默认 10s），则主动触发一个 `task_progress` 或 `status` 类型的 `workflow.step` 事件，将“已等待 X 秒”的进度实时反馈给前端。
*   **超时保护**：
    *   **空闲超时** (`_UNIT_IDLE_TIMEOUT_SECONDS`，默认 120s)：如果子任务在该时限内无任何 SSE 输出，立即抛出空闲超时异常。
    *   **总执行超时** (`_UNIT_TOTAL_TIMEOUT_SECONDS`，默认 300s)：一旦子任务总耗时超限，强行结束读取并中断流，将当前已收集的局部答案作为分支结论，防止整个任务挂起。

### 3.7 降级 Fallback 报告生成
如果最后的 `report_writer` 或 `reviewer` 节点在大长篇整合时失败（例如：LLM 提示词敏感拦截、连接意外中断或返回空内容），协同工作流将自动触发降级机制：
*   **降级策略**：调用 `_build_fallback_report` 收集并解析历史的 `Planner` 初始大纲以及各个 `Researcher` 分支的局部答案，按照预设 Markdown 格式进行结构化汇编：
    ```markdown
    # 深度研究报告：{request.question}

    ## 研究说明
    最终写作智能体未返回完整内容，以下报告由规划结果和各研究分支的有效证据自动汇编。

    ## 研究计划
    {planner.answer}

    ## {分支 1 Title}
    {branch_1.answer}

    ...

    ## 结论与证据缺口
    请结合上述分支结论交叉核验引用；未被检索证据支持的内容不应视为确定事实。
    ```
*   这保证了整个 SSE 流一定能以合法的 `coordination.completed` 和 `done` 事件平稳降级收尾。

---

## 4. 关键超时与并发配置参数

系统提供了丰富的环境变量来精细控制协同引擎的行为，各项配置如下表：

这些环境变量由 `backend/domain/agents/runtime_budget.py` 中的 `AgentRuntimeBudget.from_env()` 统一解析，`CoordinatedAgentWorkflow` 再派生模块级运行常量。新增预算项应先进入 `AgentRuntimeBudget`，避免在工作流里新增孤立常量。

| 环境变量 | 参数名 | 默认值 | 最小值限制 | 作用说明 |
| :--- | :--- | :--- | :--- | :--- |
| `COORDINATED_RESEARCH_MAX_CONCURRENT` | `_MAX_CONCURRENT_RESEARCH` | `5` | `1` | 限制系统内同时执行深研/多智能体并发的总任务数，多余请求将进入排队。 |
| `COORDINATED_RESEARCH_PER_USER_MODEL_CONCURRENT` | `_MAX_CONCURRENT_PER_USER_MODEL` | `5` | `1` | 限制同一用户、同一模型的协调单元并发。 |
| `PLAN_SOLVE_MAX_CONCURRENT_SUBAGENTS` | `_PLAN_SOLVE_MAX_CONCURRENT` | `16` | `1` | `plan_solve` 内 LLM 单元并发上限。 |
| `DEEP_RESEARCH_EVENT_QUEUE_MAXSIZE` | `_EVENT_QUEUE_MAXSIZE` | `64` | `1` | `plan_solve` / `deep_research` 的内部事件队列容量。 |
| `DEEP_RESEARCH_HEARTBEAT_SECONDS` | `_UNIT_HEARTBEAT_SECONDS` | `10` | `2` | 流式事件读取的心跳检查间隔。超时未获取新 Token 将向前端推送进度保活事件。 |
| `DEEP_RESEARCH_UNIT_IDLE_TIMEOUT_SECONDS` | `_UNIT_IDLE_TIMEOUT_SECONDS` | `120` | `10` | 单个子智能体在没有输出新事件时的空闲超时限制。 |
| `DEEP_RESEARCH_UNIT_TOTAL_TIMEOUT_SECONDS` | `_UNIT_TOTAL_TIMEOUT_SECONDS` | `300` | `30` | 单个子智能体任务从启动到结束的绝对耗时上限；会在该总时限内最多预留 `DEEP_RESEARCH_PARTIAL_FINALIZATION_WAIT_SECONDS`，停止工具后基于已捕获证据收敛。 |
| `DEEP_RESEARCH_KB_TIMEOUT_SECONDS` | `_KB_SEARCH_TIMEOUT_SECONDS` | `180` | `10` | 知识库融合检索 (`multi_kb_search`) 的最长耗时硬限，超时则触发全局熔断。 |
| `DEEP_RESEARCH_WEB_TIMEOUT_SECONDS` | `_WEB_SEARCH_TIMEOUT_SECONDS` | `35` | `10` | 联网检索 (`web_search`) 接口请求的最长耗时上限。 |
| `COORDINATED_WORKFLOW_TOTAL_TIMEOUT_SECONDS` | `_WORKFLOW_TOTAL_TIMEOUT_SECONDS` | `420` | `60` | `plan_solve` 等协同流程的兼容性总时限；`deep_research` 不再使用它包住研究、writer 和保存三个阶段。 |
| `DEEP_RESEARCH_RESEARCH_PHASE_TIMEOUT_SECONDS` | `_DEEP_RESEARCH_RESEARCH_PHASE_TIMEOUT_SECONDS` | `900` | `60` | 深研规划、研究和证据反思共享的阶段时限；分支会感知剩余时间并提前停止工具、形成 handoff，耗尽后进入 writer。 |
| `DEEP_RESEARCH_WRITER_TIMEOUT_SECONDS` | `_DEEP_RESEARCH_WRITER_TIMEOUT_SECONDS` | `300` | `180` | 研究阶段结束后单独保留给最终 writer 的时限。 |
| `DEEP_RESEARCH_REPORT_TIMEOUT_SECONDS` | `_DEEP_RESEARCH_REPORT_TIMEOUT_SECONDS` | `120` | `30` | writer 完成后用于 DOCX 渲染、上传和工件注册的独立保存时限。 |
| `DEEP_RESEARCH_PARTIAL_HANDOFF_GRACE_SECONDS` | `_PARTIAL_HANDOFF_GRACE_SECONDS` | `0.25` | `0` | 自适应分析门控的阶段性交接等待窗口。 |
| `DEEP_RESEARCH_PARTIAL_WRITER_MIN_BRANCHES` | `_PARTIAL_WRITER_MIN_BRANCHES` | `1` | `1` | 阶段性 writer 至少需要的完成分支数。 |
| `DEEP_RESEARCH_PARTIAL_FINALIZATION_WAIT_SECONDS` | `_PARTIAL_FINALIZATION_WAIT_SECONDS` | `60.0` | `1.0` | 单元总时限内为禁用工具的阶段性结论收敛预留的最大窗口，不会延长单元或工作流绝对超时。 |
| `DEEP_RESEARCH_REFLECTION_ROUNDS` | `deep_research_reflection_rounds` | `1` | `0`（最大 `2`） | 首轮研究后执行的证据反思/补研轮数；设为 `0` 可关闭。 |
| `DEEP_RESEARCH_FOLLOWUP_TASKS` | `deep_research_followup_tasks` | `2` | `1`（最大 `5`） | 每轮监督智能体最多生成的定向补研任务数。 |
| `DEEP_RESEARCH_FAST_MODEL_NAME` | `deep_research_fast_model_name` | `qwen3.6-35b` | 空值可关闭 | 规划和低预算安全分支使用的快速探索模型。 |
| `DEEP_RESEARCH_FAST_MODEL_MAX_BUDGET` | `deep_research_fast_model_max_budget` | `1` | `0`（最大 `5`） | 允许路由到快速模型的任务预算上限；`0` 可关闭。 |

默认深研预算为 `900s 研究 + 300s writer + 120s 报告保存 = 1320s`，低于请求流的 1800s 深研硬上限；阶段间不复用剩余时间，确保研究超时后仍保留 5 分钟写作和 2 分钟保存窗口。

`FIRECRAWL_API_URL` 默认指向本机开源服务 `http://127.0.0.1:3002`，`FIRECRAWL_API_KEY` 与部署侧 `FIRECRAWL_LOCAL_API_KEY` 使用同一枚随机本地密钥，由 Nginx 网关执行 Bearer 鉴权；`FIRECRAWL_SEARCH_LOCATION` 默认 `China`。提供者固定为 Firecrawl，不接受前端或请求参数切换。默认不施加时间窗，只有智能体明确传入 `timelimit` 时才按日/周/月/年过滤，避免技术主题因最近一年结果稀疏而被低相关页面挤占。

---

## 5. 开源深度研究设计对照

| 参考项目 | 值得复用的机制 | 当前实现的对应优化 |
| :--- | :--- | :--- |
| [LangChain Open Deep Research](https://github.com/langchain-ai/open_deep_research) | 研究、摘要压缩和最终报告使用可独立配置的模型；研究循环依赖结构化输出/工具调用。 | 增加快速/主模型分层；保留结构化任务计划、Handoff 和证据矩阵。 |
| [GPT Researcher](https://github.com/assafelovic/gpt-researcher) | Planner、Crawler、来源跟踪摘要、过滤聚合分层；研究深度和广度受控。 | 专业分支自主迭代查询，限制每分支工具次数，并以任务预算、反思轮次和补研数量形成有界循环。 |
| [DeerFlow](https://github.com/bytedance/deer-flow) | Lead Agent 只做编排，隔离子智能体并行研究；渐进式上下文加载、摘要与卸载。 | 子智能体使用隔离 thread/context，依赖感知并发执行，只通过结构化 Handoff 向 writer 交接。 |
| [AutoGen Magentic-One](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/magentic-one.html) | Orchestrator 维护 task/progress ledger；不同角色可使用不同 LLM/SLM。 | 运行树记录任务状态、预算、模型层级与模型名；简单分支用 35B，高风险综合和最终写作保留主模型。 |

本项目不照搬无限递归或自由生成子智能体。现有用户/模型并发租约、树级 token/tool/time 预算、最多两轮证据反思和 fail-closed 解析共同限定搜索成本；这更适合当前需要稳定 SSE、可取消、可重连和可审计的服务形态。

---

## 6. 标准 SSE 事件契约与 payload 格式

协同工作流产生的事件严格按照 `AgentEvent(type, payload, ...)` 进行序列化。`workflow.step` 的内部 process event 由 `make_process_event()` 生成，字段是 `kind/title/detail/level/timestamp/tool/duration_ms/summary/metadata`，不是旧的 `type/content`。前端 `SubagentWorkflowTimeline.vue` 读取 `kind` 并按 `metadata.task_id`、`metadata.subagent_type` 分组展示。

### 6.1 规划阶段 (Planner)
*   **`workflow.started`**：工作流正式被 Registry 激活启动。
    ```json
    { "workflow": "plan_solve", "agent_code": "csh", "coordination_mode": "plan_solve" }
    ```
*   **`plan.created`**：下发拆解后的研究任务计划清单，前端用此结构初始化步骤 Checkbox 清单。
    ```json
    {
      "workflow": "plan_solve",
      "summary": "成都理工大学地下水污染深度研究方案",
      "stages": [
        { "key": "research:1", "title": "场地地质剖面特征分析", "description": "分析区域内地层分布与隔水层条件" }
      ]
    }
    ```

### 6.2 执行与心跳阶段 (Researcher)
*   **`workflow.step`** (进度汇报)：
    ```json
    {
      "event": {
        "kind": "task_progress",
        "title": "子智能体正在分析证据",
        "detail": "已等待 12 秒，任务仍在运行；达到超时上限后将自动使用阶段性结果收敛。",
        "tool": "task",
        "level": "active",
        "metadata": {
          "task_id": "req-xxx:research:1",
          "runtime_type": "task_running",
          "elapsed_seconds": 12,
          "role": "evidence_researcher",
          "subagent_type": "evidence_researcher"
        }
      }
    }
    ```
*   **`workflow.step`** (工具开始/结果)：
    ```json
    {
      "event": {
        "kind": "tool_start",
        "title": "检索已选知识库",
        "detail": "地层分布与隔水层条件",
        "tool": "multi_kb_search",
        "level": "active",
        "metadata": {
          "task_id": "req-xxx:research:1",
          "query": "地层分布与隔水层条件",
          "kb_ids": ["kb-xxx"]
        }
      }
    }
    ```

*   **`coordination.partial_handoff`**：研究分支完成结构化交接后立即发送，payload 携带 Handoff V2。
    ```json
    {
      "workflow": "plan_solve",
      "role": "evidence_researcher",
      "task_id": "req-xxx:research:1",
      "handoff": {
        "schema_version": "2.0",
        "task_id": "req-xxx:research:1",
        "subagent_type": "evidence_researcher",
        "summary": "可直接消费的分支结论",
        "claims": [],
        "evidence_refs": [],
        "source_coverage": {
          "total_claims": 0,
          "claims_with_refs": 0,
          "unsupported_claims": 0
        },
        "validation_warnings": ["claims are empty"]
      },
      "metadata": { "partial_handoff": true, "scheduler_state": "partial_handoff" }
    }
    ```

### 6.3 收敛与完成阶段 (Writer & Artifact)
*   **`coordination.unit.completed`**：单个分支执行完毕，返回摘要预览。
    ```json
    {
      "workflow": "plan_solve",
      "role": "evidence_researcher",
      "error": null,
      "answer_preview": "根据召回的多知识库文献显示，该场地第一层为粉质粘土，厚度3.2米..."
    }
    ```
*   **`message.replace` / `message.delta`**：`plan_solve` 和 `deep_research` 在已流式输出过程事件后用 `message.replace` 发布最终 Markdown；非流式协同路径仍可使用 `message.delta`。
*   **`workflow.step`** (工件推送)：携带生成的可下载文档的下载链接和 MinIO 元数据，前端激活侧边栏工件下载卡片。
    ```json
    {
      "event": {
        "kind": "artifact",
        "title": "成都理工大学深度研究报告.docx",
        "detail": "报告生成完成，点击可下载",
        "tool": "artifact",
        "level": "success",
        "metadata": {
          "artifact_type": "docx",
          "artifact_url": "/api/v2/files/minio-artifact/download?object_name=users%2F1%2Fthreads%2Fthread_abc%2Foutputs%2Freports%2Foutput.docx&filename=output.docx&mime_type=application%2Fvnd.openxmlformats-officedocument.wordprocessingml.document"
        }
      }
    }
    ```
*   **`coordination.completed`**：返回协同任务执行总指标。
*   **`done`**：关闭连接信号。

---

© 2026 核电子学智能助手项目组
