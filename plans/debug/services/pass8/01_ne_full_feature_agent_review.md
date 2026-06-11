# 第八遍：`ne_full_feature_agent.py` 审查

**文件**：`backend/custom/agents/ne_full_feature_agent.py`（约 1040 行）  
**关联**：`agent_architecture_redesign.md` §1.1 巨石文件问题  
**与 `backend/services` 关系**：**不直接 import services**；经 `ToolAssemblyPipeline` → `chat_file_search`；文件内容由 `agent.py` 预注入。

---

## 1. 职责分布（重构进度）

| 职责 | 当前位置 | 架构 doc 目标 | 状态 |
|------|----------|---------------|------|
| 系统提示词拼接 | `_build_system_prompt` ~80 行 skill_enforcement | `support/prompts.py` | 部分委托 `shared_build_system_prompt` |
| 工具组装 | `_get_tools` → ToolAssemblyPipeline | tool_assembly | ✓ 已委托 |
| 流式 SSE | `aquery_stream` ~430 行 + handler | stream_handler | 事件在 handler，循环仍在 agent |
| 轻量委托 | `aquery_stream` 内 early return | LightweightQAAgent | ✓ |
| 深研兜底/超时 | `aquery_stream` 后半 | orchestrator | 仍耦合 |
| 工件注册 | `_emit_graph_artifacts` + handler | artifact_utils | 用 `shared_build_registered_artifact_metadata` |
| 硬编码 KB_IDS | 类常量 | 配置/请求传入 | 仍硬编码 |

**结论**：较 2241 行巨石已瘦身，但 `aquery_stream` 仍偏大，深研与普通过程交织。

---

## 2. 与会话文件（services）的间接耦合

```text
agent.py 注入 runtime_question + file_ids
        ↓
NEFullFeatureAgent.aquery_stream(question=runtime_question, image_contents=...)
        ↓
lightweight_mode? ──yes──→ LightweightQAAgent（内联 ChatFileIndex）
        │
        no → LangGraph + ToolAssembly
              └── chat_file_search（若 has_thread_file_index）

```

| 问题 | 级别 | 说明 |
|------|------|------|
| NE 类不感知 `file_ids` 字段 | — | 依赖 question 已拼接，设计如此 |
| 深研模式仍带文件提示 | P2 | 深研 + 上传文档场景交互未单独文档化 |
| `image_contents` 仅传 Lightweight/Graph 多模态 | — | 与 metadata base64 双路径（pass7） |

---

## 3. 代码质量问题

### NE-P1-01 硬编码 `KB_IDS`

```python
KB_IDS = [
    "<kb_id_ne>",
    "<kb_id_paper>",
]
```

环境/租户切换易错；应来自配置或 API 入参 exclusively。

### NE-P1-02 `DEER_FLOW_CONFIG_PATH` 副作用

模块 import 时写 `os.environ`——测试并行与多配置实例困难（P2）。

### NE-P1-03 深研与普通过程同函数

`aquery_stream` 内含：

- 超时/idle/stall 多套计时
- checkpoint 续跑
- finalize 报告
- 普通过程 LangGraph 事件循环

单函数圈复杂度极高，回归风险大。

### NE-P2-01 `_build_system_prompt` 补丁式追加

多个 `if "html-ppt" in skills` 字符串块——与架构目标「Skill→Workflow」仍有距离。

### NE-P2-02 深研 KB 策略与生产 RAG 不一致

提示要求模型自提取 hl/ll keywords 调 `multi_kb_search`，而生产 KB 侧已用 `PerKBKeywordExtractor` hybrid——**模型仍可能慢/幻觉关键词**（与 services 无关，属 Agent 提示）。

### NE-P3-01 `get_info` / `ainvoke` 包装

薄封装，维护成本低。

---

## 4. 与 `router.py` 协作

```python
# router: 无 skills → lightweight
if not self._has_user_selected_skills():
    return "lightweight"
```

| 行为 | 影响 |
|------|------|
| 默认无 skill → 轻量 | 大多数 NE 用户走 Lightweight，**不用** chat_file_search 工具 |
| 用户勾选 skill → full | 才有 ToolAssembly + 可能 chat_file_search |

与产品「默认即全功能」预期可能不符——属路由策略，非 services bug。

---

## 5. 工件路径（`user_file_service`）

`_emit_graph_artifacts` / `_finalize_deep_research_report` 调用：

```python
await shared_build_registered_artifact_metadata(
    internal_thread_id=effective_thread_id,
    artifact_path=...,
    minio_object=...,
)
```

- 依赖 `build_internal_thread_key` 解析 user_id。  
- 与 `ChatFileParser` 无交；审查 services 时计入 **user_file_service** 集成（pass4/7）。

---

## 6. 建议重构切片（对齐 architecture doc）

| 优先级 | 动作 |
|--------|------|
| 1 | 将 `aquery_stream` 深研分支迁入 `DeepResearchOrchestratorAgent` 专用 stream 适配器 |
| 2 | 普通过程仅保留：config → handler → graph.astream 循环 |
| 3 | `KB_IDS` 移入 settings |
| 4 | skill_enforcement 块迁 `prompts.py` 或 skill 元数据 |

---

## 7. 测试建议

- 深研超时后是否仍 emit artifact  
- lightweight_mode 委托是否传递 `image_contents`  
- selected_skills 含 academic-search 时 tool budget

---

*第八遍 — NEFullFeatureAgent*
