# 成理场地污染水文地质智学助手 (CSH-AI) 模块详解

> **最后同步**：2026-06-08  
> 📊 CSH 路由 → [plans/qa/00_architecture_diagrams.md](../../plans/qa/00_architecture_diagrams.md) 图 **32**

**成理场地污染水文地质智学助手 (CSH-AI)** 是专门针对成都理工大学环境与水文地质学科定制的垂直领域专家智能体系统。系统结合了多知识库并行检索、动态学术技能（Academic Skills）过滤、结构化研究草案（Research Draft）生成与后台深度研究（Background Deep Research）等一系列先进技术。

### CSH 工作流决策（与 NE 对齐）

```text
POST /api/v2/agent-runtime/query  (agent_code=csh)
         │
         ▼
 WorkflowRegistry.resolve()
         │
    workflow 显式命中 deep_research / plan_solve / multi_agent_* ?
         ├─ deep_research ─────────────► DeepResearchWorkflow（大纲/后台深研）
         ├─ plan_solve / multi_agent_* ► CoordinatedAgentWorkflow（平台协同骨架）
         └─ 否 ──► selected_skills 非空 ?
                     ├─ No  ──► StandardAgentWorkflow(lightweight)
                     └─ Yes ──► StandardAgentWorkflow(full)
```

---

## 🏗️ 总体架构与核心组件

CSH-AI 子系统围绕三层架构设计，实现了高内聚低耦合的业务闭环：

```text
POST /api/v2/agent-runtime/query (agent_code=csh)
        │
        ▼
┌──────────────────────┐
│ AgentService /       │
│ WorkflowRegistry     │
└──────────┬───────────┘
         │
    ┌────┼────┬────────────────┬──────────────────────┐
    ▼    ▼    ▼                ▼                      ▼
lightweight  full    deep_research   coordinated(plan/multi-agent)
    │         │              │                │
    ▼         ▼              ▼                ▼
Profiled     Profiled        DeepResearch     CoordinatedAgent
Executor     Executor        Workflow         Workflow
prompt_      + ToolAssembly  + Draft/Runner   + CoordinationStrategy
domain=csh   + csh templates                   + ProfiledExecutor
    │         │              │                │
    └────┬────┴──────────────┴────────────────┘
         ▼
 retrieve_context (domain Profile · MMR_ONLY)
         │
         ▼
 SSE → CSH 前端 :5174
```

### 1. 标准运行入口
*   `AgentService` 根据 `agent_code=csh` 加载 CSH profile，并交由 `WorkflowRegistry` 决定 `lightweight` / `full` / `deep_research` / `plan_solve` / `multi_agent_*`。
*   **轻量通道**与**全功能通道**都通过 `StandardAgentWorkflow + ProfiledAgentExecutor` 执行。
*   **后台深研**由 `DeepResearchWorkflow + ResearchWorkflowService + ResearchTaskRuntime` 统一处理。
*   **协同模式入口** 由 `CoordinatedAgentWorkflow + CoordinationStrategy` 提供，已实现完整的并发子任务调度、心跳保活、超时监控、熔断以及降级 fallback 报告生成。

### 2. 水文地质领域能力配置
*   **核心配置**:
    *   **独立知识库 ID**: `<kb_id_csh>` (成都理工大学水文地质垂直库)；`retrieval_profile.py` 硬编码为 **`domain`** Profile（Stage-2 **MMR_ONLY**，四路 DirectRetriever）。
    *   **检索工具**: `multi_kb_search` 默认绑定上述 KB；`MULTI_KB_SEARCH_NORMAL_MAX_CALLS` 默认 **5**（高于 NE 的 2）。
    *   **中间件配置**: 由 graph builder 注入标准中间件链与 `ContextPruningMiddleware`，在复杂推理中动态剥离 RAG 检索产生的臃肿信息。
    *   **RAG 说明**: [知识库算法和架构.md](../知识库算法和架构.md) · [rag_module.md](./rag_module.md)
*   **特色 Prompt 提示词** (`backend/application/agents/prompt_templates/csh.py`):
    *   预设了极强的学术自省和环境工程专业规范指导。
    *   强制回答满足国家《场地环境调查技术导则》、《地下水环境监测技术规范》等专业标准。

### 3. API 控制层 (`backend/api/routers/agent_runtime.py`)
*   提供统一的 RESTful 接口与标准 SSE 流式传输。
*   `GET /api/v2/agent-runtime/profiles/csh/info` — 获取助手当前的默认配置与可用模型列表。
*   `GET /api/v2/agent-runtime/skill-manifests` — 获取当前系统已注册的技能清单。
*   `POST /api/v2/agent-runtime/query` — 支持普通对话与深度研究生命周期的统一问答端点。

---

## 🔬 深度研究工作流 (Deep Research Workflow)

完整模块说明见 [agent_platform_standardization_plan.md](../agent_platform_standardization_plan.md) §5。

深度研究专为撰写“场地污染水文地质调查报告”、“抽水试验分析报告”等超长、超专业文档而设计，采用**“大纲生成 → 用户确认/修订 → 后台静默执行 → 报告自动生成”**的渐进式闭环逻辑。

