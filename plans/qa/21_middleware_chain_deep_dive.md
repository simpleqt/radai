# 21 · Middleware 链深度解析（智能体专题）

> 逐层拆解 `create_agent_graph` 装配的 **8 层工厂 Middleware** + **ContextPruning**，每题对应一个实现文件。  
> 📊 图 6：[00_architecture_diagrams.md](./00_architecture_diagrams.md) · 源码：`backend/core/agents/factory.py` L101–225

---

## 链路与钩子总览

```text
┌───────────────────────────────────────────────────────────────┐
│                  工厂链 _assemble_from_features                  │
│ DanglingToolCall  │  ToolGuardrail  │  ToolErrorHandling  │   │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                       extra_middleware                        │
│ ContextPruning / after_agent + before_model                   │
└───────────────────────────────────────────────────────────────┘

连接:
  HumanMessage ──────► DanglingToolCall
  Clarification ──────► AgentNode
  AgentNode ──────► ContextPruning / after_agent + before_model
  ContextPruning / after_agent + before_model ──────► AgentNode

```

| 钩子类型 | 哪些 Middleware 用 | 典型时机 |
|----------|-------------------|----------|
| `before_model` | ContextPruning, ViewImage | LLM 调用前改 messages |
| `after_agent` | ContextPruning | 一轮 agent 步结束后扫 ToolMessage |
| `wrap_tool_call` | ToolGuardrail, ToolErrorHandling, Clarification | 单个工具执行前后 |
| `after_model` | LoopDetection, SubagentLimit | 模型输出 tool_calls 后 |

---

## 第 0 层

### Q1：`DanglingToolCallMiddleware` 处理什么异常？

**一句话结论：** 修复或丢弃模型输出中**悬空、未闭合、格式非法**的 `tool_calls`，避免 Tool Node 因找不到有效 call id 而挂死。

#### 在本项目中的实现

- 文件：`core/agents/middlewares/dangling_tool_call.py`
- 位置：工厂链 **always 第 0 层**

#### 典型触发场景

- 流式输出在 JSON 中间截断
- `max_tokens` 耗尽导致 tool_call 数组不完整
- 模型输出重复 half-open 的 function 块

#### 常见误区

- ❌ 以为是 Guardrail → ✅ 是**格式修复**，不检查工具权限

---

## 第 1 层

### Q2：`ToolGuardrailMiddleware` 的 `wrap_tool_call` 流程？

**一句话结论：** 每个 ToolCallRequest 执行前 → `GuardrailProvider.evaluate()` → deny 则返回 error ToolMessage，**不进入工具函数**。

```text
┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐
│LangGraph Tool Node│ │   ToolGuardrail   │ │ AllowlistProvider │ │       工具函数        │
└───────────────────┘ └───────────────────┘ └───────────────────┘ └───────────────────┘

  └─ END

```

- `fail_closed=True`：Provider 异常 → deny
- NE 注入：`AllowlistProvider(allowed_tools=[t.name for t in tools])`

---

## 第 2 层

### Q3：`ToolErrorHandlingMiddleware` 捕获哪些异常？

**一句话结论：** 工具函数**已执行**后的所有异常（网络超时、Milvus 错误、参数 ValueError）→ 格式化为可读 error ToolMessage → LLM 下一轮可改策略。

#### 与 Guardrail 对比

| | Guardrail | ToolErrorHandling |
|---|-----------|-------------------|
| 工具是否运行 | 否 | 是（已尝试） |
| 典型文案 | Guardrail denied | Tool execution failed: ... |

#### 设计意图

避免整图 500；Agent 产品体验是「工具失败仍可继续答」。

---

## 第 3 层

### Q4：`SummarizationMiddleware` 在 Full Agent 如何配置？

**一句话结论：** **无工厂内置默认**；`ProfiledGraphBuilder` 在构建图时显式创建实例并传入 `RuntimeFeatures(summarization=...)`。

#### 典型参数（需读源码确认）

- 触发条件：消息轮次/ token 超阈值（约 20 轮量级）
- 行为：LLM 摘要旧 history，替换为 summarization 标记消息

#### 与 ContextPruning 关系

| 机制 | 作用对象 | 持久化 |
|------|----------|--------|
| Summarization | 旧 messages 全文 | 写入 checkpoint（带 lc_source） |
| ContextPruning | 大 ToolMessage | 提升到 state，history 留占位符 |

两者同时启用时需 UI 过滤 summarization 消息（conversations API 已过滤）。

---

## 第 4 层

### Q5：`ViewImageMiddleware` 做什么？

**一句话结论：** 多模态路径：从 messages 提取图片 → 注入模型可消费格式；配合 **`image_url_registry`** 解析 `[IMAGE:token]`。

- 文件：`core/agents/middlewares/view_image.py`
- 启用：`RuntimeFeatures(vision=True)`（NE Full 默认开）
- Lightweight：可走 `image_contents` 参数，不经过此 Middleware

---

## 第 5 层

### Q6：`SubagentLimitMiddleware` 如何限制并发子任务？

**一句话结论：** 在 **`after_model`** 截断单次 AI 响应里多余的 `task` tool_call，保留前 N 个（默认 3，clamp 2–4）。

