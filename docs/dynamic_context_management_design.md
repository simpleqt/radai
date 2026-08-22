# 动态上下文与多智能体交接设计

## 1. 问题

旧实现把大 `ToolMessage` 替换成“结果已截断”的通知。知识库和项目工具有专用状态承接，
但通用工具没有结果存储层，因此最后一次模型调用可能只看到通知，看不到真正结果。

当前实现已经补齐通用结果存储层：`ContextEngine` 统一创建 `ResultSink`、捕获适配器、
`ContextLedger`、`HistoryCompactor` 和 Artifact Store。大工具结果被转换成 `EvidenceRecord`，
超过阈值的原文写入 `ContextArtifactStore`。默认本地路径为
`CONTEXT_ARTIFACT_ROOT`（默认 `/tmp/langchain_context_artifacts`），生产可通过
`CONTEXT_ARTIFACT_STORE=minio` 启用 `MinioContextArtifactStore`，并可通过
`CONTEXT_ARTIFACT_METADATA_DB=1` best-effort 写入 `context_artifacts` 元数据表。

另一个问题是协同工作流直接拼接前序智能体的完整文本。它没有明确区分：

- 当前智能体需要执行工具；
- 当前智能体只需要前序结论；
- 当前智能体需要证据引用，但不需要工具调用过程；
- 原始大结果只应在确有需要时进入提示词。

## 2. 设计原则

1. 裁剪消息不等于删除结果。
2. 对话历史、工具结果、工作记忆和多智能体交接分别管理。
3. 原始大结果放在提示词之外，JIT 阶段按任务相关性恢复。
4. 智能体之间默认传结果，不传思维过程和工具执行轨迹。
5. 上下文预算在组装阶段执行，不在工具返回阶段粗暴截断。
6. 来源差异只存在于捕获适配器；预算、窗口、去重、物化和工具可见性不按来源写分支。
7. 渲染文本只是模型输入，不能再被系统反向解析为控制状态。

## 3. 统一组件边界

| 组件 | 唯一职责 | 不负责 |
| --- | --- | --- |
| `ContextPolicy` | 根据模型窗口和配置解析所有预算 | 保存状态、渲染提示词 |
| `ContextCaptureRegistry` | 把不同工具输出规范化为 `CapturedContext` / `EvidenceRecord` | 历史压缩、Top-K、工具开关 |
| `ContextStateAdapter` | 在一个边界读取旧 checkpoint 字段并转换为规范记录 | 新状态持久化、模型请求组装 |
| `ContextEngine` | 一次创建并共享 Store、Sink、Capture、Ledger、Compactor 和 StateAdapter | LangGraph reducer、SSE 推送 |
| `ContextLedger` | 去重、排序、最近原文窗口、token 预算、精确请求编译 | Graph State 兼容、工具调用策略 |
| `ContextPruningMiddleware` | 把 LangGraph 生命周期事件接到 `ContextEngine`，更新 reducer 和 trace | 再实现一套上下文算法 |

完整版、轻量智能体和 Writer 都从 `ContextEngine` 获取同一组组件。来源专用处理仍可扩展，
但只能实现捕获协议；不能在执行器里自行创建另一套预算、缓存或原文窗口。

## 4. 五层上下文

| 层级 | 内容 | 生命周期 | 是否默认进入模型 |
| --- | --- | --- | --- |
| L0 对话历史 | 用户消息、最终回答、必要的工具占位消息 | Thread | 是，受 `HistoryCompactor` / `ContextPolicy` 控制 |
| L1 工作记忆 | 当前目标、角色、工具权限、临时计划 | Turn/Unit | 是，按角色注入 |
| L2 结果记忆 | `ContextItem`：摘要、关键词、来源、重要度、原文引用 | Thread | 由 ContextLedger 选择 |
| L3 交接包 | 结论、证据引用、风险、下一步 | Workflow | 只给下一个相关智能体 |
| L4 原始结果 | 大工具输出、报告、表格和检索原文 | Artifact | 仅在选中后读取片段 |

