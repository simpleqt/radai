# 20 · 智能体架构模拟面试卷

> 专用于 **Agent 架构 / 实现岗** 45–90 分钟；答案详见 [19_agent_architecture_implementation.md](./19_agent_architecture_implementation.md) 与 [02_agent_system.md](./02_agent_system.md)。

---

## 卷 A：架构理解（20 min · L1/L2）

| # | 题目 | 参考 |
|---|------|------|
| 1 | 画出 API → AgentService → WorkflowRegistry → ProfiledAgentExecutor → create_agent_graph → SSE 链路 | 19-总览图、02-Q1 |
| 2 | WorkflowRegistry.resolve_name() 解析优先级 | 02-Q2、19-Q4 |
| 3 | lightweight 为何仍建 ProfiledAgentExecutor | 19-Q4 |
| 4 | ThreadState 扩展字段与 Reducer | 02-Q14、19-Q5 |
| 5 | RuntimeFeatures 五个 flag | 19-Q2 |
| 6 | 工厂 8 层 Middleware 顺序及 Clarification 为何最后 | 19-Q3 |
| 7 | ContextPruning 三步：Elevation / 裁剪 / JIT | 02-Q5、19-Q6 |
| 8 | Lightweight 内部是否走 LangGraph tool loop | 19-Q7 |

**通过线：** 8 题对 6 题，且能白板画出 WorkflowRegistry 解析优先级。

---

## 卷 B：实现细节（25 min · L2）

| # | 题目 | 参考 |
|---|------|------|
| 9 | create_agent_graph 三种 middleware 参数互斥 | 19-Q1 |
| 10 | ToolAssembly lightweight 返回空列表的含义 | 19-Q11 |
| 11 | aquery_stream 与 StreamHandler 职责分界 | 19-Q8、19-Q9 |
| 12 | set_tool_budgets 与 LoopDetection 区别 | 19-Q15 |
| 13 | task 工具 deep-research 子 Agent 禁止嵌套 task 的原因 | 19-Q10 |
| 14 | Guardrail deny vs ToolErrorHandling | 19-Q13 |
| 15 | DanglingToolCallMiddleware 位置与作用 | 19-Q12 |
| 16 | extra_middleware @Next/@Prev 用途 | 19-Q14 |
| 17 | prompt_domain ne/csh 影响哪些 Prompt | 02-Q12、19-Q20 |
| 18 | internal thread_key 格式 | 19-Q19 |

**追问链示例：**

- Q11 追：astream_events 哪些 event 名进 Handler？
- Q13 追：如何新增 general-purpose 以外的子 Agent 类型？

---

## 卷 C：架构权衡（20 min · L3）

| # | 题目 | 参考 |
|---|------|------|
| 19 | JIT 8000 字裁剪对多跳 RAG 的影响与缓解 | 02-Q11 |
| 20 | lightweight 无 ToolGuardrail 为何仍合理 | 19-Q16 |
| 21 | Summarization 与 ContextPruning 并存风险 | 19-Q17 |
| 22 | 子 Agent 静态白名单 vs Skill 动态工具 | 19-Q10、02-Q13 |
| 23 | Skill 驱动 Workflow 未落地的架构代价 | 19-Q24 |
| 24 | SQLite checkpoint 多 worker 与 Agent 有状态图矛盾 | 19-Q19、05-Q11 |

**设计题（二选一）：**

1. 设计 `SubagentRegistry`，新增 `data-analysis` 子 Agent 不改 task_tool dict  
2. 设计 Skill manifest，`html-ppt` 自动走 full + 限定 tool budget

---

## 卷 D：编码/白板（15 min）

### 题 1：伪代码 — WorkflowRegistry.resolve_name()

```python
def resolve_name(request, profile, skill) -> str:
    # 请补全解析优先级逻辑
    ...
```

**要点：** 显式 request.workflow > Skill preferred_workflow > 有 Skill 则 full > profile.default_workflow > lightweight。

### 题 2：伪代码 — ContextPruning 处理 multi_kb_search

```python
def after_tool(tool_message):
    if tool_message.name == "multi_kb_search":
        # 请写 state 提升 + 占位符替换
        ...
```

---

