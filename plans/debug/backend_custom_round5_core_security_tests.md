# backend/custom 第五轮审查：core 耦合 · 安全 · 测试 · 子智能体

**范围**：`backend/custom` + 其 **直接依赖的 core 边界**（`task_tool`、`clarification` 等）  
**原则**：只读，不改代码  
**日期**：2026-05-30  
**前置**：[backend_custom_code_review.md](./backend_custom_code_review.md)、[deep_dive_stream_handler_sse.md](./deep_dive_stream_handler_sse.md)

---

## 1. 结论摘要

| 维度 | 结论 |
|------|------|
| **core 反向依赖 custom** | `task_tool.py` 依赖 `custom` 的 `stream_utils` + `ToolAssemblyPipeline` — 分层倒置，custom 非纯「业务插件层」 |
| **CSH 域缺口延伸** | 深研**子智能体** Prompt 在 `core/tools/task_tool.py`，仍为通用/NE 向，CSH 主控错域时子任务也错域 |
| **StreamHandler** | 只拆 **原生 reasoning** 为 `thinking`；**不**处理 `` 标签 — 与 API 层 guest 行为不一致 |
| **安全** | Excel `file_path` 任意可读路径；`academic_search` 同步+线程池；pandoc subprocess |
| **测试** | **`backend/custom` 下 0 个 test 文件** |

---

## 2. core ↔ custom 依赖图

```text
┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ end            │  │ Task           │  │ NE             │  │ API            │  │ SH             │  │ Clar           │
└────────────────┘  └────────────────┘  └────────────────┘  └────────────────┘  └────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
┌────────────────┐  ┌──────────────────────┐  ┌────────────────────┐  ┌────────────────┐  ┌────────────────────────────────┐
│ stream_utils   │  │ ToolAssemblyPipeline │  │ create_agent_graph │  │ NEAgentRouter  │  │ custom.workflow.research_state │
└────────────────┘  └──────────────────────┘  └────────────────────┘  └────────────────┘  └────────────────────────────────┘

```

### 2.1 core → custom（已知）

| core 模块 | 引用 custom | 用途 |
|-----------|-------------|------|
| `core/tools/task_tool.py` | `stream_utils`, `ToolAssemblyPipeline` | 子智能体工具集与事件元数据 |
| `core/agents/middlewares/clarification.py` | `ResearchWorkflowState` | 澄清与工作流状态对齐 |

**问题（P2 架构）**：`core` 不应依赖 `custom`；长期应 `core.agents.streaming` + `core.agents.tools.assembly` 接口，custom 注入领域 Prompt/KB。

### 2.2 custom → core（正常）

`NEFullFeatureAgent` → `create_agent_graph`、`RuntimeFeatures`、各 middleware — 符合设计。

---

## 3. P0 延伸：子智能体域（`task_tool.py`）

**位置**：`backend/core/tools/task_tool.py` L40-70 `_SUBAGENT_CONFIGS["deep-research"]`

| 项 | 内容 |
|----|------|
| system_prompt | 通用「深度研究子智能体」，无 CSH/NE 分支 |
| kb_ids | 由主智能体 `configurable.kb_ids` 传入 — **KB 可对，Prompt 仍通用** |
| thread_id | `subagent-exec-{task_id}` 前缀 — `multi_kb_search` 用此判断子线程超时（`multi_kb_search_tool.py` L956） |

**影响**：CSH 深研即使用对 `kb_ids`，子智能体仍按通用规则作答，无场地污染/水文地质规范强调。

**建议（文档级，待产品确认）**：

- `LocalSubagentConfig` 增加 `domain: ne | csh` 或从 runtime metadata 选择 prompt 模板  
- 与 P0-1 `CSHDeepResearchOrchestratorAgent` 一并规划  

**优先级**：P1（主控 Prompt 错域为 P0；子智能体为增强）

---