### 阶段 1: 大纲与研究方案智能拟定
*   用户在前端发起深度研究请求后，API 层自动唤醒 `ResearchDraftAgent`。
*   根据用户的多模态输入（或上传的场地平面图/检测数据），智能体在几秒内流式返回一份详尽的 **`Research Draft Plan` (研究清单草案)**，其中包含研究维度、潜在污染源、监测井布设方案大纲等。
*   API 将工作流状态写入 `Conversation.metadata_.research_workflow`（`ResearchWorkflowState.AWAITING_USER_CONFIRMATION`）。

### 阶段 2: 人机协同修订与二次确认
*   前端在“工件预览侧栏”渲染大纲，并提供“修改大纲”与“确认启动”两个按钮。
*   用户可以直接输入修改意见（如：“增加对重金属汞的监测频次”）。
*   智能体接收到修订请求 (`confirmation_status="revised"`) 后，调用 `draft_agent.build_draft_stream()` 流式重构大纲，直至用户满意。

### 阶段 3: 后台多线程深研静默运行
*   一旦用户点击“确认启动研究” (`confirmation_status="confirmed"`)：
    1.  API 启动 `ResearchTaskService` 创建一个后台长任务。
    2.  利用 `ResearchTaskRuntime` 启动后台深研执行器。
    3.  SSE 推送后台任务启动事件后主连接结束；前端改订 `/api/v2/agent-runtime/csh/task/{id}/stream`，用户**不必保持页面开启**。
    4.  任务运行器进入后台静默自省循环，并发派遣子智能体调用 `multi_kb_search` 深入检索垂直文献、调取相关标准，进行多维度分析。

### 阶段 4: 学术级长篇报告生成与归档
*   后台研究结束后，调用 `deep_research_report_tool` 汇总全部研究资产。
*   将临时草稿编译为标准的学术 PDF 或 Markdown 工件，物理落盘于用户隔离路径 `users/{user_id}/threads/{thread_id}/outputs/` 中。
*   更新任务状态，并向用户发送完成推送。用户下次登录会话时，工件侧栏会自动弹出完整的专业报告供预览和一键下载。

---

## 🔒 动态学术技能与用户数据安全隔离

### 1. ScopedSkillsProvider 动态技能沙箱
*   CSH-AI 接入了 `ScopedSkillsProvider` 机制。
*   系统根据当前登录用户的权限组，动态扫描并过滤其可用的 `Academic Skills` (学术技能集)。
*   在运行时仅为合规工具组动态编译提示词，防范危险 Python/Shell 技能的代码注入与跨越特权执行。

### 2. ScopedStorage 用户/会话级物理隔离
*   所有的场地调查数据、水文模型图纸及报告工件全部通过 `ScopedStorage` 进行托管。
*   MinIO 物理存储桶内严格实施分层前缀：
    `users/{user_id}/threads/{thread_id}/artifacts/`
*   从底层屏蔽了不同科研团队或师生之间的敏感数据误交叉，达到金融级的数据隔离安全标准。

---

## 🎨 独立前端模块 (`frontend_csh_ai/`)

详细差异说明见 [docs_frontend/ne_full_module.md](../docs_frontend/ne_full_module.md)（CSH-AI 结构与 RadAI 基本一致）。

为了给成理师生提供最纯粹的垂直问答与深研环境，项目配备了独立前端 **`frontend_csh_ai/`**（端口 5174）。
*   **视觉主题**: 结合成都理工大学标志性的“成理绿”学术主题，带来高雅深邃的沉浸式极客体验。
*   **功能面板**:
    *   **思考链面板**: 前端可解析 `thinking` SSE 或 chunk 内 think 标签；**主站 API** 对 think 的拆分程度以 `agent_runtime.py` / `stream_handler` 为准（访客 API 过滤更完整）。
    *   **大纲交互工件侧栏**: 专门适配了深度研究的大纲交互逻辑，支持流式渲染 Markdown Draft 以及实时打分与修订。
    *   **Skills 选择抽屉**: 顶部可选择 Academic Skills，动态影响 Agent 工具与 Prompt。

---

## ✅ 域分域实现（2026-05-31 代码已落地）

| 能力 | 实现入口 | 说明 |
| :--- | :--- | :--- |
| CSH 深研编排 | `deep_research_workflow.py`、`workflow_service.py`、`task_runtime.py` | 实时路由与后台任务统一走平台工作流 |
| CSH Prompt 模板 | `prompt_builder.py` + `prompt_templates/csh.py` | 通过 profile / workflow 自动生效 |
| NE / CSH 分域 | `backend/config/agents/*.yaml` | 领域差异通过 profile、工具策略和 prompt 模板表达 |

## ⚠️ 已知实现限制（与代码审查同步）

| 项 | 说明 | 跟踪 |
| :--- | :--- | :--- |
| 思考标签 | 主站 `agent_runtime.py` 的 `chunk` 可能仍含 think 标签；访客 API 已拆分 | DOC-P0-3 · [agent_platform_standardization_plan.md](../agent_platform_standardization_plan.md) §11 |
| 深研子智能体 Prompt | `core/tools/task_tool.py` 子智能体仍为通用模板 | P1 · [plans/debug/backend_custom_round5_core_security_tests.md](../../plans/debug/backend_custom_round5_core_security_tests.md) |
