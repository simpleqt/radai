# 第八轮审查：NE vs CSH 全功能 Agent 对照

**文件**：
- `ne_full_feature_agent.py`（1040 行）
- `csh_full_feature_agent.py`（965 行）  
**diff 行数约 274**（结构高度同构）  
**日期**：2026-05-30 · **只读**

---

## 1. 相同点（重复面）

| 能力 | NE | CSH |
|------|----|-----|
| 流式主循环 | `aquery_stream` + `astream_events` v2 | 同构 |
| Handler | `NEAgentStreamHandler` | **同名类**（命名误导） |
| 工具装配 | `ToolAssemblyPipeline` | 同 |
| 轻量分支 | `LightweightQAAgent` | 同（**共用 NE Prompt**） |
| 预算 | `set_tool_budgets` / `reset_tool_budgets` | 同 |
| 深研超时/idle/ping | 同类常量 | 同 |

**维护成本**：修一处 bug 常需 **双份修改**；已有 P1-1 抽公共模块建议。

---

## 2. 差异点（应保留）

| 维度 | NE | CSH |
|------|----|-----|
| System Prompt | `prompts.build_system_prompt` | `csh_prompts.build_system_prompt` |
| 默认 KB | `KB_IDS` 核电子学双库 | 单库 `<kb_id_csh_...>` |
| 角色名 | 核电子学 | `AGENT_ROLE_NAME` 场地水文 |
| CONFIG | 可选 deerflow | 自动设 `DEER_FLOW_CONFIG_PATH`（L41–45） |
| `reset_turn_kb` | **1 次**（L620） | **2 次**（L577–578）← **bug** |

---

## 3. `DeepResearchOrchestratorAgent`

```17:18:backend/custom/agents/deep_research_orchestrator_agent.py
class DeepResearchOrchestratorAgent(NEFullFeatureAgent):
```

- 继承 NE，追加 `<deep_research_orchestrator_hard_rules>`（L34–45）。
- `_get_tools` 移除主控 `multi_kb_search` / `web_search`（L52–58），检索委派子智能体。
- **CSH 深研路由仍实例化此类** → 编排 Prompt 为 **NE 深研 + NE base system**（P0-1）。

---

## 4. 流式路径数据流（全功能模式）

```text
┌────────────────────┐ ┌────────────────────┐ ┌────────────────────┐ ┌────────────────────┐ ┌────────────────────┐ ┌────────────────────┐
│agent/csh_agent API │ │ NE/CSHAgentRouter  │ │  FullFeatureAgent  │ │     LangGraph      │ │NEAgentStreamHandler│ │      SSE 客户端       │
└────────────────────┘ └────────────────────┘ └────────────────────┘ └────────────────────┘ └────────────────────┘ └────────────────────┘

  ┌─ LOOP: 每个 event
  └─ END
  [Note API,API_SSE: API 层对 chunk 是否过滤 think 见 P0-3]

```

- Agent 层 **不** 做 think-tag 过滤；依赖 API 或 Handler（Handler 仅原生 reasoning）。
- `explore/detail` 绕过 Router，直接 `AgentClass(lightweight_mode=True)`。

---

## 5. 工具预算对照

**NE**（`ne_full_feature_agent.py` L636–646）与 **CSH**（L594–604）逻辑一致：

- 深研：`multi_kb_search: 3`, `web_search: 10`, `academic_search: 5`
- 普通 + `academic-search` skill：`multi_kb` = `MULTI_KB_SEARCH_MAX_CALLS`（NE/CSH 默认 env 可能不同），`academic_search: 2`
- 普通无 academic skill：无 `academic_search` 预算项

**CSH** `MULTI_KB_SEARCH_MAX_CALLS` 默认 5（L62–64），NE 需核对同 env 键。

---

## 6. 重构建议（仅文档，不实施）

### 6.1 短期

1. 删除 CSH 重复 `reset_turn_kb_context_budget()`。  
2. `NEAgentStreamHandler` → `AgentStreamHandler` 或按域注入 formatter。  
3. 抽出 `FullFeatureAgentBase`：仅 `_build_system_prompt` / `KB_IDS` / 日志前缀 由子类实现。

### 6.2 中期

- `CSHDeepResearchOrchestratorAgent(CSHFullFeatureAgent)` 或在 orchestrator 增加 `prompt_domain`。
- 轻量路径：`LightweightQAAgent(prompt_domain="csh"|"ne")`。

### 6.3 不宜合并

- CSH/NE 的 `csh_prompts` vs `prompts` 内容差异大，不应合并为单 Prompt 文件。

---

## 7. 测试缺口

- 无对比测试断言：同问题 NE/CSH 使用不同 system prompt 片段。  
- 无测试覆盖 CSH 双次 reset 锁行为。  
- 深研 orchestrator 在 CSH router 下应断言 orchestrator 类名/Prompt 关键字（修复 P0-1 后）。

---

*关联：[backend_custom_vs_architecture_redesign.md](./backend_custom_vs_architecture_redesign.md) · [deep_dive_v2_agent_api.md](./deep_dive_v2_agent_api.md)*
