# 23 · Lightweight vs Full 对照专章

> 20 道对照题，覆盖 **工作流解析、建图、工具、检索、Middleware、流式、Prompt、排障**。  
> 源码：`agent_service.py` · `workflow_registry.py` · `profiled_executor.py` · `graph_builder.py` · `lightweight_qa_agent.py` · `tool_assembly_pipeline.py`

---

## 总览对照图

```text
┌───────────────────────────────────────────────────────────────┐
│            WorkflowRegistry.resolve_name()                    │
│ lightweight  │  full  │  deep_research  │  plan_solve  │  ...  │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                        Full 路径                              │
│ StandardAgentWorkflow → ProfiledAgentExecutor                │
│ → ProfiledGraphBuilder → create_agent_graph                   │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                    Lightweight 路径                             │
│ StandardAgentWorkflow → ProfiledAgentExecutor                │
│ (lightweight_mode=True) → LightweightQAAgent                │
└───────────────────────────────────────────────────────────────┘

连接:
  lightweight (无 Skill, profile default) ──► StandardAgentWorkflow → LightweightQAAgent
  full (有 Skill) ──► StandardAgentWorkflow → ProfiledAgentExecutor → ProfiledGraphBuilder
  deep_research (显式) ──► CoordinatedAgentWorkflow
```

| 维度 | Lightweight | Full |
|------|-------------|------|
| 触发 | Profile `default_workflow` / 无 Skill | 有非空 Skill / 显式 `workflow=full` |
| LangGraph tool loop | ❌ | ✅ |
| multi_kb_search | 内联调用 | 注册为工具 |
| Middleware 8 层 | ❌（委托类不走图循环） | ✅ |
| ContextPruning | ❌ | ✅ |
| Clarification | ❌ | ✅（非 deep） |
| ToolGuardrail | ❌（无工具调用） | ✅ |
| 典型延迟 | 低（1 次 LLM + 预检索） | 高（多轮 tool） |

---

## L1 概念（Q1–Q5）

### Q1：WorkflowRegistry 如何决定走 Lightweight 还是 Full？

**一句话结论：** `resolve_name()` 优先级：**显式 `request.workflow`** > Skill `preferred_workflow` > 有 Skill 则 `full` > `profile.default_workflow` > `lightweight`。

**在本项目中的实现：** `backend/application/agent_workflows/registry.py`；YAML 定义 `backend/config/workflows/registry.yaml`。

**常见误区：** ❌ 看 `use_search` 决定 lightweight → ✅ 看 **`selected_skills` 是否非空** 或 **Profile `default_workflow`**。

---

### Q2：Lightweight 为何仍实例化 `ProfiledAgentExecutor`？

**一句话结论：** **统一入口** — 共享 checkpointer、thread_key、ProfiledGraphBuilder 建图参数、日志与 AgentService 编排；在 `StandardAgentWorkflow` 内 **委托** `LightweightQAAgent`。

**详细解释：**

```text
┌──────────────────┐
│ agent_runtime.py │
└──────────────────┘

  agent_runtime.py ──────────────► AgentService

  AgentService ──────────────► StandardAgentWorkflow

  StandardAgentWorkflow ──────────────► LightweightQAAgent

  AgentService ──(lightweight)──► StandardAgentWorkflow
  StandardAgentWorkflow ──(内部)──► LightweightQAAgent

```

**延伸：** 为何不 AgentService 直接 new LightweightQAAgent？— 减少双份配置、保持域统一通过 YAML Profile 驱动。

---

### Q3：Full 模式的「全功能」指什么？

**一句话结论：** **LangGraph 多轮 tool-call 循环** + **ToolAssembly 动态工具** + **Middleware 链** + **ContextPruning JIT** + **子 Agent / 澄清 / Guardrail**。

**例子：** 用户选 `html-ppt` Skill → WorkflowRegistry 解析 full → 工具含 `multi_kb_search` + Skill 文件读取 + 可能 `task` 子 Agent。

---

### Q4：Lightweight 有没有 RAG？