```python
# 逻辑 essence
task_indices = [i for tc in tool_calls if tc.name == "task"]
if len(task_indices) > max_concurrent:
    丢弃超出部分的 tool_call
```

#### 为何不在子 Agent 内限

主模型一次响应可能发出 5 个 `task` → 线程池/ token 风暴 → **在进 Tool Node 前截断**。

---

## 第 6 层

### Q7：`LoopDetectionMiddleware` 的 warn 与 hard stop 阈值？

**一句话结论：** 滑动窗口内对 **tool_calls 指纹**（name+args 哈希）计数；达 **warn_threshold=3** 注入警告 HumanMessage；达 **hard_limit=5** 强制停止工具循环。

#### 指纹算法

```python
# _hash_tool_calls: 排序 normalized tool_calls → md5[:12]
```

#### 线程安全

- 内存 `_thread_windows: OrderedDict` + `max_tracked_threads=100` LRU 淘汰

#### 走一遍例子

Agent 连续 5 次 `multi_kb_search` 相同 query → 第 3 次 warn → 第 5 次 hard stop → 强迫输出最终答案。

---

## 第 7 层

### Q8：`ClarificationMiddleware` 如何「打断」流程？

**一句话结论：** 拦截 `ask_clarification` 的 `wrap_tool_call` → 写 Clarification 状态 → 返回 **Command** 跳转/结束本轮，**不继续**后续 tool loop。

- 内置工具：`ask_clarification_tool`（clarification feature 为 True 时注入）
- 禁用条件：deep_research 或 lightweight（ProfiledGraphBuilder 构建时配置）
- SSE：`agent_runtime.py` 识别 `tool_end` + `ask_clarification`

#### 澄清类型

`missing_info` / `ambiguous_requirement` / `approach_choice` / `risk_confirmation` / `suggestion`

---

## 额外层

### Q9：`ContextPruningMiddleware` 为何用 `contextvars`？

**一句话结论：** `wrap_model_call` 里 patched messages **只应影响当次 LLM 调用**，不能写回 checkpoint；`contextvars` 传递临时 message 列表，调用结束即销毁。

- `_current_patched_messages: ContextVar`
- JIT XML 块追加到 SystemMessage **仅内存**

---

### Q10：`@Next` / `@Prev` 插入 `extra_middleware` 的规则？

**一句话结论：** 无锚点的 extra 插在 **Clarification 之前**；有 `@Next(LoopDetectionMiddleware)` 的插在锚点之后；解析失败 raise ValueError。

```python
@Next(LoopDetectionMiddleware)
class ContextPruningMiddleware:
    ...
```

文档建议：个性化 Middleware **勿改 factory 顺序**，用锚点 + extra。

---

## 综合题 L3

### Q11：若去掉 LoopDetection 只留 request_limits 会怎样？

**答案要点：**

- `set_tool_budgets` 限**总次数**，不识别「相同 args 重复」
- 模型可换 slightly different args 绕过 budget
- LoopDetection 专杀**重复模式**；两者互补

---

### Q12：Middleware 链上 Clarification 必须最后的原因？

**答案要点：**

1. 澄清后需**等待用户**，不能继续同轮 tool
2. `_insert_extra` 会把未锚定 extra 插在 Clarification 前
3. ContextPruning 需在 LLM 前 JIT，但 Clarification 后不应再 JIT 旧 evidence

---

### Q13：guest 站跑哪些 Middleware？

**答案要点：**

- guest 强制 lightweight → **不走** 完整 Middleware 图工具循环
- 无 full RuntimeFeatures 链；think 过滤在 guest 路径更完整
- checkpoint：`guest_checkpoints.db`

---

### Q14：如何单测某个 Middleware 而不起 HTTP？

**答案要点：**

1. `create_agent_graph(features=RuntimeFeatures(..., guardrail=False), extra_middleware=[待测])`
2. mock `BaseChatModel` 固定返回 tool_call
3. 断言 state messages / ToolMessage content
4. factory 支持 `middleware=[...]` 全量接管

---

### Q15：Middleware 链面试白板题 — 请按顺序写出 8 层并说一句职责

**参考答案：**

1. DanglingToolCall — 修复悬空 tool_call  
2. ToolGuardrail — Allowlist 拦截  
3. ToolErrorHandling — 工具异常变 ToolMessage  
4. Summarization — 长 history 摘要（NE 自定义实例）  
5. ViewImage — 多模态注入  
6. SubagentLimit — 截断多余 task  
7. LoopDetection — 重复 tool 模式熔断  
8. Clarification — ask_clarification 打断  

**+ extra：** ContextPruning — State Elevation + JIT

---

## 附录：文件 → Middleware 对照表

| 文件 | Middleware |
|------|------------|
| `dangling_tool_call.py` | DanglingToolCallMiddleware |
| `tool_guardrail.py` | ToolGuardrailMiddleware |
| `tool_error_handling.py` | ToolErrorHandlingMiddleware |
| `view_image.py` | ViewImageMiddleware |
| `subagent_limit.py` | SubagentLimitMiddleware |
| `loop_detection.py` | LoopDetectionMiddleware |
| `clarification.py` | ClarificationMiddleware |
| `context_pruning.py` | ContextPruningMiddleware |
| `graph_builder.py` | SummarizationMiddleware 实例化 |
