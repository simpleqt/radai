# 成理场地污染水文地质智学助手 (CSH-AI) 模块详解

> **最后同步**：2026-07-14
> 📊 CSH 路由 → [plans/qa/00_architecture_diagrams.md](../../plans/qa/00_architecture_diagrams.md) 图 **32**

**成理场地污染水文地质智学助手 (CSH-AI)** 是专门针对成都理工大学环境与水文地质学科定制的垂直领域专家智能体系统。系统结合了多知识库并行检索、动态学术技能（Academic Skills）过滤、协同深度研究和报告工件生成等能力。

### CSH 工作流决策（与 NE 对齐）

```text
POST /api/v2/agent-runtime/query  (agent_code=csh)
         │
         ▼
 WorkflowRegistry.resolve()
         │
    workflow 显式命中 deep_research / plan_solve / multi_agent_* ?
         ├─ deep_research ─────────────► CoordinatedAgentWorkflow（同链路深研）
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
Profiled     Profiled        Coordinated      CoordinatedAgent
Executor     Executor        AgentWorkflow    Workflow
prompt_      + ToolAssembly  + RuntimeTool    + CoordinationStrategy
domain=csh   + csh templates  Invoker         + ProfiledExecutor
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
*   **深度研究**由 `CoordinatedAgentWorkflow + CoordinationStrategy + RuntimeToolInvoker` 执行；HTTP 层将 `deep_research` 和 `plan_solve` 包装为可重连后台任务。
*   `POST /api/v2/agent-runtime/query` 先返回任务启动确认，前端随后通过 `GET /api/v2/agent-runtime/{agent_code}/task/{task_id}/status|stream` 获取状态、进度和最终答案，并可通过 `POST .../cancel` 取消。
*   **协同模式入口**已实现完整的并发子任务调度、心跳保活、超时监控、熔断以及降级 fallback 报告生成。

### 2. 水文地质领域能力配置
*   **核心配置**:
    *   **独立知识库 ID**: `b4c3f145-de5f-42fb-88de-2d4856d57460` (成都理工大学水文地质垂直库)；`retrieval_profile.py` 硬编码为 **`domain`** Profile（Stage-2 **MMR_ONLY**，四路 DirectRetriever）。
    *   **检索工具**: `multi_kb_search` 默认绑定上述 KB；`MULTI_KB_SEARCH_NORMAL_MAX_CALLS` 默认 **5**（高于 NE 的 2）。
    *   **中间件配置**: 由 graph builder 注入标准中间件链与 `ContextPruningMiddleware`，在复杂推理中动态剥离 RAG 检索产生的臃肿信息。
    *   **RAG 说明**: [知识库算法和架构.md](../知识库算法和架构.md) · [rag_module.md](./rag_module.md)
*   **特色 Prompt 提示词** (`backend/application/agents/prompt_templates/csh.py`):
    *   预设了极强的学术自省和环境工程专业规范指导。
    *   强制回答满足国家《场地环境调查技术导则》、《地下水环境监测技术规范》等专业标准。

### 3. API 控制层 (`backend/api/routers/agent_runtime.py` / `agent_runtime_v2.py`)
*   提供统一的 RESTful 接口与标准 SSE 流式传输。
*   `agent_runtime_v2.py` 在主 API 聚合注册 `/api/v2/agent-runtime`、`/api/v2/guest`、`/api/v2/explore`；独立访客 API 直接注册 `agent_runtime.py` 与 `guest_agent.py`，不注册 `explore`。
*   `GET /api/v2/agent-runtime/profiles/csh/info` — 获取助手当前的默认配置与可用模型列表。
*   `GET /api/v2/agent-runtime/skill-manifests` — 获取当前系统已注册的技能清单。
*   `POST /api/v2/agent-runtime/query` — 支持普通对话与深度研究生命周期的统一问答端点。

---

## 🔬 深度研究工作流 (Deep Research Workflow)

完整模块说明见 [agent_engine_module.md](./agent_engine_module.md) 与 [../agent_development_guide.md](../agent_development_guide.md)。

深度研究专为撰写“场地污染水文地质调查报告”、“抽水试验分析报告”等超长、超专业文档而设计，当前采用**“规划拆解 → 并发证据收集 → 写作汇总 → 工件生成”**的同链路协同逻辑。

### 阶段 1: 规划拆解
*   用户通过 `/api/v2/agent-runtime/query` 指定 `agent_code=csh` 和 `workflow=deep_research` 后，路由层创建可重连任务，任务内部由 `WorkflowRegistry` 路由到 `CoordinatedAgentWorkflow`。
*   Planner 生成研究任务清单，并通过 `plan.created` 标准事件推送给前端。

### 阶段 2: 并发证据收集
*   每个 Researcher 分支按任务问题并发执行预检索。
*   KB、联网和报告保存等主动工具调用统一通过 `RuntimeToolInvoker` 装配和校验，不直接 import 底层 provider。
*   检索过程受 `DEEP_RESEARCH_KB_TIMEOUT_SECONDS`、`DEEP_RESEARCH_WEB_TIMEOUT_SECONDS`、`ResourceLeaseManager`（memory/Redis 租约，含 TTL 与 fencing）和熔断器保护。

### 阶段 3: 写作汇总与可见状态持久化
*   Writer/Reviewer 汇总各分支结果；过程事件由后台 task stream 流式输出，最终 Markdown 使用 `message.replace`（并以 `done`/终态状态收束），断线后可按 `task_id` 续接。
*   父线程 checkpoint 会写入可见问答消息，并附带 `research_workflow` 元数据，包括研究任务、handoff 摘要和已使用工具。

### 阶段 4: 学术级长篇报告生成与归档
*   Workflow 通过 `RuntimeToolInvoker` 调用 `save_deep_research_report`。
*   工具生成可下载 DOCX，并通过用户/线程隔离路径和 MinIO 注册返回 artifact payload。
*   前端在 task stream 中接收 `workflow.step` artifact 事件，展示报告下载卡片。

---

## 🔒 动态学术技能与用户数据安全隔离

### 1. 托管 Skill + ScopedSkillsProvider 动态技能沙箱
*   CSH-AI 接入了 `SkillRuntimeResolver`、`agent_skill_bindings` 与 `ScopedSkillsProvider` 机制。
*   系统根据当前登录用户角色、Skill ACL、Agent-Skill 绑定、domain 与 workflow，动态过滤其可用的 `Academic Skills`。
*   运行时只为合规 Skill 动态编译 Prompt，并由 ToolPolicy 决定是否装配 `excel-experiment-analysis`、`academic-search`、`html-ppt` 等工具。
*   预置实验数据集初始化仍使用旧编码 `csh_full_feature`，`DatasetCRUD` 会将其与运行时 `csh` 互相映射。

### 2. ScopedStorage 用户/会话级物理隔离
*   所有的场地调查数据、水文模型图纸及报告工件通过 `backend/core/storage/scoped_storage.py` 与文件中心进行托管。
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
    *   **深研进度与工件侧栏**: 渲染 `plan.created`、`workflow.step`、`coordination.*` 与 artifact 事件，展示并发研究进度和报告下载入口。
    *   **Skills 选择抽屉**: 顶部可选择 Academic Skills，动态影响 Agent 工具与 Prompt。

---

## ✅ 域分域实现（2026-05-31 代码已落地）

| 能力 | 实现入口 | 说明 |
| :--- | :--- | :--- |
| CSH 深研编排 | `coordinated_agent_workflow.py`、`runtime_invoker.py` | 同链路协同深研、策略化工具调用和报告工件生成 |
| CSH Prompt 模板 | `prompt_builder.py` + `prompt_templates/csh.py` | 通过 profile / workflow 自动生效 |
| NE / CSH 分域 | `backend/config/agents/*.yaml` | 领域差异通过 profile、工具策略和 prompt 模板表达 |

## ⚠️ 已知实现限制（与代码审查同步）

| 项 | 说明 | 跟踪 |
| :--- | :--- | :--- |
| 思考标签 | 主站 `agent_runtime.py` 的 `chunk` 可能仍含 think 标签；访客 API 已拆分 | DOC-P0-3 · [../agent_development_guide.md](../agent_development_guide.md) |
| 深研子智能体 Prompt | 已迁移至 `runtime_agents.yaml`（`deep-research` 等） | 见 [codex_like_agent_runtime_optimization_plan.md](../codex_like_agent_runtime_optimization_plan.md) |
