# Backend 面试题库

> 基于 `backend/` 项目源码与架构文档整理，按**模块**与**难度层次**组织。  
> 适用场景：后端/AI 工程岗技术面、架构评审、新人 onboarding 考核。

---

## 答案阅读说明（第三版扩写）

每道模块题（01–12）尽量按以下结构组织，便于**理解原理**而非死记结论：

| 小节 | 作用 |
|------|------|
| **一句话结论** | 30 秒内能复述的核心答案 |
| **背景 / 为什么问** | 考察点、在系统里的位置 |
| **详细解释** | 分步骤、表格、**ASCII 框线图**（终端友好） |
| **在本项目中的实现** | 文件路径、关键代码引用 |
| **走一遍具体例子** | 用户/运维真实场景 |
| **常见误区** | ❌ 错误理解 vs ✅ 正确理解 |
| **延伸追问** | L2/L3 面试加深方向 |

场景题（15）按 **现象 → 根因 → 排查 → 修复 → 预防** 展开。  
速查（14）和模拟卷（13）保持精简，细节回查对应模块。

### 系统总览图（快速预览）

```text
┌─────────────────────────────────────────────────────────────────┐
│                     前端 / MCP 客户端                            │
│  RadAI :5173  │  CSH-AI :5174  │  Guest :8085  │  MCP SSE      │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP / SSE
         ┌─────────────────┴─────────────────┐
         ▼                                   ▼
┌────────────────────────────┐    ┌────────────────────────────┐
│ 主 API :8082               │    │ 访客 API :8083              │
│ v1 RAG/KB · v2 Agent       │    │ 独立 checkpoint             │
└─────────────┬──────────────┘    └─────────────┬──────────────┘
              │                                  │
              └──────────────┬───────────────────┘
                             ▼
              ┌──────────────────────────────┐
              │ AgentService + Workflow      │
              │ Lightweight / Full / 深研     │
              │ CoordinatedAgentWorkflow     │
              └──────────────┬───────────────┘
                             ▼
              ┌──────────────────────────────┐
              │ RAG Service (retrieve_context)│
              └──────────────┬───────────────┘
                             │
    ┌────────────────────────┼────────────────────────┐
    ▼            ▼           ▼           ▼            ▼
 MySQL      SQLite CP    Milvus    NebulaGraph    Workers/Kafka
```

> 更多图表（时序图、状态机、ER 图等）见 [00_architecture_diagrams.md](./00_architecture_diagrams.md)。

---

## 难度层次说明

| 层次 | 标签 | 面向对象 | 考察重点 |
|------|------|----------|----------|
| **L1 初级** | 🟢 | 1–3 年 | 概念、技术栈、模块职责、基本调用链 |
| **L2 中级** | 🟡 | 3–5 年 | 实现细节、配置项、中间件、数据流、排障 |
| **L3 高级** | 🔴 | 5+ 年 / 架构 | 权衡取舍、并发/一致性、扩展性、已知局限 |

---

## 模块索引