## 5. 单智能体 JIT 流程

```text
ToolMessage
    -> ToolCall wrapper 在 ToolNode checkpoint 前调用 ContextCaptureRegistry
    -> ResultSink 持久化大结果并生成规范 EvidenceRecord
    -> 大原文写入 ContextArtifactStore
    -> ToolMessage 替换为短 Result Capture 占位
    -> ContextStateAdapter 只在旧 checkpoint 恢复时转换兼容字段
    -> ContextLedger.compile_request 统一计算静态指令、历史、工具 schema 与动态证据
    -> 根据当前用户问题、证据质量和类别排序并在 token 预算内选择 Top-K
    -> 先选择紧凑证据，再按模型剩余 token 物化当前轮检索原文
    -> 保守 token 估算复核；超限时从最低优先级证据逐项回退
    -> ContextPack.capabilities 决定请求级工具可见性
    -> 仅注入本次 ModelRequest 的 SystemMessage
```

`ContextItem` 保存：

- `kind`：检索证据、分析结果、子智能体结果等；
- `source_tool`：结果来源；
- `summary`：无额外模型调用的头尾保真摘要；
- `keywords`：用于相关性召回；
- `importance`：业务重要度；
- `raw_ref`：原始结果的外部引用；
- `scope`、`turn_id`、`tags`：生命周期和过滤信息。

当前排序综合使用查询相关性、证据分数、类型优先级和时间衰减。`ContextGovernor` 仅作为旧
`ContextItem` 调用方的兼容适配器；生产模型请求统一由 `ContextLedger` 组装，避免字符预算
与真实模型窗口不一致。完整版 `ContextPruningMiddleware`、`LightweightQAAgent` 和 Writer
共享 `ContextEngine`；需要生成模型请求的路径统一调用 `ContextLedger.compile_request()`，
不再分别维护 history fit、JIT 拼装和预算回退逻辑。
当 tokenizer 在静态 prompt 与 JIT 后缀拼接边界产生额外 token 时，编译器优先删除排序最低的
已选记录；只有所有可选记录都无法容纳时才移除整个动态包。

`read_context_artifact` 工具提供有界原文恢复：输入 `artifact://context/...` 的 `raw_ref` 和
`max_chars`，最多读取 50000 字符，并返回 metadata、截断状态和内容。模型不应直接拼接服务器路径。

### 5.1 最近检索原文窗口

- 窗口大小由执行模型的剩余 token 动态计算，不使用固定“检索次数 × 固定字符数”；
- 候选必须属于当前用户轮次，并按 `retrieval_seq` 取最近的不同 `raw_ref`；默认最多占动态
  预算的 20%，同时受最大条数限制；旧 checkpoint 中没有 `turn_id` 的记录仅走兼容路径；
- 原文进入窗口后生成内部 `materialized_ref`，渲染为 `<loaded_retrieval_context>`，不再向模型暴露
  `raw_ref` 或内部 `ev_*`/`ctxmat_*` 标识；
- 物化记录是紧凑证据的补充，不能删除同一 `raw_ref` 下已经选中的结构化 chunk；物化成功后
  仅隐藏重复的内部 ID 和 Artifact 引用，继续保留紧凑证据正文；物化失败或被预算裁剪时必须
  恢复可读取的紧凑引用记录；
- 超过窗口时，旧原文退出模型请求，但规范记录和 Artifact 引用仍保留，可在真正需要时按引用恢复。

### 5.2 工具可见性

`ContextPack.capabilities` 是工具可见性的唯一依据。系统不解析 XML、标签或模型可见文本来猜测
是否存在 Artifact：

