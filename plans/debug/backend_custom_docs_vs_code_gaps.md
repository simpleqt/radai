# backend/custom 与官方模块文档差距清单

**对照目录**：`docs/docs_backend/*_module.md`、`docs/agent_architecture_redesign.md`  
**代码审查**：`plans/debug/backend_custom_*`  
**创建**：2026-05-30 · **文档修订**：2026-05-31（`docs/` 多轮同步，见 [CHANGELOG_文档同步_20260531.md](../../docs/CHANGELOG_文档同步_20260531.md)）

本文记录 **文档声称的行为** 与 **代码实测** 不一致处。  
**注意**：§2–§6 中许多 **纯文档** 项已在 2026-05-31 修正；**DOC-P0-1、DOC-P0-2 已于 2026-05-31 在代码中修复**（见 §1 状态表）；DOC-P0-3 仍待实现。

### 2026-05-31 文档侧已对齐（节选）

| 原差距 ID / 章节 | 处理 |
|------------------|------|
| `csh_ai` 架构图直连 Lightweight | 已改为 FullFeature → 委托 |
| `sse` explore `*/stream`、`thinking_enabled`、`attachments` | 已改为 `/detail`、`thinking`、`file_ids` |
| `explore` map/expand 均检索 | 已注明 map/expand **不检索** |
| `sms_auth` Redis | 见 [sms_auth_module.md](../../docs/docs_backend/sms_auth_module.md) |
| `agent_architecture` §3.0 | 已增「当前已实现」图 |

### 2026-05-31 代码已修复（文档已同步）

| 原差距 ID | 实现摘要 |
|-----------|----------|
| DOC-P0-1 | `CSHDeepResearchOrchestratorAgent`、`agent_factory`、`csh_router`、`runner(agent_domain="csh")` |
| DOC-P0-2 | `LightweightQAAgent.prompt_domain`；CSH 使用 `csh_prompts` + `CSH_LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT` |

### 仍建议优先跟进的代码/契约项

| 项 | 说明 |
|----|------|
| DOC-P0-3 | 主站 think 标签过滤 — **见 §1** |
| `deep_research` 请求字段 | `research_plan` vs 文档 `revision_feedback` — 以 OpenAPI 为准 |
| `explore` 端点鉴权 | 多数无需登录 — 安全说明待产品确认 |

---

## 1. P0 实现状态（2026-05-31）

| ID | 审查结论（2026-05-30） | 当前代码状态 | 生产文档 |
|----|------------------------|--------------|----------|
| DOC-P0-1 | CSH 深研走 NE 编排器 | ✅ **已修复** — `CSHDeepResearchOrchestratorAgent` + `agent_domain="csh"` | `csh_ai_module.md`、`deep_research_module.md` |
| DOC-P0-2 | 轻量路径 NE Prompt | ✅ **已修复** — `prompt_domain` + `csh_prompts` 轻量模板 | `agent_engine_module.md`、`explore_module.md` |
| DOC-P0-3 | 主站 think 进 chunk | ❌ **未修复** — guest/runner 已过滤 | `sse_events_module.md`、`csh_ai_module.md` §已知限制 |

### 历史记录（修复前）

| ID | 修复前代码实际 |
|----|----------------|
| DOC-P0-1 | `DeepResearchOrchestratorAgent(NEFullFeatureAgent)` 用于 CSH 路由与后台 runner |
| DOC-P0-2 | `LIGHTWEIGHT_SYSTEM_PROMPT` 写死核电子学；`csh_prompts` 轻量未被 `LightweightQAAgent` 调用 |

---

## 2. agent_engine_module.md

| 章节 | 文档内容 | 代码/审查 | 建议修正 |
|------|----------|-----------|----------|
| §双通道路由图 L74 | CSH/NE 均委托 LightweightQAAgent | ✅ 委托 + `prompt_domain` 分域（2026-05-31） | — |
| §CSH L84-86 | CSHAgentRouter 三种模式 | ✅ 深研用 `CSHDeepResearchOrchestratorAgent` | — |
| §multi_kb L140 | CSH `MULTI_KB_SEARCH_NORMAL_MAX_CALLS=5` | 需核对 `csh_full_feature_agent` 是否覆盖 env 默认 | 与 NE 对比表保留 |
| §后续 L199 | 深研 cancel API | ✅ 存在 | OK |

---

## 3. csh_ai_module.md