## 红旗 / 绿旗

| 红旗 | 绿旗 |
|------|------|
| lightweight = 无 RAG | lightweight = 内联 multi_kb，无 tool loop |
| create_deerflow_agent 是生产入口 | create_agent_graph（ProfiledGraphBuilder） |
| ContextPruning 删除证据 | 提升到 state + JIT |
| 所有模式都走 ToolAssembly 全工具 | lightweight assemble 返回 [] |
| WorkflowRegistry 看 use_search 决定 lightweight | 看 selected_skills + preferred_workflow |

---

## 推荐学习路径（Agent 专项 3 天）

| 天 | 内容 |
|----|------|
| Day1 | 19 模块 L1 + 02 Q1–Q5 + 跟读 agent_service + registry |
| Day2 | 19 L2 + lightweight_qa_agent + context_pruning + task_tool |
| Day3 | 19 L3 + 卷 C/E + 23 全文 + 24 D1–D7 |
| Day4 | 21 Middleware + 22 W1–W20 + 24 文档对照 |
| Day5 | **25 深研全文** + 22 W31–W40 + 卷 F |

---

## 卷 F：深度研究 Orchestrator（20 min · L2/L3）

| # | 题目 | 参考 |
|---|------|------|
| 33 | 口述深研状态机：draft → awaiting → confirmed → running → completed | 25-Q1/Q4/Q5 |
| 34 | CoordinatedAgentWorkflow 大纲阶段与编排职责边界 | 25-Q3, 22-W37 |
| 35 | 为何 Coordinated 编排器移除 multi_kb_search | 25-Q7, 22-W32 |
| 36 | deep-research 子 Agent 工具白名单与禁止嵌套 task 的原因 | 25-Q9, 22-W34 |
| 37 | confirmed 后 AgentService 启动什么后台工作流 | 25-Q10, 22-W36 |
| 38 | task stream 与主 query SSE 何时切换 | 25-Q5/Q12 |
| 39 | 取消深研任务的 API 与 runner 检测点 | 25-Q15, 22-W39 |
| 40 | 设计题：子 Agent 全量 evidence 回主图的风险与缓解 | 25-Q18 |

**通过线：** 8 题对 6 题，且能画出 confirmed 后 CoordinatedAgentWorkflow → 2× task → 子 Agent KB 的 sequenceDiagram。

**红旗补充：**

| 红旗 | 绿旗 |
|------|------|
| 深研 = Full + 长 timeout | 独立 CoordinatedAgentWorkflow + 子 Agent 检索 |
| 大纲阶段就 multi_kb_search | Draft 不检索，确认后才 runner |
| 主 Agent 可直接 web_search | blocked，必须 task 委派 |

---

## 卷 E：Lightweight vs Full + 文档对照（25 min · L2/L3）

| # | 题目 | 参考 |
|---|------|------|
| 25 | 画出 WorkflowRegistry 解析与 ProfiledAgentExecutor 轻量/全量委托关系 | 23-Q1/Q2, 24-D3 |
| 26 | ToolAssembly 在 lightweight 返回 [] 的原因与检索发生位置 | 23-Q6, 24-D14 |
| 27 | SKIP_KB 协议触发条件与用户体验 | 23-Q8, 19-Q27 |
| 28 | ContextPruning 为何 Full 独有 | 23-Q10, 24-D7 |
| 29 | StreamHandler 与 Lightweight 手写 yield 的差异 | 23-Q11, 19-Q29 |
| 30 | 对照 agent_engine 文档口述端到端路径（60 秒） | 24-D1 |
| 31 | 文档排障表：无 Skill 却很慢 | 24-D17, 23-Q17 |
| 32 | 设计第四档 `standard` 路由的权衡 | 23-Q20 |

**通过线：** 8 题对 6 题，且能在白板上画出 Lightweight 内联检索 sequenceDiagram。

**红旗补充：**

| 红旗 | 绿旗 |
|------|------|
| 文档说 lightweight 无 RAG | 内联 execute_multi_kb_search |
| ContextPruning 在 factory 默认链 | extra_middleware 注入 |
| Router 直接 new LightweightQAAgent | ProfiledAgentExecutor(lightweight_mode=True) |