## 4. `NEAgentStreamHandler` 补充审查

> 详见 [deep_dive_stream_handler_sse.md](./deep_dive_stream_handler_sse.md)；本节仅 **第五轮新增**。

### 4.1 思考内容三条路径

| 路径 | 机制 | think 标签 in chunk |
|------|------|---------------------|
| 全功能 + Handler | `extract_reasoning_content` → `thinking` SSE | **不处理**；标签可能在 `chunk` |
| 轻量 Lightweight | 原生 reasoning + `_strip_thinking_markup`（历史/后处理） | 部分剥离 |
| guest API | `_split_thinking_segments` | ✅ 拆分 |

**Handler `_on_model_stream`（L447-536）**：`reasoning_text` 与 `chunk_text` 分流；`chunk_text` 经 `StreamingTokenResolver` 仅替换 `[IMAGE:xxx]`。

### 4.2 普通模式工具轮抑制正文（L487-490）

含 `tool_call_chunks` 的模型轮次在 **非深研** 下 **不转发** 流式 chunk，依赖 `_on_model_end` 的 `missing_text` 补尾（L550+）。

| 风险 | 说明 |
|------|------|
| 补尾失败 | 用户可见输出为空，但 `log_session_diagnostic` 会 WARNING |
| 与前端 | 过程 event 仍有 tool_start/end，UI 可能显示「有工具无正文」 |

### 4.3 深研超时与 grace（`ne_full_feature_agent` L768-825）

| 机制 | 常量/行为 |
|------|-----------|
| 总时长上限 | `DEEP_RESEARCH_STREAM_TIMEOUT_SECONDS` 1800 |
| 空闲 | `DEEP_RESEARCH_IDLE_TIMEOUT_SECONDS` 600 |
| 子任务 stall grace | `DEEP_RESEARCH_TASK_STALL_GRACE_SECONDS` 300 |
| finalize grace | 延长 `deep_research_timeout_budget` |

逻辑密集，**无单测**；改超时易回归。

### 4.4 未解析 IMAGE token（L500-508）

流式阶段警告 `[IMAGE:nnn]` 未注册 — 模型幻觉或跨轮历史 token；**非崩溃**，但前端可能破图。

---

## 5. 安全审查（custom 工具）

### 5.1 Excel `file_path`（P2→P1 视部署）

**文件**：`excel_data_analysis_tool.py` L69-73

```python
if file_path:
    p = Path(file_path)
    if p.exists():
        return str(p.resolve()), p.name
```

| 风险 | 条件 |
|------|------|
| 任意文件读取 | 模型传入 `/etc/passwd` 等若进程可读且 `exists()` |
| 路径遍历 | 依赖 Agent 不泄露服务器路径；无 chroot/白名单目录 |

**建议**：仅允许 `settings.upload_dir` / 用户 thread 目录下路径；或禁止 `file_path` 仅 `dataset_code`。

### 5.2 `academic_search` 同步工具（P2）

**文件**：`acdemic_search_tool.py` L91-106 `_run_async`

在 LangGraph 已运行 event loop 时用 **ThreadPoolExecutor(1)** 跑异步检索 — 高并发下线程排队，非直接漏洞。

### 5.3 `deep_research_report_tool` pandoc（P2）

**L199-215**：`subprocess.run` 调用 `pandoc`，输入为受控临时 md — 风险低；需确保 `pandoc` 在 PATH 且版本固定。

### 5.4 `html_ppt_tool`（P3）

模板来自 `backend/skills/public/html-ppt`，非用户路径 — OK。输出上传 MinIO — 需配额与大小限制（查工具内是否有）。

### 5.5 Explore 开放 API（P1）

见 [backend_custom_docs_vs_code_gaps.md](./backend_custom_docs_vs_code_gaps.md) — 成本与滥用。

---

## 6. 测试与可观测性缺口

### 6.1 单元测试

```text
backend/custom/**/test*.py  → 0 文件

```

