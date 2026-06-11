# 协同智能体工作流规范与运行时引擎设计详解

> 最后同步：2026-06-10  
> 状态：✅ 已完全落地。多智能体并发调度、任务心跳进度保活、超时控制、知识库检索熔断与大对象异步归档已全面实现，并通过契约测试。

---

## 1. 定位与业务场景

在处理复杂、多步骤的学术或科研分析任务（如“场地污染水文地质调查报告”或“核电子学文献对比分析”）时，传统的单 Agent 循环不仅 Token 消耗大，而且容易陷入局部幻觉或工具死循环。为此，平台设计了**协同智能体工作流（Coordinated Agent Workflows）**。

平台通过统一的路由入口 `/api/v2/agent-runtime/query`，由 `AgentService` 进行调度分发，主要支持以下三类复杂协同工作流：

| Workflow | 调度策略类 (`backend/application/coordination/`) | 并发/顺序特征 | 典型应用与调度逻辑 |
| :--- | :--- | :--- | :--- |
| `plan_solve` / `deep_research` | `DefaultCoordinationStrategy` | 并发分支 + 顺序收敛 | **并发拆解与汇总**：规划智能体 (Planner) 拆解问题 → 并发启动多个子智能体 (Researcher) 收集并分析证据 → 汇总智能体 (Writer/Reviewer) 输出最终报告。 |
| `multi_agent_parallel` | `DefaultCoordinationStrategy` | 纯并发视角分析 | **多视角并发分析**：并发启动三个不同视角的分析单元 (`domain_analyst` 领域分析、`evidence_researcher` 证据检索、`risk_reviewer` 风险审查) → 聚合智能体 (`synthesizer`) 解决冲突并输出最终答案。 |
| `multi_agent_alternating` | `DefaultCoordinationStrategy` | 顺序轮次推进 | **轮次交替推进**：按轮次顺序推进 (`analyst` 提出假设 → `critic` 修正纠错 → `finalizer` 整合输出)，通过共享上下文累积协同成果并收敛。 |

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
     │         [分支 1: researcher_1]      [分支 2: researcher_2]      [分支 N: researcher_N]
     │                 │                           │                           │
     │         5. 并发预检索                5. 并发预检索                5. 并发预检索
     │           Web 检索(并发)              Web 检索(并发)              Web 检索(并发)
     │           RAG 检索(同步线程池)         RAG 检索(同步线程池)         RAG 检索(同步线程池)
     │                 │                           │                           │
     │         6. barrier.wait() ──┐       6. barrier.wait() ──┐       6. barrier.wait() ──┐
     │                             ▼                           ▼                           ▼
     │                           [汇合点] ────────────────────► [汇合点] ────────────────────► [汇合点]
     │                             │                           │                           │
     │                        (释放执行)                  (释放执行)                  (释放执行)
     │                             ▼                           ▼                           ▼
     │                       7. 子智能体分析             7. 子智能体分析             7. 子智能体分析
     │                          (心跳及超时监控)            (心跳及超时监控)            (心跳及超时监控)
     │                             │                           │                           │
     │                             └───────────────────────────┼───────────────────────────┘
     │                                                         │
     │                                                         ▼
     │                                              8. writer 汇总智能体
     │                                             (合并证据并输出长文报告)
     │                                                         │
     │                                                         ▼
     │                                              9. 异步生成 .docx 文档
     │                                                 (物理落盘与 MinIO 注册)
     │                                                         │
     │                                                         ▼
     │                                              10. 释放研究信号与资源
     │                                                         │
     ▼                                                         ▼