- 选中的记录只有有效 `artifact://context/...` 引用时，才保留 `read_context_artifact`；
- 原文已经物化到窗口时，读取工具对该请求隐藏；
- 不因一次知识库检索完成而设置全局 `tool_choice="none"`；计算、项目查询等其他工具仍可继续使用；
- 后续知识库检索是否允许由独立的请求级工具预算和检索缓存决定，不与上下文注入状态混为一谈。

## 6. 多智能体交接

统一使用 `SubagentResult`：

```json
{
  "schema_version": "2.0",
  "task_id": "req-xxx:research:1",
  "subagent_type": "evidence_researcher",
  "description": "该智能体承担的目标",
  "summary": "可直接消费的结论",
  "claims": [
    {
      "claim": "可核验的单条结论",
      "supporting_refs": ["[doc#chunk-1]"],
      "confidence": 0.78,
      "conflicts": [],
      "missing": [],
      "source_ids": ["doc#chunk-1"]
    }
  ],
  "evidence_refs": ["文献、URL、文件或结果引用"],
  "confidence": 0.78,
  "conflicts": [],
  "missing": [],
  "risks": ["不确定性和冲突"],
  "suggested_next_actions": ["仍需验证的事项"],
  "raw_artifact_ref": null,
  "raw_artifact_refs": [],
  "source_coverage": {
    "total_claims": 1,
    "claims_with_refs": 1,
    "unsupported_claims": 0,
    "kb_refs": 1,
    "web_refs": 0,
    "project_data_refs": 0,
    "raw_artifact_refs": 0
  },
  "validation_warnings": []
}
```

格式化传递时使用 `<agent_handoff>` 包裹，并内嵌 `<subagent_result_json>` 机器可读块；旧 XML/text
内容仍可 fallback 解析。`CoordinatedAgentWorkflow` 在分支完成后还会发送
`coordination.partial_handoff` 事件，payload 中直接携带上述 handoff。

### 6.1 并行模式

- `domain_analyst`：按 manifest 配置工具权限，做领域分析和边界澄清；
- `evidence_researcher`：有工具，负责事实核验；
- `risk_reviewer`：按 manifest 配置工具权限，检查遗漏、反例和风险；
- `synthesizer`：无工具，接收领域、数据、证据、风险与方案角色的结构化交接包并收敛。

### 6.2 交替模式

- `analyst`：有工具，建立初始结果；
- `critic`：无工具，只接收 analyst 的结果交接；
- `finalizer`：无工具，只接收 analyst 和 critic 的结果交接。

### 6.3 Plan-Solve / Deep Research

研究分支保留独立工具权限。Writer/Reviewer 不接收各分支的工具消息列表，只接收：

- 分支目标；
- 分支最终结论；
- 可核验证据引用；
- 风险和未决项。

这能避免 Writer 再次消化大量 `tool_start/tool_result` 过程，同时保留最终写作需要的信息。

## 7. 统一策略、预算与生命周期

- `ContextPolicy` 是所有智能体上下文参数的单一事实源；`ContextEngine.create()` 是组件构造的
  单一入口，确保 Store、Sink、Ledger、Compactor 使用同一模型和策略；
- `ContextLedger.compile_request()` 是生产请求的统一上下文编译入口，输出最终 messages、
  system content、context pack、保守估算 token 数、估算方法和剪枝状态；
- 已知 OpenAI 模型使用模型 tokenizer；未知 provider 使用混合语言保守估算，并用服务端
  `usage.input_tokens` 只增不减地校准。若 provider 返回 context overflow，提升安全系数、重新
  编译并仅重试一次；
- `LedgerBudget` 根据主执行模型窗口预留回答 token 和安全余量。历史摘要按该预算的 80%
  触发并保留 25%，不按消息条数触发，也不使用摘要模型窗口；
- `AgentRuntimeBudget` 位于 `backend/domain/agents/runtime_budget.py`，统一派生协同工作流的
  并发、心跳、空闲/总超时、KB/Web 超时、事件队列和 partial handoff 默认值；
