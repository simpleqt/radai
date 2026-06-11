# backend/custom 与架构重构方案对照审查

**对照文档**：[docs/agent_architecture_redesign.md](../../docs/agent_architecture_redesign.md)（2026-05-30 同步版）  
**代码审查**：[backend_custom_code_review.md](./backend_custom_code_review.md)  
**日期**：2026-05-30 | 未改源码

---

## 1. 结论摘要

重构方案中 **Phase 1–5、7 大体已落地**（prompts、stream_utils、stream_handler、tool_assembly、router、API 走 Router）。  
**Phase 6（SkillWorkflow / workflows/registry）未落地**；`backend/custom/workflow/` 仅为**深度研究 HTTP 状态机**，与方案中的 `workflows/` 不是同一概念。

审查发现的 **P0 缺陷**（CSH 域错配、主站思考标签泄漏）在重构文档「已落地」表中**未体现**，属于实现与文档双轨漂移，需单独排期。

---

## 2. 方案目标 vs 代码现状

| 重构目标（§二） | 文档自称状态 | 代码实证 | 差距 |
|----------------|-------------|----------|------|
| Skill 驱动路由 | ⚠️ ToolAssembly 按 Skill 注入工具 | `NEAgentRouter._resolve_mode` 仅 `lightweight \| deep_research \| full`，**无 `skill_workflow` 分支** | ❌ 未实现 §4.2 设计稿 L214-242 |
| 轻量通道保留 | ✅ | `router` → `lightweight_mode=True` → `LightweightQAAgent` | ✅；但 **Prompt 仅 NE**（P0） |
| 主智能体精简 | ⚠️ stream 已拆 | `ne_full_feature_agent.py` 仍 ~1040 行，含图循环/超时/预算 | ⚠️ 未达「只路由+调度」 |
| 流式处理器独立 | ✅ NEAgentStreamHandler | `stream_handler.py` ~987 行，仍偏大 | ⚠️ 符合文档，但 handler 仍 NE 命名且被 CSH 复用 |
| 子智能体可扩展 | ⚠️ SubagentLimitMiddleware | 新类型仍要改 core `task` 工具与 prompt | ⚠️ 与方案一致，未进一步解耦 |
| 工具框架化 | ⚠️ ToolAssemblyPipeline | 仍有 `tool_assembly` + Agent 内预算/闭包 | ⚠️ 部分达成 |

---

## 3. 「已落地」表逐项核验

| 原问题（文档 L10-20） | 核验结果 | 审查备注 |
|----------------------|----------|----------|
| 无统一路由器 | ✅ `NEAgentRouter` / `CSHAgentRouter` 存在 | CSH 路由器深研仍指向 NE 编排器（**P0**） |
| aquery_stream 900 行单函数 | ⚠️ 已拆 handler | Agent 主文件仍含 **~450 行** 流式循环（超时/续跑/预算） |
| 流式协议分散 | ✅ `sse_events_module.md` | 主站 **未** 做 guest 同级思考标签过滤（**协议与实现不一致**） |
| 上下文膨胀 | ⚠️ ContextPruningMiddleware | 无统一 Tool Archive |
| Skill 仅影响提示词 | ⚠️ ToolAssembly | 无 Skill→Workflow |
| 全局单例 Agent | ✅ create_agent_graph | — |
| 子智能体白名单静态 | ⚠️ | — |
| 多库 RAG | ✅ multi_kb_search_tool | 默认 kb 仍为 NE 硬编码 |
| 关键词 LLM | ✅（在 RAG 层） | custom 内 Path-A 等与 RAG 文档一致 |

---

## 4. 目录结构：方案 vs 实际

### 4.1 方案目标树（§三、§4.1）

```
backend/custom/agents/
├── router.py                 ✅ 存在（无 skill_workflow）
├── workflows/                ❌ 不存在
│   ├── base.py
│   ├── deep_research.py
│   └── registry.py
├── workflow/                 ✅ 存在但语义不同
│   └── research_state.py     ← HTTP 深研状态枚举，非 SkillWorkflow
```

### 4.2 已存在且符合/部分符合的拆分

| 模块 | 方案 Phase | 实际路径 | 状态 |
|------|-----------|----------|------|
| prompts | 1 | `agents/support/prompts.py` + `csh_prompts.py` | ✅ 已拆；CSH 未并入统一 builder |
| stream_utils | 2 | `agents/streaming/stream_utils.py` | ✅ |
| stream_handler | 3 | `agents/streaming/stream_handler.py` | ✅ 体量仍大 |
| tool_assembly | 4 | `agents/support/tool_assembly.py` | ✅ |
| router | 6–7 | `agents/router.py`, `csh_router.py` | ✅ 简化版（3 路而非 4 路） |
| API 用 Router | 7 | `agent.py` L790 `NEAgentRouter` | ✅ 深研草案/后台仍在 API 层（与 §7.2 一致） |

---

## 5. 调用链：方案图 vs 实际（含 CSH 缺口）

### 5.1 文档 §1.2 调用链（已更新为 2026-05-30）

文档已写 `NEAgentRouter` / `ResearchDraftAgent` / `NEAgentStreamHandler`，与代码一致。

### 5.2 文档未覆盖的实际链（审查补充）

