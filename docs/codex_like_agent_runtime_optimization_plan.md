# 参考 Codex CLI 的后端智能体架构优化方案

> 状态：统一 `AgentRuntimeManager`、显式状态机、树级预算/取消、协调单元迁移、强类型 Handoff、EventEnvelope V2、fenced lease、运行树持久化/恢复，以及“SQLite/SQL 索引 + 本地/MinIO 对象 blob”Trace 后端均已进入实现；用户可见工作流输出保持兼容。  
> 目标：将当前后端智能体从“功能型 Agent 执行链”升级为“可配置、可治理、可压缩、可协同、可观测、可回放的 Agent Runtime”。  
> 参考对象：Codex CLI / Codex App 的执行模型、Skills progressive disclosure、Subagents、MCP、Hooks/Rules、Memories/trace 思路；本轮源码基线为 OpenAI Codex `c9d52de5ca52a6b4439e7c0f69b34f6331926bb4`。  

> 落地进展：已新增 `RuntimeAgentSpec`、`RuntimeAgentLoader`、`AgentRuntimeBudget` 与 `backend/config/agents/runtime_agents.yaml`，并将 `task` 子智能体配置从代码硬编码迁移到 manifest；协调工作流的并发、超时和 partial handoff 默认值已从统一预算对象派生。Context Artifact 存储已补充 `ContextArtifactStore` 协议、本地 metadata sidecar、原子写入、安全路径清洗、受限读取、`MinioContextArtifactStore`、`read_context_artifact` 工具与 `context_artifacts` 元数据表/迁移脚本。Handoff V2 已加入 `schema_version`、`task_id`、`source_coverage`、`validation_warnings` 与机器可读 JSON 交接块，并保留旧 XML/text 输入兼容；writer 前已加入 `CitationVerifier` 预校验报告，writer 后仍执行 unsupported claim 降级。Trace/Replay 已新增 JSONL 追踪模型、`AgentTraceStore`、`AgentTraceRecorder`、dry-run/tool-cache/full 离线 replay 与 `python -m backend.cli agent replay` 调试命令；标准工作流与协调工作流在 `trace_enabled=True` 时已可写入 run/event/unit/handoff trace，`RuntimeToolInvoker` 已可写入工具调用元数据、运行策略和策略来源 trace，SSE 出口、JIT context pack 与 writer 输入输出已可写入 JSONL trace；`agent eval` 已支持 JSONL 回归集离线评分、历史记录、趋势查询和版本对比；`/api/v2/agent-runtime/runs/{run_id}` 系列调试接口已支持按权限查看 run/events/context-packs/handoffs 与 replay。工具层已按线上服务智能体定位补齐幂等、并发、有限重试、输出上限、缓存与健康状态元数据，并通过 `McpToolDescriptor` 暴露为 MCP-like 描述。当前边界：`full` replay 为离线 trace 结构复现，不在 CLI 路径中重调外部模型或工具；高频内部 runtime 事件默认进入 trace，不直接改变前端 SSE 事件序列；MCP-like adapter 先提供描述与治理边界。

> 本轮新增：`AgentControl` 已提升为 `AgentRuntimeManager` 实现，`task_tool`、标准流式循环与协调工作流共用同一棵运行树；`spawn/wait/interrupt/list`、父子 cancellation token、FIFO 执行槽与终态幂等由同一状态机治理。运行树可镜像到内存/SQLite/共享 SQL store，worker 重启时恢复历史节点，并把失去真实 task/slot 的 `running/interrupting` 安全回收到可重试 `pending`。协调工作流的进程内 semaphore 已替换为支持 TTL、续租和 fencing token 的 memory/Redis lease。Trace 可在兼容 JSONL、SQLite 索引或共享 SQL 索引与本地/MinIO 对象后端之间显式切换。模型 token、工具调用和 deadline 已在异步图、轻量循环及直接协调调用边界统一计费，预算用量随运行树元数据恢复。

---

## 1. 背景与目标

当前项目已经具备较成熟的智能体能力：

- `StandardAgentWorkflow` 支持轻量与完整对话工作流；
- `CoordinatedAgentWorkflow` 支持 `plan_solve`、`deep_research`、`multi_agent_parallel`、`multi_agent_alternating`；
- `ContextPruningMiddleware` 已实现大工具结果捕获、状态提升、JIT 动态上下文注入；
- `ContextLedger` 已按 token budget 对 `system / skills / handoff / retrieval / tool_result / history / artifact` 分类裁剪；
- `task` 工具已支持本地子智能体委派；
- Skill、Tool、Workflow 已有 registry 与 policy 基础。

从 Codex CLI 的智能体设计看，原方案识别的主干短板已经进入实现态：

- 子智能体定义已迁移到 `backend/config/agents/runtime_agents.yaml`，由 `RuntimeAgentLoader` 加载；
- 上下文压缩已从“本地临时截断”升级为 `ContextArtifactStore` + `ContextLedger` + 受限读取工具；
- 多智能体调度的并发、超时、事件队列和 partial handoff 默认值已由每个 workflow 实例创建时捕获的不可变 `AgentRuntimeBudget` 派生；
- `task` 委派已接入请求树级 `AgentControl`，统一执行并发、总派生数、最大深度、生命周期和级联取消；
- Handoff V2 已包含 `schema_version`、`task_id`、`claims`、`source_coverage` 和 `validation_warnings`；
- trace/replay/eval 已形成可切换 JSONL 或索引+对象存储的闭环。

剩余边界主要是生产化：跨主机运行树存储需要部署共享 SQL 后端并执行建表，Redis lease 需要在多 worker 环境压测；远程 MCP server 尚未替换所有外部工具；`full` replay 在 CLI 路径中仍是离线结构复现而非重新调用模型/工具。

本方案目标是：在保留现有 LangGraph / LangChain 实现的基础上，补齐 Codex-like agent runtime 的工程治理层。

---

## 2. Codex CLI 可借鉴的核心设计

### 2.1 主线程保持决策，子线程隔离噪声

Codex 的 Subagents 设计核心不是“多开几个模型”，而是避免主上下文被探索日志、测试输出、检索过程污染。

对应到本项目：

- 主 agent 只保留用户目标、关键决策、最终答案；
- research 子智能体负责噪声较大的检索、探索、表格查询、交叉验证；
- writer / reviewer 只消费结构化 handoff 和 evidence matrix；
- 工具执行轨迹默认不进入最终 writer prompt。