| 优先补测模块 | 理由 |
|--------------|------|
| `thinking_filter`（待抽取） | 跨 chunk 边界 |
| `workflow/research_state.normalize_*` | 纯函数 |
| `stream_utils.make_process_event` | 协议形状 |
| `ToolAssemblyPipeline.assemble` | 工具名集合快照 |
| `ResearchDraftAgent._sanitize_thinking_text` | 回归 |

### 6.2 集成测试建议

| 场景 | 断言 |
|------|------|
| NE 轻量 query | 无 skill 时走 Lightweight，有 KB 注入 |
| CSH 轻量（P0-2 后） | 回答领域词 |
| guest think 标签 | chunk 无 `` |
| 深研 confirmed → task stream | 收到 `background_task_started` + task SSE chunk |
| explore detail domain=csh | 不出现「核电子学智能体」 |

### 6.3 可观测性（已有 vs 缺失）

| 已有 | 缺失 |
|------|------|
| `NEAgentStreamHandler.log_session_diagnostic` | 统一 trace_id 贯穿 API→Router→Handler |
| 阶段耗时日志 Lightweight | Prometheus 指标（QPS、tool 预算耗尽率） |
| `[Agent V2] 流式生成完成` 统计 | 结构化 JSON 日志规范 |

---

## 7. 与前端 SSE 对齐（摘要）

完整矩阵：[audit_frontend_backend_sse.md](./audit_frontend_backend_sse.md)

| 第五轮强调 | 说明 |
|------------|------|
| `status` 键名 | Handler 用 `content`；agent 转发深研有时用 `status` 键 — 前端已兼容 |
| `follow_up` 对象数组 | 当前仅 string[]；未来扩展需改 TS |
| `subscribeTaskStream` 无 `draft_chunk` | 后台流不期望草案 — OK |
| RadAI/CSH `agent-v2.ts` 重复 | 维护双份 |

**思考泄漏**：前端 `ReasoningBlock` 依赖 `thinking` 事件；主站若 think 进 `chunk` 会显示在正文区 — **产品 bug**。

---

## 8. 跨模块重复（custom 相关摘录）

来自 [cross_cutting_duplication.md](./cross_cutting_duplication.md)：

| 重复项 | custom 涉及 |
|--------|-------------|
| think-tag 6+ 实现 | agent/guest/runner/lightweight |
| `make_process_event` | stream_utils vs lightweight 本地副本 |
| MinIO 私有 API | explore_learning |
| KB UUID 三套 | kb_config / NE.KB_IDS / DB binding |

---

## 9. 优先级汇总（第五轮新增项）

| ID | 级别 | 摘要 |
|----|------|------|
| R5-1 | P1 | `task_tool` 深研子智能体 CSH Prompt |
| R5-2 | P2 | core 依赖 custom 分层倒置 |
| R5-3 | P2 | Excel `file_path` 路径白名单 |
| R5-4 | P2 | 无 custom 单测 |
| R5-5 | P2 | Handler 工具轮抑制 chunk 导致空正文 |
| R5-6 | P3 | `NEAgentStreamHandler` 重命名 |

P0 仍以 [backend_custom_p0_fix_checklist.md](./backend_custom_p0_fix_checklist.md) 为准。

---

## 10. 相关文档索引

| 文档 | 内容 |
|------|------|
| [backend_custom_p0_implementation_guide.md](./backend_custom_p0_implementation_guide.md) | P0 改法 |
| [backend_custom_github_issues.md](./backend_custom_github_issues.md) | Issue 模板 |
| [deep_dive_lightweight_qa_agent.md](./deep_dive_lightweight_qa_agent.md) | 轻量 1349 行拆解 |
| [deep_dive_v2_agent_api.md](./deep_dive_v2_agent_api.md) | agent/csh 重复 |

---

*第五轮完成；未修改任何源代码。*