- `AGENT_LARGE_RESULT_THRESHOLD_CHARS`：大结果捕获阈值，默认 3000 字符；
- `RESULT_SUMMARY_INPUT_MAX_CHARS` / `RESULT_SUMMARY_MAX_CHARS`：结果总结子智能体输入/输出
  上限，默认 60000/3000 字符；
- `RESULT_SUMMARY_MAX_PER_ROUND`：单轮并发总结上限，最大 5；
- `JIT_CONTEXT_ITEM_TOP_K`：结构化证据基础 Top-K，策略会换算为 ledger 候选上限；
- `JIT_MAX_EVIDENCE_CHARS` / `JIT_MAX_PROJECT_EVIDENCE_CHARS`：仅用于旧状态兼容区，
  结构化证据走 token-aware ledger；
- ThreadState 规范字段为 `context_records`；旧字段只由 `ContextStateAdapter` 有界读取；
- ThreadState 对规范记录和兼容字段分别设置防御性上限，模型请求仍以 token 预算为最终约束；
- handoff bundle 在工作流内单独限制总字符数；
- 原始结果目录由 `CONTEXT_ARTIFACT_ROOT` 配置；生产环境优先使用 `CONTEXT_ARTIFACT_STORE=minio`
  并启用元数据表审计。

当 `trace_enabled=True` 时，`ContextPruningMiddleware` 会把每次 JIT context pack 的
`base_tokens`、`dynamic_token_budget`、`dynamic_tokens_used`、`items_total`、`items_injected`、
`selected_context_ids` 和 `usage_by_category` 写入 agent trace。协调工作流还会记录 unit 与 handoff。

## 8. 状态所有权与迁移

当前仍读取 `loaded_skills`、`retrieved_evidence`、`project_evidence` 和 `context_items`，保证旧
checkpoint 可以恢复；新产生的知识库、项目和通用工具结果统一写入 `context_records`。
`loaded_skills` 暂时保留为 Skill 规则兼容状态。兼容字段只在编译入口转换一次，之后统一进入
token-aware ledger。

规范状态不保存本次渲染后的大 `context_pack`；checkpoint 仅保存记录、Artifact 引用、预算/选择
元数据和压缩 checkpoint。每次模型调用都重新编译临时 pack，因此窗口能随当前问题和模型容量变化。

后续可分两步迁移：

1. 将知识库、项目证据完全切换为 `ContextItem + raw_ref`，停止在 Graph State 中保存完整原文；
2. 在已支持 MinIO/数据库元数据的基础上补齐 TTL、用户隔离校验和引用审计查询。

## 9. 不变量

- 任何被裁剪的大工具结果必须存在 `ContextItem` 或专用状态承接；
- 占位消息必须包含可追踪的 context item id；
- JIT 注入不得写回永久消息历史；
- 所有生产请求必须经过同一个预算编译入口，且 `estimated_tokens <= token_budget`；真实窗口以
  provider usage 与 context-overflow 恢复闭环为准；
- 任何运行路径不得直接构造第二套 Ledger/Sink/Compactor，必须经 `ContextEngine`；
- 旧状态字段只允许在 `ContextStateAdapter` 中转换，不得在中间件和执行器重复拼装；
- 工具可见性只读取结构化 `ContextPack.capabilities`，不得解析渲染文本；
- 已物化的检索原文不得同时携带可再次读取的 `raw_ref` 标签；
- 任何 Artifact 自动读取必须同时通过 `owner_user_id` 和 `owner_thread_id` 校验；缺失作用域时
  自动物化必须 fail closed；
- 大工具结果必须在 ToolNode checkpoint 前完成外置和占位符替换；`before_model` 仅为旧
  checkpoint 提供兼容迁移；
- result-only 角色不得自动获得知识库、搜索和 Skill 工具；
- handoff 不包含模型隐式推理，只包含可消费结果。