| 序号 | 模块 | 文件 | 核心话题 |
|------|------|------|----------|
| **00** | **架构图表集** | [**00_architecture_diagrams.md**](./00_architecture_diagrams.md) | **38 张** ASCII 框线图（推荐先看） |
| 01 | 总体架构与入口 | [01_overview_architecture.md](./01_overview_architecture.md) | FastAPI 双服务、CLI、配置分层、启动生命周期 |
| 02 | 智能体系统 | [02_agent_system.md](./02_agent_system.md) | AgentService、WorkflowRegistry、AgentProfile、Middleware 链、深度研究 |
| 03 | RAG 检索 | [03_rag_retrieval.md](./03_rag_retrieval.md) | LightRAG、RetrievalProfile、多路召回、融合过滤 |
| 04 | API 与流式 | [04_api_streaming.md](./04_api_streaming.md) | AgentEvent、SSEEncoder、agent-runtime SSE、Thinking 剥离 |
| 05 | 数据库与认证 | [05_database_auth.md](./05_database_auth.md) | MySQL/Redis/SQLite、NullPool、CAS/API Key |
| 06 | 工具与 Skills | [06_tools_skills.md](./06_tools_skills.md) | ToolAssemblyPipeline、PolicyToolAssembler、ScopedSkillsProvider |
| 07 | Worker 与 Kafka | [07_workers_kafka.md](./07_workers_kafka.md) | 文档处理 Worker、入库 Worker、深度研究后台任务 |
| 08 | 工程实践 | [08_engineering_practices.md](./08_engineering_practices.md) | 设计模式、降级策略、已知技术债、扩展方向 |
| 09 | LLM 模型与工厂 | [09_llm_models.md](./09_llm_models.md) | create_chat_model、配置优先级、thinking、patch 类 |
| 10 | 知识库管理 | [10_knowledge_base.md](./10_knowledge_base.md) | KB Manager、入库状态机、Agent 绑定、会话文件 |
| 11 | Guardrails 与安全 | [11_guardrails_security.md](./11_guardrails_security.md) | ToolGuardrail、Allowlist、RAG 注入攻击 |
| 12 | 探索与追问 | [12_explore_followup.md](./12_explore_followup.md) | Explore API、Follow-up、grounding |
| 13 | 模拟面试卷 | [13_mock_interview.md](./13_mock_interview.md) | 5 套卷（60/45/30/90 min）、评分与红旗 |
| 14 | 速查手册 | [14_quick_reference.md](./14_quick_reference.md) | Flashcards、环境变量、日志、mnemonic |
| 15 | 综合场景题 | [15_cross_module_scenarios.md](./15_cross_module_scenarios.md) | 12 个跨模块排障 + 2 道设计题 |
| 16 | 会话与工件 | [16_conversations_artifacts.md](./16_conversations_artifacts.md) | thread_key、rollback、ScopedStorage、Agent-KB 绑定 |
| 17 | 扩展模块 | [17_extended_modules.md](./17_extended_modules.md) | ASR、数据集、抽取、Index Sync、部署 |
| 18 | 快问快答 140 题 | [18_interview_drill.md](./18_interview_drill.md) | 考前冲刺、Middleware/深研专章 |
| **19** | **智能体架构与实现（专题）** | [**19_agent_architecture_implementation.md**](./19_agent_architecture_implementation.md) | **AgentService、WorkflowRegistry、ProfiledGraphBuilder、Middleware 链、子 Agent** |
| **20** | **智能体模拟面试卷** | [**20_agent_mock_interview.md**](./20_agent_mock_interview.md) | Agent 岗 45–90 min 专卷 |
| **21** | **Middleware 链深度** | [**21_middleware_chain_deep_dive.md**](./21_middleware_chain_deep_dive.md) | 8 层 + ContextPruning 逐层 15 题 |
| **22** | **Agent 源码走读** | [**22_agent_source_walkthrough.md**](./22_agent_source_walkthrough.md) | W1–W40 按文件精读（含深研 W31–W40） |
| **23** | **Lightweight vs Full** | [**23_lightweight_vs_full.md**](./23_lightweight_vs_full.md) | WorkflowRegistry 路由/工具/检索/Middleware/SSE 对照 20 题 |
| **24** | **文档逐节对照** | [**24_agent_engine_doc_mapping.md**](./24_agent_engine_doc_mapping.md) | agent_engine_module ↔ 源码 ↔ 面试答法 18 题 |
| **25** | **深度研究 CoordinatedAgentWorkflow** | [**25_deep_research_orchestrator.md**](./25_deep_research_orchestrator.md) | 大纲→确认→并行子Agent→报告 18 题 |

---

## 推荐面试组合

### 全栈 AI 后端（45 分钟）

1. L1：01-Q1、02-Q1、03-Q1（架构鸟瞰）
2. L2：02-Q3、03-Q4、04-Q2（Agent + RAG + SSE）
3. L3：02-Q5 或 03-Q6（选一道深挖）

### RAG 专项（30 分钟）

- 03 全部 L2 + L3-Q1、L3-Q2

### Agent 框架专项（30 分钟）

- 02 全部 L2 + L3-Q1、L3-Q3

### 智能体架构与实现专项（60–90 分钟）⭐

- [19_agent_architecture_implementation.md](./19_agent_architecture_implementation.md) 全文 L1→L3（30 题）
- [20_agent_mock_interview.md](./20_agent_mock_interview.md) 卷 B + 卷 C + 卷 E
- [21_middleware_chain_deep_dive.md](./21_middleware_chain_deep_dive.md) 8 层 Middleware
- [22_agent_source_walkthrough.md](./22_agent_source_walkthrough.md) W1–W15 精读
- [25_deep_research_orchestrator.md](./25_deep_research_orchestrator.md) Q1–Q13
- 源码跟读：agent_service → workflow_registry → profiled_executor → graph_builder → task_tool

### 安全专项（25 分钟）

- 11 全部 L2 + 05-Q7 + 15-场景6、场景12

### 新人 Onboarding 测验（30 分钟）