**一句话结论：** **有** — 在 LLM 调用前 **内联** `execute_multi_kb_search`（或 CSH 等价路径），结果注入 prompt；**不是**通过 tool loop 让模型自己调工具。

**走一遍例子：** 用户问「什么是核电子学」→ 无 Skill → lightweight → 检索词生成 → 内联 KB → 单次 astream 出答案。

---

### Q5：deep_research 与 Lightweight/Full 的关系？

**一句话结论：** 第三档独立路径 — **`CoordinatedAgentWorkflow`**，不走 Full 的 ToolAssembly 图，也不走 Lightweight 单次 LLM。

**判定：** 显式 `request.workflow="deep_research"` 或 `task_type="deep_research"`，由 WorkflowRegistry 解析。

---

## L2 实现（Q6–Q14）

### Q6：`ToolAssemblyPipeline.assemble()` 在 lightweight 下返回什么？

**一句话结论：** **空列表 `[]`** — `if lightweight_mode: return []`。

**为什么：** 检索由 `LightweightQAAgent` 内部完成，避免模型进入 tool loop 重复检索。

**Full 对比：** 按 `selected_skills`、`kb_ids`、`subagent_enabled` 组装 `multi_kb_search`、`web_search`、`task` 等。

---

### Q7：Lightweight 的检索词为何要先经 LLM 改写？

**一句话结论：** `_extract_search_query_v2` / `_extract_file_search_terms` — **避免用用户口语原句直接检索**，提升召回；有上传文件时另走文件检索词分支。

**Full 对比：** 模型在 tool loop 里自己决定何时调 `multi_kb_search(query=...)`，query 由模型生成。

---

### Q8：`SKIP_KB` 协议是什么？

**一句话结论：** Lightweight 内部常量 — 模型在检索决策阶段输出 **`SKIP_KB`** 表示 **跳过 KB/联网**，直接基于对话历史回答（如纯闲聊、已有上下文足够）。

**实现：** `lightweight_qa_agent.py` — `_SKIP_KB_TOKEN = "SKIP_KB"`；`_should_skip_kb_search()` 解析。

**误区：** ❌ 用户可见指令 → ✅ **内部协议**，非对外 API 参数。

---

### Q9：Full 的 `RuntimeFeatures` 在 Lightweight 下如何设置？

**一句话结论：** `ProfiledGraphBuilder` 在 `lightweight_mode=True` 时 **仍建图**（供 checkpoint/history），但 **`clarification=False`**，且 `StandardAgentWorkflow` **不进入** graph.astream_events 主路径。

**对照表：**

| Feature | Lightweight 建图 | Lightweight 运行时 |
|---------|------------------|-------------------|
| guardrail | 可能编译进图 | 不执行（无 tool） |
| summarization | 可能编译 | 用 `_trigger_async_compression` 替代思路 |
| clarification | False | — |
| subagent | 通常 False | — |

---

### Q10：ContextPruning 为何只出现在 Full？

**一句话结论：** Pruning 处理 **ToolMessage 膨胀**；Lightweight **无 ToolMessage**（无 tool loop），证据直接进 prompt，不需要 elevation + JIT。

**数字：** 文档称 JIT 可降 token **70%+** — 仅对多轮 Full 有意义。

---

### Q11：流式事件格式差异

**一句话结论：**

| 模式 | 事件来源 | Handler |
|------|----------|--------|
| Full | `graph.astream_events` | `StreamHandler` + `AgentEvent` + `SSEEncoder` |
| Lightweight | 手写 `yield` | 无 Handler，直接 dict + `AgentEvent` |

**Lightweight 特有：** `event` 类型 `_make_process_event`（如「检索完成」「回答完成」metadata 含 `kb_called`）。

---

### Q12：thinking 模式两者都支持吗？

**一句话结论：** **代码上** Lightweight 传 `thinking_enabled` 给 `create_chat_model`；**访客** 强制 `thinking_enabled=False`。

**Full：** Handler 负责剥离/转发 thinking chunk；Lightweight 路径较简，需单独确认前端展示一致性。

---

### Q13：`prompt_profile` 在 Lightweight 的影响

