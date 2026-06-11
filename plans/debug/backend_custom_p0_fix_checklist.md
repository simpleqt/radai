# backend/custom P0 最小修复清单

**来源**：[backend_custom_code_review.md](./backend_custom_code_review.md)、[backend_custom_per_file_review_detailed.md](./backend_custom_per_file_review_detailed.md)  
**原则**：仅列 **P0（功能错误/域错配/明显泄漏）**；不写具体补丁代码，便于拆任务/PR。  
**日期**：2026-05-30 · **状态更新**：2026-05-31（P0-1、P0-2 已在代码中落地）

---

## 验收标准（全局）

- [x] CSH 深度研究后台任务与实时深研：系统 Prompt / 工具约束体现**场地污染水文地质**，不出现 NE 专属「核电子学智能体」硬规则。
- [x] CSH / explore(`domain=csh`) 轻量问答：系统 Prompt 为 CSH 领域，回答不默认以核电子学专家自居。
- [ ] `POST /api/v2/agent/query` 与 `POST /api/v2/csh_agent/query`：`chunk` 中不出现未过滤的 `` / `<thought>` 原文（与 guest 行为一致）。
- [ ] 上述三条各有 **1 条自动化或手工用例** 记录在 PR/测试说明中。

---

## P0-1：CSH 深度研究使用 NE 编排器 ✅（2026-05-31）

**已实现**：`csh_deep_research_orchestrator_agent.py`、`agent_factory.py`、`csh_router.py`、`runner.py`（`agent_domain="csh"`）、`csh_agent.py`。

| 项 | 内容 |
|----|------|
| **现象** | CSH 用户确认深研后，主控仍为 `DeepResearchOrchestratorAgent(NEFullFeatureAgent)` |
| **涉及文件** | `agents/deep_research_orchestrator_agent.py`、`agents/csh_router.py`、`tasks/runner.py`、`api/v2/csh_agent.py` |
| **建议改法** | ① 新增 `CSHDeepResearchOrchestratorAgent(CSHFullFeatureAgent)` 或组合注入 `build_system_prompt` from `csh_prompts`；② `run_deep_research_background(..., domain="csh")` 分支实例化；③ `CSHAgentRouter` 深研分支改用 CSH 编排器 |
| **勿做** | 仅改日志前缀不改 Prompt |
| **验证** | CSH 账号发起深研，抓 checkpoint 或首条 status：不应出现仅 NE 才有的 skill_enforcement 原文（若可观测） |

---

## P0-2：轻量模式 NE Prompt 用于 CSH / Explore ✅（2026-05-31）

**已实现**：`LightweightQAAgent.prompt_domain`、`csh_prompts.CSH_LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT`、`csh_full_feature_agent.py`（`prompt_domain="csh"`）；Explore `domain=csh` 经 `CSHFullFeatureAgent` 自动生效。

| 项 | 内容 |
|----|------|
| **现象** | `LightweightQAAgent` 的 `LIGHTWEIGHT_SYSTEM_PROMPT` 写死核电子学；`CSHFullFeatureAgent`、`explore/detail` 均委托该类 |
| **涉及文件** | `agents/lightweight_qa_agent.py`、`agents/csh_full_feature_agent.py`、`api/v2/explore.py` |
| **建议改法** | `LightweightQAAgent.__init__(..., subject: str \| None)` 或 `system_prompt: str`；CSH 传入 `csh_prompts` 精简版；explore 按 `resolve_subject(domain)` 传入 |
| **验证** | `domain=csh` 调用 explore detail，问题「什么是渗透系数」；回答应含水文地质语境，且不应要求「核探测器」类表述 |

---

## P0-3：主站 SSE 未过滤思考标签（chunk 泄漏）

| 项 | 内容 |
|----|------|
| **现象** | `agent.py` / `csh_agent.py` L929-936 直接 `yield chunk`；`_split_thinking_segments` 未使用；guest.py L151 已过滤 |
| **涉及文件** | `api/v2/agent.py`、`api/v2/csh_agent.py`；可抽 `agents/streaming/thinking_filter.py` 与 guest/runner 共用 |
| **建议改法** | 流式 chunk 路径与 `guest.py` 对齐：visible → `chunk`，thinking → `thinking`；或下沉到 `NEAgentStreamHandler._on_model_stream` 统一出口 |
| **验证** | 使用会输出 `` 的模型，主站对话 UI 不应显示标签内原文 |

---

## 建议 PR 拆分（最小 3 PR）

| PR | 范围 | 依赖 |
|----|------|------|
| PR-A | P0-3 思考过滤 + 抽公共模块 | 无 |
| PR-B | P0-2 Lightweight 领域参数 + CSH/Explore 传参 | 无 |
| PR-C | P0-1 CSH 编排器 + runner domain | 可与 B 并行，合并前需联调深研全链路 |

---

## 不在此清单的 P1（紧随其后）

| ID | 摘要 | 文件 |
|----|------|------|
| P1-1 | `agent.py` / `csh_agent.py` 抽公共流式模块 | api/v2 |
| P1-2 | `TaskManager.remove_task` 在 runner finally 调用 | tasks/ |
| P1-3 | explore 开放 API 鉴权/限流 | api/v2/explore.py |
| P1-4 | 异常信息脱敏 | api/v2/*.py |
| P1-5 | 删除死代码 `_normalize_*`、未使用 import | agent.py |

---

## 文档同步（P0 修复后）

- [x] [agent_architecture_redesign.md](../../docs/agent_architecture_redesign.md) §已落地表：CSH 深研/轻量域（2026-05-31）
- [ ] [sse_events_module.md](../../docs/docs_backend/sse_events_module.md)：主站/guest 过滤差异或统一后更新
- [x] [agent_engine_module.md](../../docs/docs_backend/agent_engine_module.md)（轻量 `prompt_domain`、CSH 深研编排）

---

## 任务追踪表（可复制到 Issue）

| ID | 状态 | 负责人 | PR |
|----|------|--------|-----|
| P0-1 | 已完成 | 2026-05-31 | CSH 深研编排器 |
| P0-2 | 已完成 | 2026-05-31 | 轻量 Prompt 分域 |
| P0-3 | 待办 | | |

---

*修复时请勿扩大范围：P0 阶段不做 workflows/registry 或 API 大重构。*

**实施步骤详见**：[backend_custom_p0_implementation_guide.md](./backend_custom_p0_implementation_guide.md)