- [13_mock_interview.md](./13_mock_interview.md) 卷 D + [14_quick_reference.md](./14_quick_reference.md)

### 深度研究专项（30 分钟）

- [25_deep_research_orchestrator.md](./25_deep_research_orchestrator.md) 全文
- [20 卷 F](./20_agent_mock_interview.md) + 22 W31–W40
- 文档：[`deep_research_module.md`](../../docs/docs_backend/deep_research_module.md)

### 考前 15 分钟

- [18_interview_drill.md](./18_interview_drill.md) 140 题快问快答（或 101–140 Agent/Middleware/深研专章）
- 或 [14_quick_reference.md](./14_quick_reference.md) 速查

### 会话 / 隔离 / 部署专项（25 分钟）

- 16 全部 + 05-Q16–Q18 + 17-Q4/Q8/Q12

---

## 关键源码速查

```
backend/cli.py                              # 命令行入口
backend/api/http_server.py                  # 主 API（8082）
backend/api/guest_http_server.py            # 访客 API（8083）
backend/api/routers/agent_runtime.py        # Agent 运行时 API
backend/config.yaml                         # Agent/模型运行时配置
backend/config/settings.py                  # 环境变量 Pydantic Settings
backend/config/agents/ne.yaml               # NE 智能体 Profile
backend/config/agents/csh.yaml              # CSH 智能体 Profile
backend/config/workflows/registry.yaml      # 工作流注册表
backend/application/agent_service.py        # AgentService 编排入口
backend/application/agent_workflows/registry.py   # WorkflowRegistry
backend/application/agent_executors/profiled_executor.py  # ProfiledAgentExecutor
backend/application/agent_executors/graph_builder.py      # ProfiledGraphBuilder
backend/application/agent_executors/lightweight_qa_agent.py # 轻量 QA
backend/application/tools/tool_assembly_pipeline.py        # 工具装配
backend/application/agents/prompt_builder.py               # Prompt 构建
backend/core/agents/factory.py              # create_agent_graph()
backend/core/agents/middlewares/            # Middleware 链
backend/core/skills/scoped_skills_provider.py  # Skill 加载
backend/rag/retrieval_profile.py
backend/rag/service.py
backend/db/connection.py
backend/api/dependencies/auth.py
```

---

## 题量统计

| 类型 | 约计 |
|------|------|
| 模块 Q&A（01–12） | **165+ 道**（较初版 +15） |
| 专题模块（16–17） | **28 道** |
| **智能体专题（19–25）** | **113 详解题 + 模拟卷题** |
| 快问快答（18） | **140 题**（含 Middleware 121–130、深研 131–140） |
| 模拟面试卷（13） | 5 套 + 追问模板 |
| 速查条目（14） | 33 条 + env/日志 |
| 综合场景（15） | 12 场景 + 2 设计题 |
| ASCII 图表集（00） | **38 张**（含 MCP SSE / Thinking 等） |
| **合计** | **~460+ 题/条目** |

---

## 更新说明

- 文档同步基准：2026-06-10
- **第三版**：01–12 模块答案全面扩写；15 场景题同步加深
- **第四版**：新增 [00_architecture_diagrams.md](./00_architecture_diagrams.md)；01–12 Q1 内嵌 ASCII 框线图
- **第五–八版**：L2/15 场景/docs_backend 各模块逐步补图（见 00 图集 21–33）
- **第九版（2026-06-01）**：`task_worker` / `keywords` / `extraction` / `integrations` 专章；00 图集 34–36；01 Q3–Q11、02 L3、07-Q4、12-Q4 扩写
- **第十版（2026-06-01）**：**04 全模块 Q3–Q14**、**09 Q3/Q4/Q11**、**11 Q3/Q10/Q11** 补图；`asr_module` 链路图；00 图集 **37–38**（MCP SSE / Thinking）
- **第十五版（2026-06-01）**：`docs/` 与 `plans/` 全部图表统一为 **ASCII 框线图**（移除 Mermaid，终端友好）
- **第十二版（2026-06-01）**：新增 **19 智能体架构与实现专题（30 题）**、**20 Agent 专卷**；README 增加 Agent 专项学习路径
- **第十三版（2026-06-01）**：新增 **21–24**；19 增补 Q26–Q30；20 增卷 E
- **第十四版（2026-06-01）**：新增 **25 深度研究 Orchestrator 18 题**；**22 增补 W31–W40**；**18 增补 121–140**（Middleware + 深研快问）；20 增卷 F
- 架构演进参考：`docs/agent_architecture_redesign.md`