**一句话结论：** 影响 **轻量 Prompt 模板**（由 `PromptBuilder.build()` 按 Profile 的 `prompt_profile` 字段选择）、文件检索前缀、域专用模板。

**例子：** CSH 水文地质术语在检索词生成 prompt 里用 domain 前缀。

---

### Q14：explore 线程的 Lightweight 特殊逻辑

**一句话结论：** `thread_id.startswith("explore__")` → `use_direct_query=True` — 探索式学习 **直查模式**，跳过部分检索改写逻辑。

**源码：** `lightweight_qa_agent.py`、`profiled_executor.py`。

---

## L3 权衡与排障（Q15–Q20）

### Q15：何时应强制走 Full 而非 Lightweight？

**一句话结论：** 用户 **选了 Skill**、需要 **多工具编排**（web + KB + 子 Agent + 澄清）、或 **多跳推理** 时必须 full。

**产品层：** 前端传 `selected_skills: ["html-ppt"]` 即触发 full；运维勿在 API 层误传空 Skill 数组与非空字符串混用。

---

### Q16：Lightweight 无 ToolGuardrail 是否安全？

**一句话结论：** **合理** — 无工具调用即无越权工具名；风险在 **Prompt 注入** 与 **RAG 内容**，由 RAG 过滤与 Prompt 约束覆盖，非 Guardrail 层。

**Full：** Guardrail 检查 `allowed_tools` 与 AllowlistProvider。

---

### Q17：无 Skill 却很慢 — 排查清单

| 步骤 | 检查 | 期望 |
|------|------|------|
| 1 | 日志 `[WorkflowRegistry] resolved=` | `lightweight` |
| 2 | 是否误带 `selected_skills` | 空或全空字符串 |
| 3 | KB 数量与 `execute_multi_kb_search` | 多库并发耗时 |
| 4 | 是否走了 deep_research | 不应 |

---

### Q18：有 Skill 但像 Lightweight 一样快 — 可能原因

**一句话结论：** 可能 **Skill 列表格式错误** 被 WorkflowRegistry 判 false；或 **解析了 lightweight 分支** 因配置缓存旧代码。

**查：** `workflow_registry.py` 的 skill 非空判定；请求体 JSON 字段名是否为 `selected_skills`。

---

### Q19：Lightweight 与 Full 的 checkpoint 是否共用？

**一句话结论：** **共用** — 同一 `AsyncSqliteSaver` 与 `thread_key`；Lightweight `_save_turn` 写入 messages；Full 由 LangGraph 每步 checkpoint。

**风险：** 同 thread 若先 lightweight 后 full，历史 messages 格式需兼容 — 靠 ThreadState 统一 schema。

---

### Q20：设计题 — 新增「中等复杂度」第四档路由

**一句话结论（参考答法）：** 在 `registry.yaml` 增加 **`standard`** 工作流：有 KB 无 Skill → 内联 KB + **单轮** tool（仅 `multi_kb_search` 一次），不走完整 Middleware 链；权衡是 **配置分叉** vs **延迟**。

```text
┌──────────────────────────────────────┐
│ 用户问题                                 │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ R                                    │
└──────────────────────────────────────┘
         (低)
           │
           ▼
         (中)
           │
           ▼
         (高/有Skill)
           │
           ▼
┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ full           │  │ lightweight    │  │ standard 单工具   │
└────────────────┘  └────────────────┘  └────────────────┘

```

**延伸：** 可用启发式（问题长度、是否含「对比/方案/报告」）或轻量分类模型。

---

## 附录：对照速记

```
Lightweight = 无 Skill → StandardAgentWorkflow → LightweightQAAgent → 内联 RAG → 单次 LLM → 无 Middleware 链
Full        = 有 Skill → StandardAgentWorkflow → ProfiledAgentExecutor → ToolAssembly → LangGraph 循环 → ContextPruning → StreamHandler
Deep        = 显式 deep_research → CoordinatedAgentWorkflow → 后台 TaskManager
```

相关：[19 Q4/Q7/Q16](./19_agent_architecture_implementation.md) · [21 Middleware](./21_middleware_chain_deep_dive.md) · [24 文档对照](./24_agent_engine_doc_mapping.md)
