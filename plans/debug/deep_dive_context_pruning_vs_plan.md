# 深度审查：ContextPruningMiddleware vs 多轮工具上下文方案

> 代码：`core/agents/middlewares/context_pruning.py`（298 行）  
> 规划：[多轮工具调用上下文管理重构方案.md](../多轮工具调用上下文管理重构方案.md)  
> 注入点：`ne_full_feature_agent.py`、`csh_full_feature_agent.py`  
> 只读审查。

---

## 1. 实现概要

`ContextPruningMiddleware` 在 **每次模型调用前**（`before_model` / `abefore_model`）执行：

1. **State Elevation**：`read_local_skill_file` → `state.loaded_skills`，ToolMessage 替换为短占位符  
2. **Context Isolation**：`multi_kb_search` → `state.retrieved_evidence`，同样占位  
3. **Generic Pruning**：其它工具输出 `len > char_threshold`（默认 1500）→ 截断通知  
4. **JIT Assembly**：将 skills + evidence 拼进 **第一条** `SystemMessage`（或新建），仅本次 `wrap_model_call` 可见

环境变量：

| 变量 | 默认 | 作用 |
|------|------|------|
| `JIT_MAX_EVIDENCE_BLOCKS` | 2 | evidence 块数上限 |
| `JIT_MAX_EVIDENCE_CHARS` | 8000 | evidence 总字符上限 |

---

## 2. 与规划文档对齐

| 规划目标 | 实现状态 | 说明 |
|----------|----------|------|
| State Elevation + JIT | ✅ 部分 | 仅 2 个具名工具 + 通用阈值 |
| SummarizationMiddleware | ✅ | 独立中间件，trigger 20 条 messages |
| 统一 Tool Context Archive（MySQL） | ❌ | 无归档表 |
| 按 `max_input_tokens` 动态预算 | ❌ | `resolve_max_input_tokens` 未接入本中间件 |
| 子智能体总结器压缩管线 | ❌ | |
| 不修改对话历史展示语义 | ⚠️ | checkpoint 中 messages **会被改写**占位符；`conversations.get_messages` 过滤 summarization 前缀，**不过滤** `[State Elevation:` |
| `keep_latest_n` 保留最近 N 次工具结果 | ❌ **未实现** | 构造参数存在，**类内从未读取** |

规划文档文首「部分实现」标注 **准确**。

---

## 3. 执行时序

```text
┌────────────────────────┐ ┌────────────────────────┐ ┌────────────────────────┐
│     LangGraph Node     │ │ContextPruningMiddleware│ │       Chat Model       │
└────────────────────────┘ └────────────────────────┘ └────────────────────────┘

  [Note LLM: JIT 拼入 SystemMessage，evidence 仍占 token]

```

---

## 4. 与 Summarization 分工

| 中间件 | 触发 | 作用对象 |
|--------|------|----------|
| `SummarizationMiddleware` | messages ≥ 20 | 旧轮次摘要为一条 AI 消息 |
| `ContextPruningMiddleware` | 每次 model call | 单轮内大 ToolMessage + JIT 注入 |

**叠加效果**：摘要后 checkpoint 仍有占位符 ToolMessage；JIT 再次把 evidence **全文** 注入 SystemMessage — **窗口压力未根本消除**，只是从 messages 挪到 system。

---

## 5. 问题清单

| ID | 严重度 | 问题 |
|----|--------|------|
| CP-1 | **medium** | `keep_latest_n` 死参数 — 与规划「保留最近 N 次工具输出」不符 |
| CP-2 | medium | JIT 将完整 `loaded_skills` / `retrieved_evidence` 写入 SystemMessage — 多 skill 叠加仍可爆窗 |
| CP-3 | medium | `_process_state_elevation` **就地修改** `state["messages"]` 并写回 checkpoint — 不可逆占位，排障困难 |
| CP-4 | medium | 仅处理 `multi_kb_search`，`explore_learning` / `chat_file_search` 等走 generic 1500 截断 — 行为不一致 |
| CP-5 | low | `contextvars` 补丁消息与并发请求 — 同 worker 内 asyncio 任务若共享 context 需验证（LangGraph 通常 per-run） |
| CP-6 | low | `get_messages` UI 可能展示 `[State Elevation:...]` 占位符（若未过滤） |
| CP-7 | info | `lightweight_mode` **仍注入** ContextPruning — 轻量路径也承担 JIT 开销 |

---

## 6. 与 `context_limits.py` 断层

`resolve_max_input_tokens()` 在 `factory.py`、`rag/service.py` 使用，**未**用于：

- `char_threshold` 动态化  
- `JIT_MAX_EVIDENCE_CHARS` 按模型比例缩放  
- Summarization trigger 条数

规划 §2.2 所述「预算基础已存在」— **接线未完成**。

---

## 7. 前端 / 会话 API 联动

`conversations.get_messages` 过滤：

- `metadata.lc_source == summarization`  
- 内容以 `State Elevation` 等开头 — **未在过滤列表中**

若占位符进入 checkpoint，历史面板可能显示系统占位文本。

---

## 8. 整改路线图（对齐规划）

| 阶段 | 动作 |
|------|------|
| P0 | 实现或删除 `keep_latest_n`；`get_messages` 过滤三类占位前缀 |
| P1 | `char_threshold` / JIT caps ← `resolve_max_input_tokens(model) * ratio` |
| P2 | 扩展具名工具表（explore、chat_file_search、bash 输出） |
| P3 | Tool Archive 表 + 按需回放，JIT 只注入摘要 ID |
| P4 | 子智能体压缩作为超阈值后备 |

---

## 9. 建议测试

1. 连续 3 次 `multi_kb_search` — evidence 块数 ≤ `JIT_MAX_EVIDENCE_BLOCKS`  
2. 大 skill 加载后 checkpoint 中 ToolMessage 长度 < 200 字符  
3. 同一 thread reload 后 JIT 仍能从 `loaded_skills` state 恢复  
4. 对比 token 计数：仅 pruning vs pruning+summarization @ 25 轮  