### 2.2 Progressive Disclosure

Codex Skills 不会一开始加载完整技能文件，而是先让模型看到技能名称、描述、触发条件，选择后再读取 `SKILL.md`，需要 reference 时再进一步读取。

对应到本项目：

- Skill 初始只暴露 skill index；
- `read_local_skill_file` 后才注入完整规则；
- `read_skill_reference` 按需加载引用资料；
- JIT 上下文只注入与当前问题相关的技能片段。

### 2.3 Agent Manifest

Codex custom agents 通过配置描述 agent 的名称、用途、instructions、model、reasoning effort 与工具范围；本项目只参考其配置化思路，不引入代码执行能力。

对应到本项目：

- 将 `_SUBAGENT_CONFIGS` 从代码迁移为 YAML/TOML；
- planner、researcher、writer、reviewer、verifier 等角色都由 manifest 声明；
- 支持按 workflow、domain、skill 选择不同 agent role。

### 2.4 MCP-like Tool Layer

Codex 通过 MCP 将外部系统、文档、浏览器、Figma、GitHub 等接入为标准化工具。

对应到本项目：

- 保留现有 `ToolRegistry` / `ToolPolicy`；
- 为每个工具补充 `instructions`、risk level、approval mode、side effect；
- 外部服务逐步收敛为 MCP 或 MCP-like adapter；
- 工具装配统一走 runtime policy，不允许编排层绕过。

### 2.5 Trace、Replay 与可评估性

Codex 的高可靠性来自执行过程可观察、可复盘、可迭代。

对应到本项目：

- 每次 agent run 生成完整 trace；
- trace 能回放 planner 输出、分支任务、工具调用、handoff、writer 输入输出；
- 支持离线评估上下文压缩质量、引用覆盖率、工具调用预算、最终答案一致性。

### 2.6 请求树级控制面与执行租约