| 章节 | 文档内容 | 代码/审查 | 建议修正 |
|------|----------|-----------|----------|
| 架构图 L16 | `Router --> QA[LightweightQAAgent]` | 实际：`CSHFullFeatureAgent(lightweight)` 再委托 | 图改为 `CSHFullFeatureAgent → LightweightQAAgent` |
| L27-28 | 轻量 1-2 秒 | 依赖检索+LLM，非承诺 SLA | 改为「通常较快」或删具体时间 |
| L31-39 | CSH Prompt 在 csh_prompts | ✅ 全功能 + 轻量（`prompt_domain=csh`） | — |
| 深研 L66-70 | `run_deep_research_background(agent_domain="csh")` | ✅ CSH 编排器 | — |
| 思考链 L101 | `<think>` 分离 | 前端可能处理；后端主站未统一过滤 | 链到 sse 文档 |

---

## 4. deep_research_module.md

| 章节 | 文档内容 | 代码/审查 | 建议修正 |
|------|----------|-----------|----------|
| L5 | CSH 与 RadAI 共用流程 | ✅ API/状态机共用 | OK |
| L47 编排器 | 多步编排 | ✅ NE + CSH 双类 + `agent_factory` | — |
| §8 Router → Orchestrator | 按域工厂选择 | ✅ 文档已更新 | — |
| L80 `revision_feedback` | 请求字段 | 代码为 `research_plan` + `confirmation_status` | 与 OpenAPI 对齐字段名 |

---

## 5. sse_events_module.md

| 章节 | 差距 | 建议 |
|------|------|------|
| L10 explore `*/stream` | 实际 `POST /explore/detail` | 改路径 |
| L172 `thinking_enabled` | 代码 `thinking` | 统一命名 |
| L173 `attachments` | 代码 `file_ids` | 统一命名 |
| §3.2 thinking | 未说明 agent/csh vs guest 差异 | 见 [backend_custom_sse_and_api_contract_gaps.md](./backend_custom_sse_and_api_contract_gaps.md) |

---

## 6. explore_module.md

| 章节 | 差距 | 建议 |
|------|------|------|
| 服务层「先 retrieve」 | `generate_map`/`expand_node` **不检索**（`explore_learning.py` L434-435） | 文档改为「map/expand 轻量 LLM；resources/quiz 检索」 |
| detail 轻量 Agent | domain=csh → `prompt_domain=csh` | ✅ 2026-05-31 |
| 鉴权 | 多数端点无登录 | 安全说明 + 链 `认证方案.md` |

---

## 7. skills_tools_module.md

| 项 | 状态 |
|----|------|
| `tool_assembly.py` 路径 | ✅ |
| `academic_search` → `acdemic_search_tool.py` | 文档可注拼写 |

---

## 8. follow_up_suggestions_module.md

| 项 | 状态 |
|----|------|
| agent/csh/guest 接入 | ✅ |
| guest `follow_up_guest_enabled` | 代码存在，文档可补 env |

---

## 9. guest_module.md（交叉）

| 项 | 差距 |
|----|------|
| thread 键 | 文档 `g_{device}__t_{}` 与 `build_internal_thread_key` 访客路径需一致；`guest.py` 自建 `internal_tid`（审查 P2） |

---

## 10. agent_architecture_redesign.md

| 项 | 差距 |
|----|------|
| §已落地 `skill_workflow` | 代码无 `get_workflow_for_skills` |
| Router 示例 `sandbox_enabled` | 当前 `router.py` 无此参数 |
| CSH | 全文 NE centric | 增 §「CSH 平行域」 |

详见 [backend_custom_vs_architecture_redesign.md](./backend_custom_vs_architecture_redesign.md)。

---

## 11. 文档修订优先级（2026-05-31 状态）

| 优先级 | 文档 | 状态 |
|--------|------|------|
| P0 同步 | `csh_ai_module.md`, `deep_research_module.md` | ✅ 含 DOC-P0-1/2 代码与文档 |
| P0 同步 | `sse_events_module.md` | ✅ 字段名、explore `/detail` |
| P1 | `explore_module.md` | ✅ map/expand 不检索 |
| P1 | `agent_engine_module.md` | ✅ 路由/排障 + 轻量 `prompt_domain` |
| P2 | `agent_architecture_redesign.md` | ✅ §3.0 已实现图 |

---

## 12. 修复后「文档验收」检查表

- [x] `csh_ai_module.md` 架构图与 `csh_router.py` 一致（2026-05-31）
- [x] `deep_research_module.md` 列出 `CSHDeepResearchOrchestratorAgent`（2026-05-31）
- [ ] `sse_events_module.md` 说明主站 chunk 已过滤思考标签（**待 P0-3 代码**）
- [x] `explore_module.md` map/expand 检索策略准确
- [x] `agent_engine_module.md` 轻量路由 + `prompt_domain` 表述

---

*维护者：P0 代码合并时在同一 PR 或 follow-up doc PR 中勾选 §12。*
