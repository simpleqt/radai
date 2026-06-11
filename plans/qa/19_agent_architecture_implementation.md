# 19 · 智能体架构与实现（专题）

> 本模块**只谈 Agent**：AgentService 编排、WorkflowRegistry、ProfiledGraphBuilder、Middleware 链、Lightweight 实现、子 Agent、流式骨架。  
> 📊 图表：[00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 4–8；源码对照：[agent_engine_module.md](../../docs/docs_backend/agent_engine_module.md)

---

## 架构总览

```text
┌───────────────────────────────────────────────────────────────┐
│                             API 层                             │
│ agent_runtime.py (/agent-runtime/)                             │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                       编排层 AgentService                      │
│ Profile 加载 + WorkflowRegistry 工作流解析                    │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                       工作流层 Workflows                        │
│ StandardAgentWorkflow (lightweight/full)                       │
│ CoordinatedAgentWorkflow (deep_research/plan_solve/...)       │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                        执行层 Executors                         │
│ ProfiledAgentExecutor + ProfiledGraphBuilder                  │
│ LightweightQAAgent（绕过 LangGraph 工具循环）               │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                        引擎层 core/agents                        │
│ create_agent_graph  │  ThreadState + Reducers  │  Middleware 链 │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                              流式层                              │
│ StreamingRuntime + StreamHandler  │  AgentEvent + SSEEncoder    │
└───────────────────────────────────────────────────────────────┘

连接:
  agent_runtime.py ──────► AgentService.run_stream()
  AgentService ──────► WorkflowRegistry.resolve_name()
  StandardAgentWorkflow ──(lightweight)──► LightweightQAAgent
  StandardAgentWorkflow ──(full)──► ProfiledAgentExecutor → ProfiledGraphBuilder
  CoordinatedAgentWorkflow ──(deep_research)──► 大纲 → 并行子 Agent
  ProfiledGraphBuilder ──────► create_agent_graph + Middleware 链
  StreamingRuntime ──────► AgentEvent → SSEEncoder
```

---

## L1 初级 🟢

### Q1：`create_agent_graph()` 是唯一建图入口吗？参数有哪些？

**一句话结论：** 生产路径**统一**走 `backend/core/agents/factory.py` 的 `create_agent_graph()`；它包装 LangChain 的 `create_agent()`，用 `RuntimeFeatures` 或全量 `middleware` 组装图。

#### 核心参数

| 参数 | 作用 |
|------|------|
| `model` | Chat 模型实例 |
| `tools` | 业务工具列表 |
| `system_prompt` | 系统提示词 |
| `features` | 声明式 RuntimeFeatures（与 middleware 二选一） |
| `extra_middleware` | 额外中间件（如 ContextPruning） |
| `middleware` | **全量接管**中间件链（不能与 features 同用） |
| `state_schema` | 默认 `ThreadState` |
| `checkpointer` | AsyncSqliteSaver |
| `name` | 图名称日志用 |

#### 互斥规则（面试必考）

```python
# 三者不能乱组合
middleware  + features        → ValueError
middleware  + extra_middleware → ValueError
features    + extra_middleware → ✅ 推荐（Full Agent 用法）
```

---

### Q2：`RuntimeFeatures` 有哪些开关？默认值是什么？

**一句话结论：** 五个特性 flag 控制中间件与内置工具是否挂载；**guardrail/summarization 无内置默认**，必须传 `False` 或自定义 Middleware 实例。

| 字段 | 默认 | 为 True 时 |
|------|------|------------|
| `clarification` | True | ClarificationMiddleware + ask_clarification 工具 |
| `vision` | False | ViewImageMiddleware |
| `subagent` | False | SubagentLimitMiddleware |
| `summarization` | False | 须传入 SummarizationMiddleware 实例 |
| `guardrail` | False | 须传入 ToolGuardrailMiddleware 实例 |

**NE FullFeature 典型：**

```python
RuntimeFeatures(
    summarization=self._create_summarization_middleware(),  # 实例
    vision=True,
    subagent=SubagentLimitMiddleware(...) if enabled else False,
    clarification=(not deep and not lightweight),
    guardrail=ToolGuardrailMiddleware(provider=AllowlistProvider(...)),
)
```

---

### Q3：工厂默认中间件链的 8 层顺序是什么？为什么这个顺序？

**一句话结论：** 顺序在 `_assemble_from_features()` 硬编码：**Dangling → Guardrail → ToolError → Summarization → Vision → SubagentLimit → LoopDetection → Clarification（最后）**。

```text
┌──────────────────┐
│ DanglingToolCall │
└──────────────────┘

  DanglingToolCall ──────────────► M1

  M1 ──────────────► M2

  M2 ──────────────► M3

  M3 ──────────────► M4

  M4 ──────────────► M5

  M5 ──────────────► M6

  M6 ──────────────► M7

  M7 ──────────────► Node

```

| 顺序原则 | 说明 |
|----------|------|
| Guardrail 在 Tool 前 | 非法 tool_call 先拦 |
| ToolError 紧跟 | 工具异常变 ToolMessage，不崩图 |
| LoopDetection 在 Clarification 前 | 死循环先断，再允许追问 |
| Clarification 最后 | 追问可**跳出**主循环等用户 |

**ContextPruning 不在工厂链内**，由 `ProfiledGraphBuilder` 经 `extra_middleware` 注入。

---

### Q4：Lightweight 路径如何与 Full 路径共享外层契约？

**一句话结论：** 新架构中 `StandardAgentWorkflow` 统一编排 lightweight 和 full，根据工作流类型选择 `ProfiledAgentExecutor(lightweight_mode=True)` 或 `LightweightQAAgent`；域差异通过 YAML AgentProfile 驱动，无需每个域独立 Agent 类。

#### 好处

- AgentService / WorkflowRegistry 无需分叉两套 Agent 类
- 会话 thread_id、configurable 注入逻辑统一在 StandardAgentWorkflow 中
- 将来 lightweight 与 full 切换只需改 Profile 的 `default_workflow`

#### 代价

- lightweight 仍会初始化 ProfiledAgentExecutor（有开销），但 LightweightQAAgent 执行时**不走**图的工具循环

---

### Q5：`ThreadState` 相对标准 `AgentState` 多了哪些字段？

**一句话结论：** 扩展字段支撑 **JIT 组装、多模态、工件追踪**；Reducer 保证 checkpoint 合并正确。

| 字段 | Reducer | 用途 |
|------|---------|------|
| `loaded_skills` | dict 合并 | Skill 文件内容 |
| `retrieved_evidence` | list 追加 | RAG 块 |
| `artifacts` | list 去重 | 报告/PPT id |
| `viewed_images` | dict 合并 | 视觉多模态 |
| `thread_data` | — | workspace/uploads 路径 |

源码：`backend/core/agents/state.py`

---

## L2 中级 🟡

### Q6：`ContextPruningMiddleware` 的三个钩子分别做什么？

**一句话结论：** **`after_agent`** 扫描 ToolMessage 做 State Elevation；**`before_model` / `wrap_model_call`** 做 JIT 注入；`contextvars` 保证 patched messages 只影响当次 LLM 调用。

```text
┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│  Tool Node   │ │ContextPruning│ │ Graph State  │ │     LLM      │
└──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘

  [Note CP,LLM: 下一轮 model 调用]
  [Note LLM: 调用结束 JIT 不持久化]

```

| 工具名 | 提升目标 |
|--------|----------|
| `multi_kb_search` | `retrieved_evidence` |
| `read_local_skill_file` | `loaded_skills[skill_name]` |
| 其他超长输出 | 通用截断 > char_threshold |

环境变量：`JIT_MAX_EVIDENCE_BLOCKS=2`，`JIT_MAX_EVIDENCE_CHARS=8000`

---

### Q7：`LightweightQAAgent.aquery_stream` 内部步骤是什么？（不走 LangGraph 工具循环）

**一句话结论：** **读 checkpoint 历史 → 生成/复用检索 query → `execute_multi_kb_search` → 拼 prompt → 单次 `create_chat_model().astream()` → 写回 checkpoint**。

```text
┌──────────────────────────────────────┐
│ aquery_stream                        │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ Hist                                 │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ RQ                                   │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ KB                                   │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ WS                                   │
└──────────────────────────────────────┘
         (是)
           │
           ▼
         (否)
           │
           ▼
┌──────────────────────────────────────┐
│ Web                                  │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ Prompt                               │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ LLM                                  │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ Save                                 │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ 经外层 Handler 转 SSE                    │
└──────────────────────────────────────┘

```

#### 与 full 的本质区别

| | lightweight | full |
|---|-------------|------|
| 工具调用 | 内联 Python 函数 | LangGraph Tool Node |
| Middleware | 不经过 ToolGuardrail 链 | 完整 8 层 + ContextPruning |
| 多轮 tool | 无 | ReAct 循环 |
| Prompt | `LIGHTWEIGHT_ANSWER_TEMPLATE` | 完整 System + Skill |

#### 特殊：`explore__` thread 前缀

`use_direct_query=True` — 探索式学习场景优化，跳过部分 query 生成逻辑（见 `ne_full_feature_agent.py` L606）。

---

### Q8：`ProfiledAgentExecutor.aquery_stream` full 模式骨架是什么？

**一句话结论：** 配置 thread → 重置 registry/budget → 创建 `StreamingRuntime` + `StreamHandler` → `graph.astream_events()` 循环 → Handler 映射 SSE → finally 清理 budget。

#### 关键步骤（源码 L617+）

1. `reset_registry()` — 图片 URL 注册表
2. `set_tool_budgets({multi_kb_search: N, web_search: 10, ...})` — 请求级限流
3. `StreamHandler(is_deep_research=...)`
4. `async for event in self._graph.astream_events(...)`
5. Handler 产出 chunk/status/event
6. `reset_tool_budgets()` in finally

#### deep_research 模式差异

- `multi_kb_search` budget 放宽为 3/轮
- `clarification=False`
- Handler `is_deep_research=True` 影响超时与事件

---

### Q9：`StreamingRuntime` / `StreamHandler` 与 `AgentService` 的职责如何划分？

**一句话结论：** **`AgentService` = Profile 加载 + 工作流解析 + 生命周期管理**；**`StreamingRuntime` = 图循环 + 超时 + budget**；**`StreamHandler` = LangGraph 事件 → SSE 协议**（thinking 剥离、tool 事件、artifact）。

| 职责 | 所在 |
|------|------|
| Profile 加载、工作流解析 | AgentService |
| thread_id / configurable | StreamingRuntime |
| lightweight 委托 | StandardAgentWorkflow |
| astream_events 循环 | StreamingRuntime |
| 首 token 延迟日志 | StreamingRuntime |
| on_chat_model_stream → chunk | StreamHandler |
| tool_start/end → event | StreamHandler |
| thinking 标签状态机 | StreamHandler |
| AgentEvent 标准化 | domain/agents/events.py |
| SSE 编码 | SSEEncoder |

---

### Q10：`task` 工具如何派发子 Agent？

**一句话结论：** `core/tools/task_tool.py` 根据 `_SUBAGENT_CONFIGS`（如 `deep-research`、`general-purpose`）在**隔离上下文**里 `create_agent_graph` 跑子图，结果通过 `stream_writer` 回传主流程。

```text
┌──────────────────────────────────────┐
│ CFG                                  │
└──────────────────────────────────────┘
           │
           ▼
           │
           ▼
┌────────────────┐  ┌─────────────────┐
│ DR             │  │ general-purpose │
└────────────────┘  └─────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ SubGraph                             │
└──────────────────────────────────────┘
           │
           ▼
           │
           ▼
┌────────────────┐  ┌───────────────────┐
│ SW             │  │ allowed_tools 白名单 │
└────────────────┘  └───────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ Main                                 │
└──────────────────────────────────────┘

```

#### deep-research 子 Agent 规则（节选）

- **允许：** multi_kb_search, web_search, read_local_skill_file...
- **禁止：** task（禁止嵌套）, ask_clarification, save_deep_research_report
- max_turns=12, timeout=600s

#### 面试点

子 Agent 白名单是**静态 dict**，Skill 新增工具不会自动穿透 — 架构已知限制。

---

### Q11：`ToolAssemblyPipeline.assemble()` 输入输出是什么？

**一句话结论：** 输入会话上下文（skills、kb_ids、use_search、subagent_enabled…），输出 **`list[BaseTool]`** 注入 `create_agent_graph`。

#### lightweight 特殊行为

```python
# application/tools/tool_assembly_pipeline.py
if lightweight_mode:
    return []  # 工具列表为空；检索在 LightweightQAAgent 内联
```

#### full 模式典型工具

- 动态闭包 `multi_kb_search`（注入 kb_ids）
- `web_search` / `academic_search`（按 flag）
- Skill 声明工具（html_ppt_tool 等）
- `read_local_skill_file` / `read_skill_reference`
- `task`（subagent_enabled）
- **移除** sandbox bash/read_file（生产默认）

---

### Q12：`DanglingToolCallMiddleware` 解决什么问题？

**一句话结论：** 模型输出**未闭合或悬空**的 tool_call（JSON 截断、格式错误）会在进 Tool Node 前被修复或丢弃，避免 LangGraph 整图异常。

#### 背景

流式 + 长上下文下，偶发 `tool_calls` 数组不完整 → Tool Node 找不到 id → 图 hang 或 500。

#### 位置

工厂链 **第 0 层**，最先执行。

---

### Q13：`ToolErrorHandlingMiddleware` 与 Guardrail deny 有何不同？

**一句话结论：** **Guardrail deny** = 策略不允许调用（未执行）；**ToolErrorHandling** = 工具**已执行但抛异常**（网络超时、Milvus 错误），格式化为 error ToolMessage 还给 LLM。

| | Guardrail | ToolErrorHandling |
|---|-----------|-------------------|
| 时机 | 执行前 | 执行后 catch |
| 典型原因 | 不在 Allowlist | RAG 超时 |
| LLM 看到 | denied 文案 | 工具报错摘要 |

---

### Q14：`@Next` / `@Prev` 装饰器如何插入 `extra_middleware`？

**一句话结论：** 给 Middleware 类标注锚点，`_insert_extra()` 在工厂链中**相对 Clarification 或指定类**插入；ContextPruning 通常无锚点，插在 Clarification **之前**。

```python
@Next(LoopDetectionMiddleware)
class MyMiddleware(AgentMiddleware):
    ...
```

文档建议：个性化 Middleware **不要改 factory.py 顺序**，用 `extra_middleware` + 锚点（见 agent_engine_module 后续开发方向）。

---

### Q15：`set_tool_budgets` / `request_limits` 如何限制工具调用？

**一句话结论：** **请求级 ContextVar**：`aquery_stream` 开始时 `set_tool_budgets({"multi_kb_search": 2, ...})`，工具内递减，超限 raise 或返回友好错误；finally 重置。

| 模式 | multi_kb 典型上限 |
|------|-------------------|
| NE 普通 | MULTI_KB_SEARCH_NORMAL_MAX_CALLS（如 2） |
| deep_research | 3 |
| CSH | 可更高（5） |

与 **LoopDetectionMiddleware** 互补：budget 限次数，Loop 限相同调用模式。

---

## L3 高级 🔴

### Q16：为何 lightweight 不挂载 `ToolGuardrailMiddleware` 仍安全？

**一句话结论：** Lightweight **没有 LangGraph tool_call 面** — 检索/联网是 Python 内联调用，模型无法 hallucinate 任意工具名；风险转为 **Prompt 注入影响检索 query**，靠 Guardrails 输入层与 KB 权限缓解。

#### 对比

| 攻击面 | full | lightweight |
|--------|------|-------------|
| 非法 tool_call | Allowlist 拦 | 无 tool_call |
| 恶意检索 query | multi_kb 内联 | 同左 |
| 联网 | use_search flag | 同左 |

---

### Q17：`SummarizationMiddleware` 与 `ContextPruning` 会不会冲突？

**一句话结论：** 两者都减 history，但机制不同 — **Summarization** 用 LLM 摘要替换旧 messages；**ContextPruning** 保留 messages 骨架、证据进 state。同时开启时需验证 checkpoint 里是否出现 `lc_source=summarization` 与 JIT 占位符并存；NE 默认注入 Summarization 实例（约 20 轮触发）。

#### 排障

用户反馈「历史消息变模糊」→ 查 Summarization 是否触发  
「检索细节丢失」→ 查 JIT 裁剪 env

---

### Q18：深度研究编排与 Full Agent 的关系？

**一句话结论：** 深研通过 `CoordinatedAgentWorkflow` 实现，包含大纲生成 → 用户确认 → 并行子 Agent 检索 → 报告生成；**不是**独立的 orchestrator 类，而是 WorkflowRegistry 解析 `deep_research` 工作流时创建 CoordinatedAgentWorkflow 实例。

```text
┌──────────────────┐
│ Draft            │  大纲生成阶段（SSE draft_chunk）
└──────────────────┘
    │
    ▼
┌──────────────────┐
│ Confirm          │  用户确认大纲
└──────────────────┘
    │
    ▼
┌──────────────────┐
│ Background       │  并行子 Agent 执行（task 工具）
└──────────────────┘
    │
    ▼
┌──────────────────┐
│ Report           │  save_deep_research_report 生成报告
└──────────────────┘
```

工作流注册：`backend/config/workflows/registry.yaml` 中 `deep_research` 条目，provider 为 `create_coordinated_workflow`。

---

### Q19：checkpointer 的 `thread_id` 与 `build_internal_thread_key` 在 Agent 层如何用？

**一句话结论：** 客户端 `thread_id` 写入 `configurable`；登录用户组合为 **`u_{user_id}__t_{thread_id}`**；访客 **`g_{device_id}__t_{thread_id}`** — Agent 与 conversations API 必须用同一规则。

#### Agent 侧

`aquery_stream` 设置：

```python
effective_config["configurable"]["thread_id"] = effective_thread_id
```

内部 key 转换在 checkpointer 配置或 thread_key 工具函数中完成（见 `core/agents/thread_key.py`）。

---

### Q20：如何实现「只改配置不改代码」的域定制？

**一句话结论：** 新架构通过 **YAML AgentProfile** 驱动域定制：新建 `config/agents/env.yaml` + Prompt 模板（`prompt_templates/`），AgentService 自动加载，无需创建任何 Python 子类。

#### 最小域接入清单

1. `config/agents/env.yaml`（AgentProfile 配置）
2. `prompt_templates/env_*.py`（Prompt 模板）
3. KB 绑定 + RetrievalProfile 映射
4. ToolPolicy 配置（可选）

AgentService / WorkflowRegistry / ProfiledAgentExecutor **全部复用**。

---

### Q21：`ViewImageMiddleware` 与 `image_url_registry` 如何配合？

**一句话结论：** 多模态消息经 ViewImage 过滤/注入；模型输出 `[IMAGE:token]` 时，**registry** 在当次请求内映射 token→URL；Handler 解析为前端可访问链接；`aquery_stream` 开头 `reset_registry()` 防跨请求污染。

---

### Q22：`ClarificationMiddleware` 如何「打断」Agent 循环？

**一句话结论：** 模型调用 `ask_clarification` 工具 → Middleware 拦截 → **不再继续 tool loop** → SSE 带 clarification 元数据 → 等待用户下一条消息。

API 层识别（`agent.py`）：`tool_end` + `ask_clarification` → 特殊 event 载荷。

---

### Q23：从 LangChain `create_agent` 到 LangGraph 编译，项目加了什么？

**一句话结论：** 项目不手写 StateGraph 节点，而是用 **LangChain Agents 1.x 的 `create_agent()`**（内部编译 LangGraph），通过 **middleware 链 + ThreadState + checkpointer** 扩展；`ProfiledGraphBuilder` 组装各组件后调用 `create_agent_graph()`。

| 层 | 技术 |
|----|------|
| 编排 | LangGraph（经 create_agent） |
| 中间件 | LangChain AgentMiddleware |
| 状态 | ThreadState TypedDict |
| 持久化 | AsyncSqliteSaver |

---

### Q24：Agent 架构已知限制与演进方向（面试综合）

**一句话结论：** 四条硬限制 + 四条演进线。

#### 已知限制

1. **Skill 不驱动 Workflow** — 仅 Prompt + 工具（但 Skill `preferred_workflow` 可影响解析）
2. **子 Agent 工具白名单静态** — `_SUBAGENT_CONFIGS` dict
3. **主站 think 过滤未完全统一** — P0-3
4. **SQLite checkpoint 多 worker** — 生产瓶颈

#### 演进方向（架构文档）

- SkillWorkflowExecutor
- SubagentRegistry 装饰器注册
- StreamHandler 可插拔事件链
- PostgreSQL checkpointer
- OpenTelemetry 追踪

```text
┌──────────────────────────────────────────┐
│ 现状 AgentService + WorkflowRegistry    │
└──────────────────────────────────────────┘

  现状 ──────────────► T1 SkillWorkflowExecutor

  T1 ──────────────► T2 SubagentRegistry 装饰器注册

  T2 ──────────────► PostgreSQL checkpointer
```

---

### Q25：排障表 — Agent 层常见现象

| 现象 | 查什么 | 期望 |
|------|--------|------|
| 无 Skill 仍很慢 | WorkflowRegistry resolve | lightweight |
| 工具调不到 | Allowlist vs ToolAssembly | 工具名在 allowed_tools |
| 多轮后爆 token | ContextPruning 是否注入 | JIT 日志 |
| 子 Agent 无 web | LocalSubagentConfig | allowed_tools 含 web_search |
| 澄清不出现 | clarification feature | full 且非 deep/lightweight |
| checkpoint 串号 | thread_key | u_user__t_xxx |
| 图片 404 | image_url_registry | 同请求内 reset |

代码入口：`agent_runtime.py` · `agent_service.py` · `factory.py` · `stream_handler.py`

---

## L3 进阶（续）— 深研 / 压缩 / 路由边界

### Q26：DeepResearch 与 Full 的 Agent 层差异

**一句话结论：** 深研走 **CoordinatedAgentWorkflow**（大纲 + 并行子 Agent + 报告）；Full 走 **StandardAgentWorkflow + ProfiledAgentExecutor + LangGraph 工具循环**；WorkflowRegistry 在解析时**最优先**选 `deep_research`（显式 request.workflow 最高优先级）。

| 维度 | Full | DeepResearch |
|------|------|--------------|
| 工作流 | `StandardAgentWorkflow` | `CoordinatedAgentWorkflow` |
| 工具 | ToolAssembly 全量 | 研究专用 + task 工具 |
| Middleware | 工厂链 + ContextPruning | 独立编排，非同一图 |
| SSE | AgentEvent 标准事件 | process/draft_chunk 事件 |
| 触发 | 默认/Skill | 显式 `workflow=deep_research` |

```text
WorkflowRegistry.resolve_name()
        │
        ├── deep_research ──► CoordinatedAgentWorkflow
        ├── full ──► StandardAgentWorkflow → ProfiledAgentExecutor
        └── lightweight ──► StandardAgentWorkflow → LightweightQAAgent
```

**延伸：** 访客 API 拒绝 `deep_research` — 见 `agent_runtime.py`。

---

### Q27：`SKIP_KB` 与 Lightweight 内联检索

**一句话结论：** Lightweight **不挂载** `multi_kb_search` 工具，在 `_prepare_messages` 前 **内联** `execute_multi_kb_search`；模型可用 **`SKIP_KB`** 协议跳过 KB/联网检索。

**流程：**

```text
(empty sequence diagram)

```

**在本项目中的实现：** `lightweight_qa_agent.py` — `_SKIP_KB_TOKEN = "SKIP_KB"`；`ToolAssembly.assemble()` 在 `lightweight_mode=True` 时 **return []**。

**误区：** ❌ lightweight = 无 RAG → ✅ 有 RAG，只是 **无 tool loop**。

---

### Q28：SummarizationMiddleware 与 ContextPruning 分工

**一句话结论：** **Summarization** 在 token 超阈值时 **压缩旧 Human/AI 轮次**；**ContextPruning** 在 **工具返回后** 把 ToolMessage 提升进 state 并 JIT 注入；二者互补、作用对象不同。

| Middleware | 钩子 | 作用对象 |
|------------|------|----------|
| Summarization | before_model | 历史对话轮次 |
| ContextPruning | after_agent + before_model | ToolMessage → evidence/skills |
| ViewImage | before_model | 多模态 image block |

**Full 模式：** 两者常并存；Lightweight **不走** 工厂 Middleware 链（委托 `LightweightQAAgent` 单次 LLM）。

---

### Q29：`StreamHandler` / `SSEEncoder` 协议转换职责

**一句话结论：** 把 LangGraph **`astream_events` 原始 chunk** 通过 `StreamHandler` 转为中间事件，再由 `AgentEvent` 标准化，最后 `SSEEncoder` 编码为前端 SSE 协议（`message.delta` / `workflow.step` / `reasoning.delta` / `error`）。

```text
┌──────────────────────┐
│ graph.astream_events │
└──────────────────────┘

  graph.astream_events ──────────────► Raw

  Raw ──────────────► NEH

  NEH ──────────────► SSE

  SSE ──────────────► RadAI / CSH 前端

```

**Lightweight 对比：** 不经过 Handler，直接 `yield {"type":"chunk",...}` 与 `event` 进程事件。

**已知债：** P0-3 — think 过滤在 Handler 与 middleware 路径未完全统一。

---

### Q30：从 `agent_engine_module.md` 到源码的映射表

| 文档章节 | 源码锚点 | 面试一句 |
|----------|----------|----------|
| 端到端路径 | `api/routers/agent_runtime.py` | POST → AgentService → WorkflowRegistry → Executor |
| 编排服务 | `application/agent_service.py` | Profile 加载 + 工作流解析 |
| 建图工厂 | `core/agents/factory.py` | `create_agent_graph` + Middleware 链 |
| ThreadState | `core/agents/state.py` | Reducer 合并 evidence/skills |
| 工作流 | `config/workflows/registry.yaml` | 6 种工作流 YAML 驱动 |
| Profile | `config/agents/ne.yaml` 等 | YAML AgentProfile 域配置 |
| Full | `application/agent_executors/profiled_executor.py` | ProfiledGraphBuilder + ContextPruning |
| Lightweight | `application/agent_executors/lightweight_qa_agent.py` | 内联 KB + 单次 astream |
| 子 Agent | `core/tools/task_tool.py` | `_SUBAGENT_CONFIGS` 白名单 |
| Checkpoint | `checkpoints.db` | `u_{user}__t_{thread}` |
| 事件 | `domain/agents/events.py` | AgentEvent 标准化 |
| SSE | `application/event_stream/sse_encoder.py` | SSEEncoder 编码 |

**用法：** 面试官说「对照你们文档讲实现」— 按上表逐节 1 分钟；详见 [24_agent_engine_doc_mapping.md](./24_agent_engine_doc_mapping.md)。

---

## 附录：源码阅读顺序（Agent 专项）

1. `api/routers/agent_runtime.py` — API 入口
2. `application/agent_service.py` — AgentService 编排
3. `application/agent_workflows/registry.py` — WorkflowRegistry 工作流解析
4. `application/agent_executors/profiled_executor.py` — ProfiledAgentExecutor
5. `application/agent_executors/graph_builder.py` — ProfiledGraphBuilder
6. `core/agents/factory.py` — 建图 + Middleware 链
7. `core/agents/middlewares/context_pruning.py` — JIT
8. `core/tools/task_tool.py` — 子 Agent
9. `application/agent_executors/streaming_runtime.py` + `stream_handler.py` — SSE
10. `application/tools/tool_assembly_pipeline.py` — 工具装配
11. `domain/agents/events.py` — AgentEvent 标准化
12. `application/event_stream/sse_encoder.py` — SSEEncoder