Codex 的关键做法不是让每个子任务各持有一个互不相干的 semaphore，而是让根会话和所有子会话共享一个控制面。源码中的 [`AgentControl`](https://github.com/openai/codex/blob/c9d52de5ca52a6b4439e7c0f69b34f6331926bb4/codex-rs/core/src/agent/control.rs) 统一维护 registry、并发限制和 rollout budget；[`ExecutionGuard`](https://github.com/openai/codex/blob/c9d52de5ca52a6b4439e7c0f69b34f6331926bb4/codex-rs/core/src/agent/control/execution.rs) 用执行租约保证槽位随真实执行结束释放；[`spawn`](https://github.com/openai/codex/blob/c9d52de5ca52a6b4439e7c0f69b34f6331926bb4/codex-rs/core/src/agent/control/spawn.rs)、`wait`、`interrupt` 则通过同一控制面观察和改变生命周期。

对应到本项目：

- 每个根请求创建一个 `AgentControl`，通过 `ContextVar` 传给 `task` 工具；
- `PENDING -> RUNNING -> terminal` 状态转换与并发槽绑定，不以“已发出 cancel”代替“执行已退出”；
- 根流关闭会锁住请求树并中断全部后代，关闭后禁止晚到的注册；
- 派生总量、活动并发数和最大深度分别限制，超量调用进入 FIFO 队列而不是被中间件静默删除；
- 协作取消信号只读，生命周期变更必须经过控制面，避免调用方绕过状态机。

当前实现已接管 `task_tool` 与 `CoordinatedAgentWorkflow` 的 planner/researcher/writer 单元，并提供持久运行树、稳定 child thread id、崩溃状态回收和 fenced 并发 lease。它仍不是完整 Codex rollout：模型调用中点不能无损续接，恢复粒度取决于 LangGraph checkpointer，跨主机恢复还要求共享 SQL store 与共享 checkpoint 后端。

---

## 3. 当前架构评估

### 3.1 已完成能力

| 能力 | 当前状态 | 主要模块 |
| --- | --- | --- |
| 标准对话工作流 | 已有 | `backend/application/agent_workflows/standard_agent_workflow.py` |
| 多智能体协同 | 已有 | `backend/application/agent_workflows/coordinated_agent_workflow.py` |
| 子智能体委派 | 已有 | `backend/core/tools/task_tool.py` |
| 上下文状态提升 | 已有 | `backend/core/agents/middlewares/context_pruning.py` |
| token-aware JIT | 已有 | `backend/core/agents/context/ledger.py` |
| 结构化 handoff | 已有 | `backend/core/agents/context/subagent_adapter.py` |
| Skill 权限绑定 | 已有 | `backend/application/skills/runtime_resolver.py` |
| Tool policy | 已有 | `backend/application/tools/tool_assembly_pipeline.py` |
| Workflow registry | 已有 | `backend/config/workflows/registry.yaml` |
| 请求树生命周期控制 | 已有 | `backend/core/agents/control.py` |
| 请求内循环/无进展检测 | 已有 | `backend/core/agents/middlewares/loop_detection.py` |
| 流式 ContextVar 隔离 | 已有 | `backend/application/agent_executors/streaming_runtime.py` |

### 3.2 主要问题

1. **Agent 角色定义已收敛到 manifest**

   `task` 工具和 `CoordinatedAgentWorkflow` 均通过 `backend/config/agents/runtime_agents.yaml` / `RuntimeAgentLoader` 读取运行时角色。当前 manifest 覆盖 `planner`、专业研究分支、`report_writer`、`reviewer`、`deep-research` 和 `general-purpose`。Python 中仍保留 `research_specialists.py` 的角色别名与兜底映射，用于把 planner 输出的泛化 `agent_type` 归一到 manifest 角色。

2. **运行时预算已有统一抽象，仍需按需扩展**

   `AgentRuntimeBudget` 位于 `backend/domain/agents/runtime_budget.py`，集中解析协调工作流并发、超时、事件队列、partial handoff 以及可选 tree token/tool 上限。`CoordinatedAgentWorkflow` 在实例创建时捕获不可变快照，显式注入的预算优先；模块常量仅保留为旧测试/内部导入兼容别名，不再读取环境变量。运行期扣减由 `AgentRuntimeManager` 以 token/tool 原子账本执行，标准图、轻量循环和直接协调工具均接入。

3. **Context Artifact 已具备本地/MinIO 后端**

   `ContextArtifactStore` 协议、本地 `ArtifactStore`、`MinioContextArtifactStore`、`FallbackContextArtifactStore`、metadata sidecar 和 `context_artifacts` 表写入已落地。默认仍是本地 `/tmp/langchain_context_artifacts`，生产需配置 `CONTEXT_ARTIFACT_STORE=minio` 并完成元数据表迁移。

4. **Handoff V2 已在生产边界做强类型校验**

   兼容层仍接受旧 TypedDict/text 输入，但发布与持久化前统一经过 strict Pydantic V2 model 校验；未知字段、coverage 不一致和结构错误会被拒绝。长 handoff 使用字段级压缩，始终保留完整 XML envelope 与可解析 JSON，而不再截断整个结构。

5. **Trace 已成为一等运行档案**

   `AgentTraceStoreProtocol` 同时支持原 JSONL、`IndexedAgentTraceStore` 和共享 `SQLAlchemyIndexedAgentTraceStore`。索引只保存 run/ordinal/kind/event id/sequence/unit id/hash，对完整 `{kind,payload}` 做 SHA-256 内容寻址并写入本地或 MinIO blob；写入顺序为 blob durable 后提交索引，支持 event_id 幂等、并发 ordinal、重开恢复和完整性校验。CLI、API、Recorder、SSE 与 writer 均通过同一 factory 选后端；共享 SQL 后端由 `202607110002_add_agent_trace_index_tables` 迁移提供。

6. **循环检测已改为请求状态派生**

   `LoopDetectionMiddleware` 只分析当前用户轮次尾部的工具调用和有意义结果集合：相同参数连续调用、或结果集合连续不增长时告警/硬停止。不同参数、新结果和新用户轮次都会重置进度判断，不再因匿名请求共享全局 `default` 桶而串扰。

---

## 4. 目标架构

```text
┌──────────────────────────────────────────────────────────────┐
│                         AgentService                         │
│  request normalization / skill resolution / workflow routing  │
└───────────────────────────────┬──────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│                       Agent Runtime                           │
│  AgentManifest + RuntimeBudget + RuntimePolicy + Trace         │
└───────────────┬───────────────────────┬──────────────────────┘
                │                       │
                ▼                       ▼
┌─────────────────────────┐   ┌───────────────────────────────┐
│ Context Runtime          │   │ Tool Runtime                   │
│ - ContextLedger          │   │ - ToolRegistry                 │
│ - ContextArtifactStore   │   │ - ToolPolicy                   │
│ - JIT Assembly           │   │ - MCP Adapter                  │
│ - Memory Index           │   │ - RuntimeToolInvoker           │
└───────────────┬─────────┘   └────────────────┬──────────────┘
                │                              │
                ▼                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    Workflow Orchestrator                      │
│  plan_solve / deep_research / parallel / alternating           │
└───────────────┬───────────────────────┬──────────────────────┘
                │                       │
                ▼                       ▼
┌─────────────────────────┐   ┌───────────────────────────────┐
│ Subagent Pool            │   │ Writer / Verifier              │
│ - researcher             │   │ - consumes handoff only         │
│ - table analyst          │   │ - verifies evidence matrix      │
│ - citation verifier      │   │ - final answer / report         │
└─────────────────────────┘   └───────────────────────────────┘
```

---

## 5. 核心改造方案

## 5.1 Agent Manifest 配置化

### 5.1.1 已新增配置文件

已新增并投入使用：

```text
backend/config/agents/runtime_agents.yaml
```

示例：

```yaml
agents:
  planner:
    description: 拆解复杂任务，生成 ResearchTask 列表
    model: default
    reasoning_effort: medium
    tool_access: false
    max_turns: 6
    timeout_seconds: 120
    output_schema: ResearchTaskPlan

  deep-research:
    description: 单一研究维度证据检索与分析
    model: default
    reasoning_effort: medium
    tool_access: true
    max_turns: 12
    timeout_seconds: 300
    allowed_tools:
      - multi_kb_search
      - web_search
      - academic_search
      - read_local_skill_file
      - read_skill_reference
      - chat_file_search
    disallowed_tools:
      - task
      - save_deep_research_report
    output_schema: SubagentResult

  report_writer:
    description: 根据 evidence matrix 和 handoff 生成最终报告
    model: default
    tool_access: false
    max_turns: 6
    timeout_seconds: 240
    output_schema: MarkdownReport
```

当前 manifest 还包含 `domain_analyst`、`data_analyst`、`evidence_researcher`、`risk_reviewer`、`solution_architect`、`reviewer` 与 `general-purpose`。引用校验当前由 `backend/application/agent_workflows/evidence_verifier.py` 的 `CitationVerifier` 在 writer 前执行，不是独立 manifest 角色。

### 5.1.2 已新增模块

```text
backend/application/agents/runtime_agent_loader.py
backend/domain/agents/runtime_agent.py
```

`RuntimeAgentSpec` 当前是 dataclass，字段为：

```python
@dataclass(frozen=True)
class RuntimeAgentSpec:
    name: str
    description: str
    system_prompt: str
    allowed_tools: set[str] | None = None
    required_tools: set[str] = field(default_factory=set)
    disallowed_tools: set[str] = field(default_factory=set)
    max_turns: int = 12
    timeout_seconds: int = 300
    model: str | None = None
    reasoning_effort: str | None = None
    tool_access: bool = True
    output_schema: str | None = None
    can_use_network: bool = True
    can_read_kb: bool = True
```

### 5.1.3 迁移点

- `backend/core/tools/task_tool.py` 已通过 `RuntimeAgentLoader` 加载 `RuntimeAgentSpec`；
- `CoordinatedAgentWorkflow._run_unit` 已从 runtime agent manifest / `context.agent_specs` 读取 role 配置；
- workflow task 的 `agent_type` 经 `research_specialists.py` 归一化后映射到 `RuntimeAgentSpec`。

---

## 5.2 统一 AgentRuntimeBudget

### 5.2.1 当前状态

已由 `backend/domain/agents/runtime_budget.py` 中的 `AgentRuntimeBudget.from_env()` 集中解析，并由 `AgentRuntimeBudget.snapshot()` 在 workflow 实例创建时冻结：

- `COORDINATED_RESEARCH_MAX_CONCURRENT`
- `DEEP_RESEARCH_UNIT_TOTAL_TIMEOUT_SECONDS`
- `DEEP_RESEARCH_UNIT_IDLE_TIMEOUT_SECONDS`
- `DEEP_RESEARCH_HEARTBEAT_SECONDS`
- `DEEP_RESEARCH_KB_TIMEOUT_SECONDS`
- `DEEP_RESEARCH_WEB_TIMEOUT_SECONDS`
- `PLAN_SOLVE_MAX_CONCURRENT_SUBAGENTS`
- `COORDINATED_RESEARCH_PER_USER_MODEL_CONCURRENT`
- `DEEP_RESEARCH_EVENT_QUEUE_MAXSIZE`
- `COORDINATED_WORKFLOW_TOTAL_TIMEOUT_SECONDS`
- `DEEP_RESEARCH_PARTIAL_HANDOFF_GRACE_SECONDS`
- `DEEP_RESEARCH_PARTIAL_WRITER_MIN_BRANCHES`
- `DEEP_RESEARCH_PARTIAL_FINALIZATION_WAIT_SECONDS`

工具调用次数、递归限制和上下文 token 预算仍分别由 `RetrievalRuntimePolicy`、LangGraph runnable config 与 `ContextPolicy/LedgerBudget` 管理。

### 5.2.2 已新增结构

```python
@dataclass(frozen=True)
class AgentRuntimeBudget:
    max_concurrent_research: int = 5
    max_concurrent_per_user_model: int = 5
    plan_solve_max_concurrent_subagents: int = 16
    event_queue_maxsize: int = 64
    unit_heartbeat_seconds: int = 10
    unit_idle_timeout_seconds: int = 120
    unit_total_timeout_seconds: int = 300
    kb_search_timeout_seconds: int = 180
    web_search_timeout_seconds: int = 35
    workflow_total_timeout_seconds: int = 420
    partial_handoff_grace_seconds: float = 0.25
    partial_writer_min_branches: int = 1
    partial_finalization_wait_seconds: float = 60.0
```

### 5.2.3 生效规则

当前生效规则：

```text
explicit AgentRuntimeBudget injection
  > workflow-construction-time AgentRuntimeBudget.snapshot()/from_env()
  > built-in dataclass defaults

RuntimeAgentSpec.timeout_seconds / max_turns
  = role-specific additional limits
```

`CoordinatedAgentWorkflow` 的兼容模块常量只服务旧测试和内部导入，不再在 import 时读取环境。`AgentRuntimeContext.runtime_budget` 字段已预留，但当前 registry 主链路尚未把该字段自动注入 workflow；需要请求级覆盖时应显式传入构造参数，后续再统一接入 registry factory。

### 5.2.4 预期收益

- `AgentRuntimeBudget` 统一协调工作流的时间、事件队列和并发成本；
- 委派树 fan-out、活动槽位和深度由 `AgentControlLimits` 独立约束；
- 工具次数与上下文 token 继续由各自的请求级 policy 管理；
- 前端可展示“本次深度研究预算”；
- trace 可记录预算使用率。

---

## 5.3 上下文压缩升级

### 5.3.1 五层上下文模型

沿用现有 `docs/dynamic_context_management_design.md` 的五层模型：

| 层级 | 内容 | 生命周期 | 默认进模型 |
| --- | --- | --- | --- |
| L0 | 对话历史 | Thread | 是，受 summarization 控制 |
| L1 | 工作记忆 | Turn/Unit | 是 |
| L2 | 结果记忆 | Thread | 由 ContextLedger 选择 |
| L3 | 交接包 | Workflow | 只给相关下游 |
| L4 | 原始结果 | Artifact | 按需读取 |

### 5.3.2 ContextArtifactStore 生产化

当前接口已落地为同步协议，便于在 tool/result sink 路径中直接使用：

```python
class ContextArtifactStore(Protocol):
    def store(
        self,
        content: str,
        *,
        thread_id: str | None = None,
        source_tool: str = "unknown",
        suffix: str = ".txt",
        attributes: dict[str, Any] | None = None,
    ) -> str: ...

    def read(self, raw_ref: str | None, *, max_chars: int | None = None) -> str | None: ...
    def metadata(self, raw_ref: str | None) -> ContextArtifactMetadata | None: ...
```

建议存储后端：

- MinIO 存 raw content；
- MySQL 存 metadata；
- Redis 可选做短期 cache。

当前落地实现：

- `ArtifactStore` 和 `MinioContextArtifactStore` 采用同步协议，便于在现有 tool/result sink 路径中无侵入接入；
- `CONTEXT_ARTIFACT_STORE=minio` 时优先写 MinIO，失败后由 `FallbackContextArtifactStore` 回落本地；
- `CONTEXT_ARTIFACT_METADATA_DB=1` 时 best-effort 写入 `context_artifacts` 表；代码默认关闭，迁移完成后 `run_agent_api.sh`/`agent_api_service.sh` 已默认开启，仍可通过环境变量覆盖；
- `read_context_artifact` 工具按 `max_chars` 受限读取 raw ref，供 writer 或调试 replay 按需拉取原始大结果。

本地 sidecar / MinIO metadata 字段：

```text
raw_ref
thread_id
source_tool
size_chars
sha256
suffix
created_at
attributes
```

### 5.3.3 JIT 注入策略

当前 `ContextLedger` 已有分桶预算并在 JIT 组包后写入 trace。当前行为：

- 对超大结果只注入摘要、claim 和 `raw_ref`；
- `ContextPruningMiddleware` 在模型调用前生成 `<dynamic_context_pack>`，并记录 `selected_ids`、分类用量和预算；
- `read_context_artifact` 工具可按 `max_chars` 有界读取 `artifact://context/...` 原文；
- `ContextGovernor` 仍保留为旧 `ContextItem` 调用方兼容适配器。

### 5.3.4 压缩质量指标

每轮记录：

```json
{
  "base_tokens": 12000,
  "dynamic_token_budget": 6000,
  "dynamic_tokens_used": 4200,
  "items_total": 64,
  "items_injected": 12,
  "usage_by_category": {
    "skills": 800,
    "retrieval": 2400,
    "handoff": 700,
    "tool_result": 300
  },
  "dropped_high_score_items": 3
}
```

---

## 5.4 Handoff 强校验

### 5.4.1 当前状态

`SubagentResult` 使用 `TypedDict` 表达 Handoff V2。`build_subagent_result()` 从最终答案、预检索证据和结构化 evidence records 中抽取 claims / refs；`normalize_subagent_result()` 支持模型 JSON、代码块 JSON 和文本 fallback；`validate_subagent_result()` 输出轻量 `validation_warnings`。这已经形成“机器可读 JSON 交接块 + fallback 文本抽取”的落地形态，但尚未迁移为 Pydantic 强 schema。

### 5.4.2 新版 SubagentResult

```python
class SubagentResult(TypedDict, total=False):
    schema_version: str = "2.0"
    subagent_type: str
    task_id: str
    description: str
    summary: str
    claims: list[ClaimRecord]
    evidence_refs: list[str]
    confidence: float
    conflicts: list[str] = []
    missing: list[str] = []
    risks: list[str] = []
    suggested_next_actions: list[str] = []
    raw_artifact_refs: list[str] = []
    source_coverage: SourceCoverage
```

`SourceCoverage`：

```python
class SourceCoverage(TypedDict, total=False):
    total_claims: int
    claims_with_refs: int
    unsupported_claims: int
    kb_refs: int
    web_refs: int
    project_data_refs: int
    raw_artifact_refs: int
```

### 5.4.3 Writer 输入约束

Writer prompt 输入只允许：

- 原始用户任务；
- planner plan；
- evidence matrix；
- `SubagentResultV2[]`；
- verification result。

禁止输入：

- 子智能体完整 tool logs；
- 子智能体多轮中间回答；
- 未结构化的大段检索原文。

---

## 5.5 多智能体协同升级

### 5.5.1 工作流类型

保留现有四类：

- `full`
- `plan_solve`
- `deep_research`
- `multi_agent_parallel`
- `multi_agent_alternating`

建议新增两类内部模式：

| 模式 | 用途 |
| --- | --- |
| `review_then_write` | writer 前增加 verifier/reviewer |
| `research_with_partial_writer` | 高优分支完成后先产出阶段性报告，剩余分支继续补充 |

### 5.5.2 调度策略

当前已有 `select_next_wave`。建议调度器增加：

- `priority`：高优任务先执行；
- `dependency`：依赖满足才执行；
- `resource_class`：区分 LLM、KB、Web、SQL；
- `failure_policy`：失败后跳过、降级、重试或触发 verifier；
- `partial_handoff_policy`：达到最小证据覆盖后可先写阶段性结果。

### 5.5.3 子智能体深度

默认规则：

```text
root agent depth = 0
direct subagent depth = 1
subagent depth >= 1 时禁用 task 工具
```

这条约束现已由 `AgentControlLimits.max_depth=1` 在运行时强制执行，同时 child graph 以 `subagent=False` 构建且 manifest 禁止 `task`，形成控制面与工具面的双重防线。深度属于请求树控制面，不并入 `AgentRuntimeBudget`。

### 5.5.4 模型分层

建议按角色选择模型：

| 角色 | 模型策略 |
| --- | --- |
| planner | 中高推理，低工具 |
| researcher | 中等推理，工具能力强 |
| citation-verifier | 快速模型，低推理 |
| risk-reviewer | 中高推理，无工具 |
| writer | 高质量输出模型 |
| fallback compiler | 无模型或低成本模型 |

---

## 5.6 Skill Progressive Disclosure

### 5.6.1 Skill Index

当前 skill resolution 返回可用技能列表。建议将可见信息限制为：

```json
{
  "name": "nuclear-electronics",
  "label": "核电子学",
  "description": "用于核电子学问题的专业规则和检索指导",
  "allowed_workflows": ["full", "deep_research"],
  "required": true
}
```

完整 `SKILL.md` 必须通过工具读取。

### 5.6.2 Skill Reference 分层

Skill 包建议结构：

```text
SKILL.md
references/
  retrieval.md
  formulas.md
  report_style.md
scripts/
  validate_input.py
```

模型先读 `SKILL.md`，只有当 `SKILL.md` 指向 reference 且任务需要时，才读对应 reference。

### 5.6.3 Skill 预算

`ContextLedger` 中 `skills` 当前有单独预算。建议增加：

- required skill 优先；
- 当前 workflow skill 优先；
- 最近读取且与 query overlap 高的 skill 片段优先；
- 大 skill 只保留 rule summary，reference 通过 raw ref 按需读取。

---

## 5.7 在线服务 Tool Runtime

### 5.7.1 ToolSpec 增强

当前 `ToolSpec` 已有 `risk_level`。建议扩展：

```yaml
tools:
  web_search:
    provider: backend.application.tools.providers.async_web_search_tool
    description: 联网搜索
    instructions: |
      用于补充时效性信息。不得替代已选知识库中的权威证据。
      返回结果必须包含 URL 和标题。
    risk_level: network
    side_effect: external_read
    timeout_seconds: 35
    idempotent: true
    parallel_safe: true
    max_concurrency: 8
    max_attempts: 2
    retryable_errors: [timeout, connection, rate_limited, unavailable]
    max_output_chars: 30000
    cache_enabled: true
    cache_ttl_seconds: 300
```

已落地形态：

- `ToolSpec` 已包含 `instructions`、`timeout_seconds`、`side_effect`、`policy_source`、幂等、并发、有限重试、输出上限和缓存字段；
- `backend/config/tools/registry.yaml` 已为 `web_search`、`academic_search`、`read_context_artifact`、`save_deep_research_report`、HTML/PPT 与 Excel 工具补齐可审计元数据；
- `PolicyToolAssembler` / `ToolAssemblyPipeline` 会把 registry 元数据挂到 LangChain tool metadata；
- `RuntimeToolInvoker` 会读取 metadata，统一进入 timeout、有限重试、并发控制、trace 与数据权限 guardrail 链路。

### 5.7.2 Tool Instructions 注入

工具说明不应全部塞进 system prompt。建议：

- 初始只注入工具名和一句话用途；
- 当前 agent 选中某工具时，JIT 注入该工具的 `instructions`；
- 调用前由 middleware 校验用户、知识库、项目和副作用范围。

已落地形态：

- 工具调用 trace 已记录 `risk_level`、`side_effect`、`policy_source`、`timeout_seconds`、幂等、并发和重试策略；
- `RuntimePolicyGuardrailProvider` 统一校验 allowlist、知识库授权、领域授权、禁用风险等级与副作用白名单；
- 单个可选依赖不可用时工具标记为 `unavailable`，同一策略中的其他工具继续装配。

### 5.7.3 MCP 接入优先级

优先 MCP 化：

1. Web / academic search；
2. 文件系统和 artifact；
3. 表格/数据分析；
4. 外部文档和平台连接器；
5. 浏览器自动化或可视化测试。

已落地形态：

- `backend/application/tools/mcp_adapter.py` 新增 `McpToolDescriptor`、`descriptor_from_spec`、`descriptor_from_tool` 与 `descriptors_for_policy`；
- Web、academic、artifact 工具可从 ToolRegistry 直接导出 MCP-like descriptor，包含输入 schema、provider、超时、并发、重试、缓存和副作用信息；
- `backend/application/mcp/tool_catalog.py` 已可按 `agent_code` / `policy_name` / `domain` / `workflow` / `selected_skills` 输出工具目录；
- `create_platform_mcp_app` 已新增 `list_available_tools` 工具，外部 MCP 客户端可先发现当前服务绑定智能体的 policy tools；
- `/api/v1/mcp/tools` 已提供同样的 HTTP catalog 入口，便于前端、运维和自动化测试审计工具暴露面；
- 当前 adapter 是“描述层/治理层 + discovery 入口”，真实远程 tool execution、独立 MCP server 与外部连接器替换可以在不改变 runtime policy 的前提下渐进完成。

---

## 5.8 Run Trace、Replay 与评估

当前已落地最小可用骨架：

- `backend/application/agent_trace/models.py` 定义 run、event、unit、tool_call、context_pack、handoff 的可序列化记录；
- `backend/application/agent_trace/store.py` 提供 JSONL 追加、读取、列举和覆盖写入能力；
- `backend/application/agent_trace/recorder.py` 提供可选 `AgentTraceRecorder`，让 workflow 在 trace 关闭时保持 no-op；
- `backend/application/agent_trace/replay.py` 支持不调用模型/工具的 dry-run 统计回放；
- `backend/cli.py` 支持 `python -m backend.cli agent replay --run-id xxx --mode dry-run`；
- `StandardAgentWorkflow` 已在 `trace_enabled=True` 时写入 run 和用户可见 event；
- `CoordinatedAgentWorkflow` 已在 `trace_enabled=True` 时写入 run、用户可见 event、unit 和 handoff；
- `RuntimeToolInvoker` 已在 `trace_enabled=True` 时写入 tool_call 元数据、耗时、参数预览、结果预览和错误摘要；
- `SSEEncoder` 已支持按 `AGENT_TRACE_ENABLED` / `AGENT_TRACE_SSE_ENABLED` 自动记录 `sse.*` 出站事件；
- `ContextPruningMiddleware` 已在 JIT 组包后写入 context_pack 预算、注入条目和分类用量 trace；
- `EvidenceWriterStage` 已在 no-tool writer 阶段写入 `writer.input` 和 `writer.output` 摘要 trace；
- `agent replay` 已支持 dry-run、tool-cache、full 离线 replay 模式；
- `agent eval` 已支持 JSONL 回归集离线评分；
- `EvalHistoryStore` 已支持 eval 结果 JSONL 历史记录、pass_rate 趋势查询与版本对比；
- `agent eval --record --version`、`agent eval-history`、`agent eval-compare` 已支持 CLI 评估闭环；
- `/api/v2/agent-runtime/runs/{run_id}`、`/events`、`/units`、`/tool-calls`、`/context-packs`、`/handoffs` 和 `/replay` 已提供认证后的 trace 调试入口，普通用户只能读取自己的 run，管理员可审计全部 run；
- `AgentRuntimeContext` 已预留 `run_id`、`trace_enabled`、`runtime_budget`、`agent_specs`、`context_policy` 字段。

后续生产化增强：

- trace 从 JSONL 升级到数据库/对象存储组合；
- eval 指标接入定时任务、趋势报表和 prompt/workflow 发布门禁；
- 前端 run trace 可视化与按步骤 drill-down。

### 5.8.1 新增 Trace 表

建议新增：

```text
agent_runs
agent_run_events
agent_run_units
agent_run_tool_calls
agent_run_context_packs
agent_run_handoffs
```

### 5.8.2 agent_runs

```text
run_id
request_id
user_id
thread_id
agent_code
workflow
model_name
selected_skills
tool_policy
status
started_at
ended_at
total_tokens_estimated
total_tool_calls
error_code
```

### 5.8.3 agent_run_units

```text
unit_id
run_id
parent_unit_id
role
agent_type
task_title
task_question
status
started_at
ended_at
timeout_seconds
tool_access
```

### 5.8.4 agent_run_context_packs

```text
pack_id
run_id
unit_id
model_call_index
base_tokens
dynamic_token_budget
dynamic_tokens_used
items_total
items_injected
selected_context_ids
usage_by_category
```

### 5.8.5 Replay

新增命令：

```bash
python -m backend.cli agent replay --run-id xxx --mode dry-run
python -m backend.cli agent replay --run-id xxx --from-unit researcher_2
python -m backend.cli agent eval --dataset docs/evals/deep_research.jsonl
python -m backend.cli agent eval --dataset docs/evals/deep_research.jsonl --record --version v1
python -m backend.cli agent eval-history --dataset docs/evals/deep_research.jsonl
python -m backend.cli agent eval-compare --baseline-version v1 --candidate-version v2
```

Replay 模式：

| 模式 | 行为 |
| --- | --- |
| `dry-run` | 不调用模型，只重放事件和上下文包 |
| `tool-cache` | 工具结果从 trace 读取，生成可复用缓存 |
| `full` | 离线复现 run/unit/event/handoff/context/tool-cache 结构；CLI 默认不重调外部模型或工具 |

---

## 6. 数据与接口设计

## 6.1 AgentRuntimeContext 增强

当前 `AgentRuntimeContext` 已包含 user、profile、workflow、kb、permissions。建议新增：

```python
runtime_budget: AgentRuntimeBudget
run_id: str
trace_enabled: bool
agent_specs: dict[str, RuntimeAgentSpec]
context_policy: ContextPolicy
```

## 6.2 SSE / Trace 事件增强

落地策略：用户可见 SSE 事件保持兼容稳定；高频、调试型 runtime 事件默认进入 JSONL trace，并通过 `/api/v2/agent-runtime/runs/{run_id}` 系列接口按需查询。后续若前端需要实时 drill-down，可在不改变默认聊天流的前提下增加调试订阅开关。

新增事件：

```text
runtime.budget.created
runtime.budget.updated
context.pack.created
context.artifact.stored
handoff.created
handoff.verified
trace.persisted
```

示例：

```json
{
  "type": "context.pack.created",
  "payload": {
    "unit_id": "research:1",
    "items_total": 38,
    "items_injected": 8,
    "dynamic_tokens_used": 2300,
    "usage_by_category": {
      "retrieval": 1500,
      "skills": 500,
      "handoff": 300
    }
  }
}
```

## 6.3 API 增强

新增调试接口，已基于 JSONL trace 落地为认证后可用；普通用户只能读取自己的 run，管理员可审计全部 run：

```text
GET  /api/v2/agent-runtime/runs/{run_id}
GET  /api/v2/agent-runtime/runs/{run_id}/events
GET  /api/v2/agent-runtime/runs/{run_id}/units
GET  /api/v2/agent-runtime/runs/{run_id}/tool-calls
GET  /api/v2/agent-runtime/runs/{run_id}/context-packs
GET  /api/v2/agent-runtime/runs/{run_id}/handoffs
POST /api/v2/agent-runtime/runs/{run_id}/replay
```

---

## 7. 迁移路线

## Phase 1：配置化和预算治理

目标：不改变用户体验，先把硬编码运行参数收拢。

任务：

- [x] 新增 `RuntimeAgentSpec` 和 `runtime_agents.yaml`；
- [x] 迁移 `_SUBAGENT_CONFIGS`；
- [x] 新增 `AgentRuntimeBudget`；
- [x] 将 workflow 中分散 timeout/concurrency/tool budget 的默认值汇入预算对象；
- [x] workflow 实例创建时捕获不可变预算快照，取消 import-time 环境读取；
- [x] 新增请求树级 `AgentControl`，统一总派生数、活动槽、最大深度、等待、终态与级联取消；
- [x] `task` 委派接入 `AgentControl`，超出并发的调用公平排队且不重复获取旧 semaphore；
- [x] 流式执行改由稳定 producer task 驱动，保证跨 `__anext__` Task 的请求上下文连续性；
- [x] 循环检测改为从当前用户轮次的消息和结果进度派生，移除进程级共享历史；
- [x] trace 先提供 run metadata 和 unit lifecycle 的可序列化模型；
- [x] 协调工作流可在 `trace_enabled=True` 时自动写入 JSONL trace；
- [x] 标准工作流可在 `trace_enabled=True` 时自动写入 JSONL trace；
- [x] `RuntimeToolInvoker` 工具调用元数据写入 JSONL trace。

验收：

- 原有 `deep_research`、`plan_solve` 行为不回退；
- 子智能体 max_depth 固定为 1；
- 配置新增一个 agent role 不需要改 `task_tool.py`。

## Phase 2：上下文 Artifact 生产化

目标：大结果不再依赖本地 `/tmp`。

任务：

- [x] 新增 `ContextArtifactStore` 协议；
- [x] 本地 `ArtifactStore` 增加 metadata sidecar、原子写入、安全路径清洗和受限读取；
- [x] 实现 `MinioContextArtifactStore`；
- [x] MySQL 增加 artifact metadata 表；
- [x] `ResultSink` 写入新 store；
- [x] `ContextLedger` 注入 raw ref metadata；
- [x] 新增 artifact read tool。

验收：

- 容器重启后历史上下文 raw ref 可读；
- 不同用户不能读彼此 artifact；
- 大工具结果不进入 checkpoint 消息原文。

## Phase 3：Handoff V2 与 Verifier

目标：最终 writer 只基于结构化证据写作。

任务：

- [x] `SubagentResult` 增加 V2 字段：`schema_version`、`task_id`、`raw_artifact_refs`、`source_coverage`、`validation_warnings`；
- [x] researcher handoff 输出机器可读 JSON 块；
- [x] parser 失败时 fallback 到现有文本抽取；
- [x] handoff evidence matrix 加 source coverage；
- [x] writer 前增加 `citation-verifier` 强校验。

验收：

- 每条 claim 有 confidence；
- unsupported claims 可统计；
- writer prompt 不包含 tool logs。

## Phase 4：Trace / Replay / Eval

目标：智能体运行可回放、可评估、可优化。

任务：

- [x] 增加 trace JSONL 模型、store 与 recorder；
- [x] 标准工作流 run/event 写入 JSONL trace；
- [x] 协调工作流 run/event/unit/handoff 写入 JSONL trace；
- [x] `RuntimeToolInvoker` tool_call 元数据写入 JSONL trace；
- [x] SSE 出站事件写入 JSONL trace；
- [x] JIT context pack 预算和注入条目写入 JSONL trace；
- [x] writer 输入输出摘要写入 JSONL trace；
- [x] CLI dry-run replay；
- [x] tool-cache/full 离线 replay；
- [x] 建立 deep_research 回归集样例与 `agent eval` CLI。
- [x] eval 历史记录、趋势查询和版本对比 CLI。

验收：

- 任意 run 可查看 planner、branches、writer 输入输出；
- 可用 tool-cache 模式复现工具缓存并审计最终答案来源；
- 可统计平均工具调用、超时率、引用覆盖率。

## Phase 5：MCP-like Tool Runtime

目标：工具层标准化，对外部系统统一接入。

任务：

- [x] ToolSpec 增加 instructions、timeout、side_effect、幂等、并发、重试、输出上限和缓存策略；
- [x] RuntimeToolInvoker 注入工具 instructions/timeout 元数据；
- [x] 工具调用统一经过数据权限、风险等级和副作用范围 guardrail；
- [x] web / academic / artifact tool 优先 MCP-like descriptor 化。
- [x] 统一普通工具与结构化知识库工具的并发、超时和有限重试边界；
- [x] 增加进程级工具健康与执行统计，并通过管理员接口暴露。

验收：

- 工具策略可从配置完整解释；
- 工具调用 trace 包含风险等级和策略来源；
- 编排层无直接 provider 绕过。
- `GET /api/v2/agent-runtime/admin/tool-runtime` 可查看当前 worker 观测到的健康状态、调用量、错误、超时、重试和平均排队/执行时间。

## Phase 6：统一 AgentControl 生产化

目标：让标准循环和多智能体工作流运行在“整棵线程树共享、可持久、可预算、可中断、可恢复”的同一控制面上。

任务：

- [x] 定义 `AgentRuntimeManager`、`AgentStateMachine`、`RunBudget` 与树级 cancellation token；
- [x] `task_tool` 改为调用 manager，提供 `spawn/wait/interrupt/list` 且保持原输出；
- [x] planner/researcher/writer 协调单元迁移到 manager，并使用稳定 child thread id 接入 checkpointer；
- [x] 统一 EventEnvelope V2，保留 legacy SSE wire format；
- [x] 将本地协调 semaphore 替换为带 TTL/renew/fencing 的 memory/Redis lease；
- [x] Handoff 发布/持久化边界切换为 strict structured output；
- [x] 增加内存、SQLite 运行树 store，以及终态 CAS、连续 lifecycle sequence、重启 hydrate/recovery；
- [x] 标准流式循环和协调工作流在拥有 manager 时持久化根运行终态；
- [x] Trace 增加数据库索引 + 内容寻址对象 blob 后端；
- [ ] 在部署环境启用共享 SQL store、Redis lease 和共享 checkpointer，并完成多 worker 故障注入压测；
- [x] 将 token/tool/deadline 消耗全部并入一个可原子扣减的 tree budget，并将用量快照写入运行树元数据用于重启恢复；
- [ ] 前端增加运行树、恢复记录和 trace sequence 缺口的 drill-down 页面。

恢复语义：

- `pending` 节点可直接被新 worker 认领；
- 崩溃遗留的 `running/interrupting` 不恢复不存在的协程或本地槽，而是记一条 `agent.recovered` 后回到 `pending`；
- 已终态节点不可回退，重复终态写入幂等；
- 同一个 child thread id 由 LangGraph checkpointer 决定从哪个 checkpoint 继续；
- root 完成前会中断并排空当前后代，未被新 worker 认领的历史非终态节点会被收敛为 `interrupted`。

---

## 8. 测试计划

### 8.1 单元测试

新增或扩展：

```text
backend/tests/test_runtime_agent_manifest.py
backend/tests/test_agent_runtime_budget.py
backend/tests/test_context_artifact_store.py
backend/tests/test_subagent_handoff_v2.py
backend/tests/test_agent_trace_replay.py
backend/tests/test_agent_control.py
backend/tests/test_task_tool_agent_control.py
backend/tests/test_streaming_runtime_agent_control.py
backend/tests/test_loop_detection_middleware.py
backend/tests/test_coordinated_runtime_budget_snapshot.py
backend/tests/test_agent_runtime_manager.py
backend/tests/test_agent_control_persistence.py
backend/tests/test_agent_runtime_store.py
backend/tests/test_agent_runtime_manager_factory.py
backend/tests/test_resource_lease.py
backend/tests/test_coordinated_resource_lease_integration.py
backend/tests/test_event_envelope_v2.py
backend/tests/test_indexed_agent_trace_store.py
```

当前落地状态：上述文件均已存在。新增聚焦测试覆盖请求树状态机、取消后执行租约、FIFO 并发、根关闭闩锁、最大深度、跨 Task 流式上下文、重复调用无进展、新用户轮次隔离和预算实例快照；原有 `test_context_governance.py` 仍保留更宽的集成覆盖。

### 8.2 集成测试

场景：

- full workflow 单轮问答；
- deep_research 多分支；
- plan_solve 有依赖任务；
- KB 超时触发熔断；
- 大工具结果进入 artifact；
- writer 只消费 handoff；
- trace replay 复现。

### 8.3 质量指标

| 指标 | 目标 |
| --- | --- |
| worker 重启后任务可恢复率 | 按故障注入样本统计，持续提升 |
| 子任务取消传播延迟 | P95 可观测并设发布门禁 |
| 重复工具调用率 | 可统计并逐步下降 |
| unsupported claim 比例 | 可统计并逐步下降 |
| 每个成功答案 token/tool 成本 | 按 workflow/model 分层统计 |
| partial handoff 成功率 | 可统计并建立基线 |
| 多 worker 实际并发 | 不超过 global/user-model lease 限额 |
| trace 完整率与 sequence 连续率 | 目标 100%，缺口立即告警 |
| replay 成功率 | > 95% |

---

## 9. 风险与应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 配置化后行为漂移 | 旧工作流表现变化 | Phase 1 保持默认值与旧代码一致 |
| Handoff JSON 不稳定 | writer 输入缺失 | JSON parser + repair + 文本 fallback |
| Artifact 权限问题 | 数据泄漏风险 | user_id/thread_id 双重校验 |
| Trace 数据量大 | 存储压力 | 大字段入 MinIO，DB 只存 metadata |
| 多智能体成本升高 | 费用和延迟增加 | RuntimeBudget + AgentControl + role model 分层 |
| 工具 MCP 化改动大 | 迁移周期长 | 先 MCP-like adapter，再逐步替换 |

---

## 10. 推荐优先级

最高优先级：

1. 在部署环境启用共享 SQL runtime store、Redis fenced lease 与共享 checkpointer；
2. 完成 worker kill/restart、取消传播和多 worker 并发故障注入压测；
3. 将 token/tool/deadline 消耗统一并入 tree budget，并写入验收指标；（代码已完成，待部署压测校准阈值）
4. 为 trace sequence 缺口、lease 丢失和恢复失败增加告警。

中优先级：

5. 对象 blob 切换到 MinIO adapter 并增加生命周期/保留策略；
6. Skill progressive disclosure 强化；
7. 前端运行树与 trace drill-down。

后续增强：

8. 真实外部 MCP server / remote transport 执行替换；
9. eval 定时任务、趋势报表和 prompt/workflow 发布门禁；
10. 前端 run trace 可视化。

---

## 11. 总结

当前系统已不再只是第一层内存治理：Agent Manifest 管理角色，`AgentRuntimeBudget` 管理工作流时限，统一 `AgentRuntimeManager` 管理整棵委派树，持久 store 负责重启恢复，fenced lease 负责跨 worker 并发，strict Handoff 与 EventEnvelope 负责边界契约，索引+对象 Trace 负责复盘与完整性验证。

下一步不应继续增加 Agent 角色，重点是部署和量化这个控制面：共享 SQL/Redis/checkpointer、统一 token/tool/deadline 扣减、可鉴权取消接口、故障注入与多 worker 压测，以及由验收指标驱动的发布门禁。

最终目标是让后端智能体具备 Codex CLI 那种长期可演进能力：不是单次回答更聪明，而是每个 workflow、skill、tool、agent role 都能被配置、观测、复盘和迭代。