[输出 SSE 事件] <──────────────────────────────────────────────┘
```

---

## 3. 核心机制设计

为了保障在重度并发下的高性能与系统稳定性，平台在 `CoordinatedAgentWorkflow` 中实现了一整套健壮的底层机制：

### 3.1 事件循环非阻塞设计 (`run_blocking`)
由于 RAG（多知识库检索与 RRF 融合计算）和文档生成（python-docx 复杂表格与版面排版）都是重 CPU 密集型任务，如果在 FastAPI 的主 Event Loop 中直接同步执行，会导致主线程被长时间独占，使其他用户的请求或处于活跃状态的 SSE 遇到数秒甚至数分钟的延迟（Event Loop Delay）。
*   **非阻塞隔离器** (`backend/application/blocking.py`)：实现了一个独立维护的 `ApplicationBlockingExecutor`（继承自 `concurrent.futures.Executor`）。工作线程在后台以 daemon 模式运行，任务以 `partial` 的形式通过 `run_blocking` 工具函数投递。
*   **跨 Event Loop 调用**：由于知识库检索依赖复杂的底层 RAG 图，工作线程必须通过 `asyncio.run` 在独立的子线程 Loop 中拉起 `execute_multi_kb_search`，实现了引擎和 API 层的完全物理隔离。
*   **工件注册重构**：平台已全面将同步工件注册函数替换为异步的 `register_local_artifact_for_thread`，保证大对象文件操作不阻塞。

### 3.2 并发协同屏障 (`_AsyncTaskBarrier`)
在 `plan_solve` 工作流中，各个研究分支可能由于检索词长度、网络状况或数据库性能的不同，导致预检索（Web / RAG）完成的时间存在显著差异。
*   **设计目的**：为了确保所有 Researcher 子任务在**执行大模型推理分析前**拥有相同粒度的前序证据（例如：Researcher 1 不能在 Researcher 2 的预检索还没结束前就抢占计算资源），引入了 `_AsyncTaskBarrier`。
*   **同步逻辑**：
    1. 在 ` CoordinatedAgentWorkflow._run_plan_solve` 初始化时，根据拆解的 Task 数量构造 Barrier。
    2. 每个分支在完成其前期的 Web 和 RAG 检索后，会调用 `await research_barrier.wait()`。
    3. `_AsyncTaskBarrier` 内部维护一个 `asyncio.Event` 与到达计数器。当所有分支均报告就绪（`arrived >= parties`）时，Event 被激活，所有子智能体任务同步启动大模型分析，大幅提高了多任务协同的有序性。

### 3.3 知识库检索超时与熔断器 (KB Circuit Breaker)
在高负载的并发深研中，由于向量计算与图谱检索较慢，个别子分支的慢检索或超时可能会把整个研究任务拖垮。
*   **超时硬限**：每次 RAG 检索任务都受限于 `_KB_SEARCH_TIMEOUT_SECONDS`（默认 180s）。
*   **熔断触发**：若某一个分支的检索协程发生了 `TimeoutError`，除了该分支记录错误外，还会调用 `kb_circuit_open.set()` 激活全局熔断器。
*   **熔断检查与快速失败（Fail-Fast）**：
    ```python
    async with self._kb_search_lock:
        if kb_circuit_open.is_set():
            raise RuntimeError("本次研究的知识库检索已因前序分支超时而停止")
        try:
            kb_result = await run_blocking(
                self._execute_kb_search_blocking,
                task.question,
                list(context.kb_ids),
                timeout_seconds=float(_KB_SEARCH_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError as exc:
            kb_circuit_open.set()  # 触发熔断
            raise RuntimeError(f"知识库检索超过 {_KB_SEARCH_TIMEOUT_SECONDS} 秒，已停止后续检索") from exc
    ```
    处于排队状态的其他分支在尝试获取 I/O 锁后，一经检测到熔断器已开启，将立即快速失败，快速释放被占用的并发锁资源。

### 3.4 子任务单元超时、心跳与流式保活 (`_run_unit`)
由于大模型生成较长内容且包含工具调用，Researcher 子任务的耗时往往不可控。为了避免 SSE 连接因长时间无事件输出而被网关或前端 Abort，平台对子智能体单元实现了心跳控制：
*   **排队资源限制**：通过 `asyncio.Semaphore` 控制最大并发的深研流程。当达到 `DEEP_RESEARCH_MAX_CONCURRENT` 限制时，任务自动处于排队状态。此时系统会定时向前端投递 `research_queued` 进度状态。
*   **保活心跳**：单元在 `_run_unit` 的事件循环中，利用 `asyncio.shield` 包装了大模型流式迭代器，并在此循环中执行超时轮询。如果等待时间超过 `_UNIT_HEARTBEAT_SECONDS`（默认 10s），则主动触发一个 `task_progress` 或 `status` 类型的 `workflow.step` 事件，将“已等待 X 秒”的进度实时反馈给前端。
*   **超时保护**：
    *   **空闲超时** (`_UNIT_IDLE_TIMEOUT_SECONDS`，默认 120s)：如果子任务在该时限内无任何 SSE 输出，立即抛出空闲超时异常。
    *   **总执行超时** (`_UNIT_TOTAL_TIMEOUT_SECONDS`，默认 300s)：一旦子任务总耗时超限，强行结束读取并中断流，将当前已收集的局部答案作为分支结论，防止整个任务挂起。

### 3.5 降级 Fallback 报告生成
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
    ```
*   这保证了整个 SSE 流一定能以合法的 `coordination.completed` 和 `done` 事件平稳降级收尾。

---

## 4. 关键超时与并发配置参数

系统提供了丰富的环境变量来精细控制协同引擎的行为，各项配置如下表：

| 环境变量 | 参数名 | 默认值 | 最小值限制 | 作用说明 |
| :--- | :--- | :--- | :--- | :--- |
| `DEEP_RESEARCH_MAX_CONCURRENT` | `_MAX_CONCURRENT_RESEARCH` | `2` | `1` | 限制系统内同时执行深研/多智能体并发的总任务数，多余请求将进入排队。 |
| `DEEP_RESEARCH_HEARTBEAT_SECONDS` | `_UNIT_HEARTBEAT_SECONDS` | `10` | `2` | 流式事件读取的心跳检查间隔。超时未获取新 Token 将向前端推送进度保活事件。 |
| `DEEP_RESEARCH_UNIT_IDLE_TIMEOUT_SECONDS` | `_UNIT_IDLE_TIMEOUT_SECONDS` | `120` | `10` | 单个子智能体在没有输出新事件时的空闲超时限制。 |
| `DEEP_RESEARCH_UNIT_TOTAL_TIMEOUT_SECONDS` | `_UNIT_TOTAL_TIMEOUT_SECONDS` | `300` | `30` | 单个子智能体任务从启动到结束的绝对耗时上限。 |
| `DEEP_RESEARCH_KB_TIMEOUT_SECONDS` | `_KB_SEARCH_TIMEOUT_SECONDS` | `180` | `10` | 知识库融合检索 (`multi_kb_search`) 的最长耗时硬限，超时则触发全局熔断。 |
| `DEEP_RESEARCH_WEB_TIMEOUT_SECONDS` | `_WEB_SEARCH_TIMEOUT_SECONDS` | `35` | `10` | 联网检索 (`web_search`) 接口请求的最长耗时上限。 |
| `AGENT_BLOCKING_MAX_WORKERS` | `_MAX_WORKERS` | `8` | `1` | 物理阻塞执行器线程池 (`ApplicationBlockingExecutor`) 的最大工作线程数。 |

---

## 5. 标准 SSE 事件契约与 payload 格式

协同工作流产生的事件严格按照 `AgentEvent(type, payload, ...)` 进行序列化，前端三端依靠以下规范进行状态树维护和渲染：

### 5.1 规划阶段 (Planner)
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

### 5.2 执行与心跳阶段 (Researcher)
*   **`workflow.step`** (进度汇报)：
    ```json
    {
      "event": {
        "type": "task_progress",
        "title": "子智能体正在分析证据",
        "content": "已等待 12 秒，任务仍在运行；达到超时上限后将自动使用阶段性结果收敛。",
        "tool": "task",
        "level": "active",
        "metadata": { "task_id": "req-xxx:research:1", "runtime_type": "task_running", "elapsed_seconds": 12, "role": "researcher_1" }
      }
    }
    ```
*   **`workflow.step`** (工具开始/结果)：
    ```json
    {
      "event": {
        "type": "tool_start",
        "title": "检索已选知识库",
        "content": "地层分布与隔水层条件",
        "tool": "multi_kb_search",
        "level": "active",
        "metadata": { "task_id": "req-xxx:research:1" }
      }
    }
    ```

### 5.3 收敛与完成阶段 (Writer & Artifact)
*   **`coordination.unit.completed`**：单个分支执行完毕，返回摘要预览。
    ```json
    {
      "workflow": "plan_solve",
      "role": "researcher_1",
      "error": null,
      "answer_preview": "根据召回的多知识库文献显示，该场地第一层为粉质粘土，厚度3.2米..."
    }
    ```
*   **`message.delta`**：流式推送由写作智能体或降级编译器输出的最终学术报告 Markdown 正文。
*   **`workflow.step`** (工件推送)：携带生成的可下载文档的下载链接和 MinIO 元数据，前端激活侧边栏工件下载卡片。
    ```json
    {
      "event": {
        "type": "artifact",
        "title": "成都理工大学深度研究报告.docx",
        "content": "报告生成完成，点击可下载",
        "tool": "artifact",
        "level": "success",
        "metadata": {
          "artifact_type": "docx",
          "artifact_url": "http://<api_host>:<api_port>/api/v2/files/download?path=users%2F1%2Fthreads%2Fthread_abc%2Foutputs%2Freports%2Foutput.docx"
        }
      }
    }
    ```
*   **`coordination.completed`**：返回协同任务执行总指标。
*   **`done`**：关闭连接信号。

---

© 2026 核电子学智能助手项目组
