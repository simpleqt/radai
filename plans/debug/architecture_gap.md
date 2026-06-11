# 架构文档 vs 代码现状差距分析

> 对照 [agent_architecture_redesign.md](../../docs/agent_architecture_redesign.md)（2026-05-30 同步版）与审查结论。

---

## 1. 文档声称已落地 vs 代码实测

| 文档条目 | 声称 | 代码实测（2026-05-30 审查） | 差距 |
|----------|------|------------------------------|------|
| 统一路由器 | ✅ NE/CSH Router | `router.py` / `csh_router.py` 存在 | 低 — 达成 |
| 流式协议集中 | ✅ sse_events_module | 协议文档齐全 | 中 — 实现分散在 agent/csh/guest/runner/http_server |
| `aquery_stream` 拆分 | ⚠️ Handler 已迁出 | `stream_handler.py` **987 行**；`ne_full_feature_agent` **1040 行** | **高** — Handler 仍偏大，Agent 未「只路由」 |
| 上下文修剪 | ⚠️ ContextPruningMiddleware | `core/agents/middlewares/context_pruning.py` 297 行 | 低 — 已接入 |
| Tool Archive | 规划 | 未见统一 archive 模块 | 中 — 仍规划 |
| Skill→Workflow | ⚠️ ToolAssemblyPipeline | 按 skill 注入工具，无 workflow 映射表 | 中 |
| 全局单例 Agent | ✅ 参数化 graph | `create_agent_graph` 存在 | 低 |
| 多库 RRF | ✅ multi_kb_search_tool | **1223 行** + 硬编码 KB | 中 — 功能有，质量债重 |
| 关键词 hybrid | ✅ PerKB | `extractor.py` 951 行 | 低 |

---

## 2. 文档「现状问题」是否仍成立

### 2.1 巨石文件（§1.1）

| 文档描述 | 当前行数 | 结论 |
|----------|----------|------|
| ne_full_feature_agent 2241 行 | **1040** | ✅ 已瘦身 ~54% |
| 提示词在 agent 内 | 已迁 `support/prompts.py` | ✅ 改善 |
| 流解析 400 行在 agent | 已迁 `stream_handler.py` | ✅ 改善 |
| `_get_tools` if-else | 已迁 `tool_assembly.py` | ✅ 改善 |
| **但** lightweight_qa_agent | **1349 行** | ❌ 新巨石 |
| **但** agent.py API 层 | **1354 行** | ❌ API 层巨石 |

### 2.2 主→子智能体（§1.2）

文档调用链仍基本准确；额外发现：

- 深研后台任务走 `tasks/runner.py`，与 API 层 think-tag 第三份实现
- CSH 与 NE API 双份维护，文档未强调 **91% 重复**

### 2.3 子智能体扩展（§1.3）

- `deerflow/tools/builtins/task_tool.py` **1149 行** — 文档称改 task_tool，仍是高风险扩展点
- `SubagentLimitMiddleware` 存在；Skill 工具穿透仍靠手工配置

---

## 3. 重构目标完成度（§二）

| # | 目标 | 完成度 | 说明 |
|---|------|--------|------|
| 1 | Skill 驱动路由 | **30%** | 仍依赖 `task_type` / `mode` / `deep_research` 字符串 |
| 2 | 轻量 QA 快速通道 | **90%** | `LightweightQAAgent` 存在但过大 |
| 3 | 主智能体精简 | **50%** | 编排已拆，Agent 文件仍千行级 |
| 4 | 流式处理器独立 | **70%** | Handler 独立，API 层仍大量 SSE 逻辑 |
| 5 | 子智能体可扩展 | **40%** | registry 有，task_tool 仍需改代码 |
| 6 | 工具框架化 | **60%** | ToolAssemblyPipeline 好；multi_kb 仍硬编码 |

---

## 4. 建议写入架构 doc 的勘误

1. **行数更新**：ne agent 1040 行（非 2241）；补充 lightweight_qa、agent.py、csh_agent、rag/service 行数。  
2. **双轨维护**：明确 `core/` 为 canonical，`deerflow/` 为兼容层。  
3. **SSE 实现清单**：列出 6 处 think-tag 实现，标注 canonical 目标模块。  
4. **P0 安全**：internal retrieve、SMS_SECRET、scripts 密钥 — 与功能架构并列章节。  
5. **agent.py 死代码**：`_split_thinking_segments` 未接入流循环。

---

## 5. 推荐下一阶段（对齐文档 Phase）

| Phase | 动作 | 对应文档目标 |
|-------|------|--------------|
| A | 合并 agent/csh API + 统一 think-tag | §4 流式独立 |
| B | 拆分 rag/service patches | 检索架构稳定 |
| C | `kb_registry` 配置化 | 多库/Skill 路由 |
| D | task_tool 瘦身 + subagent 注册表 | §5 子智能体 |
| E | Tool Archive + Context 统一 | §二-1、§二-3 |