```
POST /api/v2/csh_agent/query
  → CSHAgentRouter
       → deep_research → DeepResearchOrchestratorAgent(NEFullFeatureAgent)  ← P0
       → lightweight   → CSHFullFeatureAgent → LightweightQAAgent(NE Prompt) ← P0
  → run_deep_research_background (confirmed)
       → DeepResearchOrchestratorAgent  ← 无 domain 参数

POST /api/v2/explore/detail (domain=csh)
  → CSHFullFeatureAgent(lightweight=True)
       → LightweightQAAgent(NE Prompt)  ← P0
```

**方案缺口**：全文以 NE 为主，**未定义 CSH 平行域**（`CSHAgentRouter`、`csh_prompts`、CSH 编排器）。

---

## 6. API 层：§7.1「重构后」达成度

| 项 | 方案期望 | 实际 |
|----|----------|------|
| 不再 `agent_cls = DeepResearch... if ... else NEFull...` | 统一 Router | ✅ 普通流式已用 Router |
| API 不关心 Orchestrator vs Full | 是 | ⚠️ API 仍直接 `ResearchDraftAgent`、`run_deep_research_background`、工作流 metadata |
| `sandbox_enabled` | Router 构造参数 | ❌ 当前 `router.py` **无** `sandbox_enabled` 字段（方案示例过时） |
| CSH 端点 | 未写 | `csh_agent.py` **重复** agent 逻辑，未共享方案中的单一 API 抽象 |

---

## 7. 与 SSE 文档的一致性

对照 [sse_events_module.md](../../docs/docs_backend/sse_events_module.md)：

| 协议约定 | 实现 | 差距 |
|----------|------|------|
| `chunk` 为回答正文 | 主站 agent/csh 直传模型 chunk | 可能含 `` 泄漏（**未文档化**） |
| `thinking` 推理片段 | guest 拆分后发出；主站依赖原生 `thinking` 事件 | **行为因端点而异**，文档未区分 |
| explore 流 | 文档写 explore stream | `explore/detail` 用 agent 流，无 `follow_up`（文档已注） |
| `thinking_enabled` | QueryRequest.thinking | 与字段名 `thinking`（代码）需 Swagger 对齐 |

建议在 `sse_events_module.md` 增 §「思考内容过滤」：主站 NE/CSH 与 guest 差异，直至代码统一。

---

## 8. 迁移计划 Phase 完成度

| Phase | 内容 | 完成度 | 遗留 |
|-------|------|--------|------|
| 1 | prompts.py | ✅ | csh_prompts 重复 |
| 2 | stream_utils | ✅ | — |
| 3 | stream_handler | ✅ | 987 行，可再拆 |
| 4 | tool_assembly | ✅ | academic 文件名拼写 |
| 5 | 精简 ne_full_feature | ⚠️ ~50% | 流式主循环仍在 Agent |
| 6 | workflows + registry | ❌ 0% | 仅 research_state |
| 7 | API → router | ✅ | csh_agent 未 DRY；后台 runner 绕过 Router |

**方案预估 12h**；Phase 6 + CSH 域 + API DRY + P0 修复 **未纳入原估算**。

---

## 9. 设计决策（§九）仍成立与否

| # | 决策 | 审查意见 |
|---|------|----------|
| 1 | 无全局工具 Registry | ✅ 仍成立 |
| 2 | 保留 LangGraph | ✅ |
| 3 | 复用 SubagentExecutor | ✅ |
| 4 | Router 不继承 Agent | ✅ 已实现 |
| 5 | 闭包 multi_kb 在 Agent/Assembly | ✅ 在 ToolAssemblyPipeline |
| 6 | 深研委托 Orchestrator | ✅ 但 **应分 NE/CSH 子类** |
| 7 | 装饰器注册 workflow | ❌ registry 未建 |

**建议新增决策 #8**：`CSHFullFeatureAgent` 不得继承/委托 NE 专用 `DeepResearchOrchestratorAgent` 与 `LightweightQAAgent`，除非显式传入 `domain`。

---

## 10. 推荐后续工作（对齐架构文档）

| 优先级 | 工作项 | 对应方案章节 | 预估 |
|--------|--------|-------------|------|
| P0 | CSH 深研/轻量域修复 | 扩展 §三架构图 | 1–2d |
| P0 | 主站 chunk 思考过滤对齐 guest | §1.2 流式 | 0.5d |
| P1 | 抽取 `api/v2/agent_stream_common.py` | §7 API | 1d |
| P1 | `runner` 增加 `domain` + 编排器工厂 | §4 DeepResearchWorkflow | 1d |
| P2 | 实现 `workflows/registry` + `skill_workflow` 路由 | Phase 6 | 2–3d |
| P2 | 精简 `aquery_stream` 至 handler+router | Phase 5 | 2d |
| P3 | 更新 `agent_architecture_redesign.md` CSH 与 P0 已知问题 | 文档 | 2h |

---

## 11. 相关 debug 文档

| 文档 | 用途 |
|------|------|
| [backend_custom_p0_fix_checklist.md](./backend_custom_p0_fix_checklist.md) | P0 最小修复清单（可执行条目） |
| [backend_custom_per_file_review_detailed.md](./backend_custom_per_file_review_detailed.md) | 逐文件深度 |
| [backend_custom_code_review.md](./backend_custom_code_review.md) | 总览 |

---

*本对照不否定重构方向；主要标明「文档已落地」与「生产缺陷」之间的盲区。*
