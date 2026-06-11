# backend/custom — GitHub/GitLab Issue 模板

复制到 Issue 跟踪系统；勾选验收标准后关闭。  
**不修改代码**，仅提供任务描述。

---

## Issue P0-1：CSH 深度研究使用 NE 编排器与 NE 系统 Prompt

### 标签

`P0` `bug` `csh-ai` `deep-research` `backend/custom`

### 描述

CSH-AI 用户在深度研究（实时或后台 `run_deep_research_background`）时，主控智能体为 `DeepResearchOrchestratorAgent`，其继承 `NEFullFeatureAgent` 并使用 `prompts.py` 中的核电子学系统提示，与 `csh_prompts.py` / `CSHFullFeatureAgent` 设计不一致。

### 复现

1. 登录 CSH 前端，发起 `task_type=deep_research` 或模式 `deep-research`
2. 确认研究计划并启动后台任务
3. 观察主控行为/检索策略偏向 NE 领域（或检查 checkpoint / 日志中的 system 片段）

### 涉及文件

- `backend/custom/agents/deep_research_orchestrator_agent.py`
- `backend/custom/agents/csh_router.py`（L77-95）
- `backend/custom/tasks/runner.py`（L212+）
- `backend/custom/api/v2/csh_agent.py`（后台任务创建）

### 建议方案

见 [backend_custom_p0_implementation_guide.md](./backend_custom_p0_implementation_guide.md) PR-C。

### 验收标准

- [ ] CSH 深研主控 system prompt 来自 `csh_prompts` 或 `CSHDeepResearchOrchestratorAgent`
- [ ] `run_deep_research_background(..., agent_domain="csh")` 生效
- [ ] NE RadAI 深研回归通过

---

## Issue P0-2：轻量模式 LightweightQAAgent 对 CSH/Explore 仍使用 NE Prompt

### 标签

`P0` `bug` `csh-ai` `explore` `lightweight`

### 描述

`LightweightQAAgent` 硬编码 `LIGHTWEIGHT_SYSTEM_PROMPT`（核电子学）。`CSHFullFeatureAgent` 与 `explore/detail`（`domain=csh`）在 `lightweight_mode=True` 时仍委托该类，导致 CSH 轻量问答与探索详解以核电子学角色作答。

注：`csh_prompts.build_system_prompt(lightweight_mode=True)` **已实现但未接入**。

### 复现

```http
POST /api/v2/csh_agent/query
{"question":"什么是渗透系数？","selected_skills":[]}
```

或 explore detail `domain=csh`。

### 建议方案

见 [backend_custom_p0_implementation_guide.md](./backend_custom_p0_implementation_guide.md) PR-B。

### 验收标准

- [ ] CSH 轻量路径使用 `csh_prompts` 轻量 system 段
- [ ] CSH 专用 `LIGHTWEIGHT_ANSWER_TEMPLATE`（无 NE 文献示例）
- [ ] explore `domain=csh` detail 通过领域用例

---

## Issue P0-3：主站 agent/csh SSE 未过滤  思考标签

### 标签

`P0` `bug` `sse` `security-ux`

### 描述

`api/v2/agent.py` 与 `csh_agent.py` 定义了 `_split_thinking_segments`，但流式 `chunk` 分支直接转发 `event.content`。`guest.py` 已正确拆分。用户可能在正文区看到 `` 内容。

### 复现

使用会在正文中输出 think 标签的模型，调用主站 query，观察 assistant 正文。

### 建议方案

见 PR-A：抽取 `agents/streaming/thinking_filter.py` 并在 agent/csh 使用。

### 验收标准

- [ ] 主站 chunk 不含未闭合 think 标签
- [ ] thinking 内容进入 `type:thinking` 事件
- [ ] guest 行为不变（回归）

---

## Issue P1-1：合并 agent.py 与 csh_agent.py 重复流式逻辑

### 标签

`P1` `refactor` `tech-debt`

### 描述

两文件约 91% 结构重复（~2400 行），修复易漏改。

### 建议

抽取 `api/v2/agent_stream_common.py`，参数化 `agent_code` / `Router` 类。

---

## Issue P1-2：TaskManager 已完成任务不回收内存

### 标签

`P1` `bug` `memory` `deep-research`

### 描述

`TaskManager.remove_task` 存在但无调用方；`_tasks` 字典持续增长。

### 建议

`runner.py` finally 调用 `remove_task`；可选 terminal 后延迟清理。

---

## Issue P1-3：Explore API 无鉴权导致 LLM/检索成本风险

### 标签

`P1` `security` `explore`

### 描述

`POST /api/v2/explore/topics|map|expand|quiz|detail` 无登录，可被滥刷。

### 建议

可选鉴权 + 限流；见 `plans/认证方案.md`。

---

## Issue P1-4：深研子智能体 Prompt 未区分 CSH/NE 域

### 标签

`P1` `bug` `csh-ai` `core`

### 描述

`backend/core/tools/task_tool.py` 中 `deep-research` 子智能体 system_prompt 为通用模板；CSH 深研时子任务规范未对齐水文地质。

### 依赖

建议 P0-1 完成后处理。

---

## Issue P2：为 backend/custom 补充单元测试

### 标签

`P2` `testing`

### 描述

`backend/custom` 下无任何 `test_*.py`。

### 建议首批

- think tag 拆分（抽取后）
- `normalize_research_workflow_state`
- `ToolAssemblyPipeline` 工具列表快照

---

## Issue DOC：同步官方模块文档与代码差距

### 标签

`documentation`

### 描述

`docs/docs_backend/csh_ai_module.md`、`explore_module.md`、`sse_events_module.md` 等与实现不一致。

### 清单

见 [backend_custom_docs_vs_code_gaps.md](./backend_custom_docs_vs_code_gaps.md) §12 验收表。

---

## 里程碑建议

| 里程碑 | Issues |
|--------|--------|
| M1 正确性 | P0-1, P0-2, P0-3 |
| M2 可维护 | P1-1, P1-2, P1-4 |
| M3 安全/成本 | P1-3 |
| M4 质量 | P2, DOC |

---

*创建日期：2026-05-30*
