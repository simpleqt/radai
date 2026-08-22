# 项目文档索引

本目录集中存放「核电子学智能助手 (RadAI)」「成理水文地质智学助手 (CSH-AI)」「基础力学课程智能体 (FMCA)」的设计说明、模块文档与运维指南。

> 项目根目录 [README.md](../README.md) 提供快速入门；**详细架构与模块说明以本文档为准**。

---

## 项目概览

| 组件 | 路径 | 端口 | 说明 |
| :--- | :--- | :--- | :--- |
| 主 API | `backend/api/http_server.py` | **8082** | 全功能 FastAPI（RAG、Agent、知识库、MCP、数据集） |
| 拆分 Agent API | `scripts/run_agent_api.sh` | **8082** | `API_SERVICE_ROLE=agent`，轻量承载对话运行时并委托检索 |
| 拆分 Knowledge API | `scripts/run_knowledge_api.sh` | **8086** | `API_SERVICE_ROLE=knowledge`，本地预热 RAG、检索和索引同步 |
| 访客 API | `backend/api/guest_http_server.py` | **8083** | 精简服务（登录 + 问答 + 会话，独立 checkpoint） |
| RadAI 前端 | `frontend/` | **5173** | 核电子学智能助手（`agent_code=ne`） |
| CSH-AI 前端 | `frontend_csh_ai/` | **5174** | 成理水文地质智学助手（`agent_code=csh` / `groundwater`） |
| FMCA 前端 | `frontend_fmca/` | **5176** | 基础力学课程智能体（`agent_code=fmca`，不绑定知识库） |
| 访客前端 | `guest/` | **8085** | 访客/演示模式 |
| 评测框架 | [`eval/`](../eval/README.md) | — | HotpotQA、Paper KB Ragas、GraphRAG-Bench 官方指标、消融实验 |

**技术栈**：Python 3.12–3.14（当前验收环境 3.14.2）+ FastAPI + LangChain/LangGraph + RAGAnything + LightRAG + SelfIngestionPipeline；Vue 3 + TypeScript + Vite 8 + Tailwind CSS 4；MySQL、Milvus、NebulaGraph、MinIO、Redis、Kafka、SQLite/Redis Checkpointer（主 API 可选，访客 API 固定 SQLite）。依赖基线见 [运行时依赖版本矩阵](./dependency_version_matrix.md)。

```text
┌─────────────┐  POST /api/v2/agent-runtime/query  ┌────────────────────┐
│   前端/UI   │ ─────────────────────────────────► │ agent_runtime.py   │
└─────────────┘  AgentRuntimeQueryRequest           └─────────┬──────────┘
                                                              │
                              AgentService.run_stream()        │
                                        │                     │
                                        ▼                     │
                              WorkflowRegistry.resolve()       │
                                        │                     │
                         ┌──────────────┼──────────────────────────────┐
                         ▼              ▼                              ▼
                   lightweight        full        deep_research / plan_solve / multi_agent_*
                         │              │                              │
                         ▼              ▼                              ▼
                StandardAgent    StandardAgent               CoordinatedAgent
                Workflow         Workflow                    Workflow
                         │              │                              │
                         ▼              ▼                              ▼
              ProfiledAgentExecutor      plan -> evidence subagents -> writer
                         │                              │
                         └──────────────┬───────────────┘
                                        ▼
                         AgentEvent → SSEEncoder → SSE        │
```

### 系统全景

```text
┌─────────────────────────────────────────────────────────────────┐
│                     前端四端 (Vue 3)                             │
│  RadAI :5173 │ CSH-AI :5174 │ FMCA :5176 │ Guest :8085         │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP / SSE
         ┌─────────────────┴─────────────────┐
         ▼                                   ▼
┌────────────────────────────┐    ┌────────────────────────────┐
│ 主 API :8082               │    │ 访客 API :8083              │
│ v1 RAG/KB · v2 Agent/Explore│    │ 独立 checkpoint             │
└─────────────┬──────────────┘    └─────────────┬──────────────┘
              │                                  │
┌─────────────▼──────────────────────────────────▼──────────────┐
│    AgentService → WorkflowRegistry → Workflow                  │
│ StandardAgentWorkflow / CoordinatedAgentWorkflow               │
│ Profile: ne / csh / groundwater / fmca                         │
└─────────────┬────────────────────────────────────────────────┘
              │
┌─────────────▼──────┬──────────┬──────────┬──────────┬─────────┐
│ MySQL │ Milvus │ NebulaGraph │ MinIO │ Redis │ Kafka │ SQLite/Redis CP│
└───────┴────────┴─────────────┴───────┴───────┴───────┴─────────┘
```

HTTP 边界说明：`lightweight`、`full` 和 `multi_agent_*` 直接通过 `/query` 返回完整 SSE；`deep_research` 与 `plan_solve` 由可重连后台任务承载，`POST /query` 返回启动确认，之后通过 `/{agent_code}/task/{task_id}/status|stream|cancel` 查看、订阅和取消。

面试详解 → [plans/qa/02_agent_system.md](../plans/qa/02_agent_system.md)

---

## 快速运行

```bash
# 安装依赖
make install

# 初始化数据库
make init-db

# 启动后端
make run-api              # 主 API → http://localhost:8082
make run-guest-api        # 访客 API → http://localhost:8083
make run-worker           # Kafka 任务 Worker
make run-ingest-worker    # 文档入库 Worker

# 拆分模式
scripts/run_knowledge_api.sh   # Knowledge API → http://127.0.0.1:8086
scripts/run_agent_api.sh       # Agent API → http://localhost:8082

# 启动前端
bash run_frontends.sh     # RadAI:5173 + CSH-AI:5174 + FMCA:5176
cd guest && npm run dev   # 访客前端:8085

# 测试
make test
```

环境变量配置见 `backend/.env` 与 `backend/config/settings_sections.py`；`backend/config.yaml` 仍作为旧版运行时/模型工厂 fallback。Checkpointer 由 `AGENT_CHECKPOINTER_BACKEND` 选择（默认 `sqlite`，共享部署可用 `redis`）；运行树镜像、资源租约和 Trace 后端分别由 `AGENT_RUNTIME_STORE_*`、`AGENT_RESOURCE_LEASE_*`、`AGENT_TRACE_*` 控制。`docker-compose.yml` 编排 API、Worker、Ingest Worker、MySQL、Redis、Kafka、MinIO、Prometheus、Grafana；Milvus、NebulaGraph 需接入外部服务或另行编排。Compose 变量来自根目录 `.env`（模板为 `.env.example`），应用变量来自 `APP_ENV_FILE`（默认 `backend/.env`）。

---

## 模块文档

### 架构与运维

| 文档 | 内容 |
| :--- | :--- |
| [项目介绍与系统架构.md](./项目介绍与系统架构.md) | 当前项目介绍、智能体整体框架、前后端实现框架与功能全景图 |
| [agent_development_guide.md](./agent_development_guide.md) | 智能体平台开发指南：Profile、Workflow、Policy、RuntimeContext、AgentEvent 与动态工具装配 |
| [coordinated_agent_workflows.md](./coordinated_agent_workflows.md) | Plan/Solve、多智能体并行与交替工作流扩展规范 |
| [codex_like_agent_runtime_optimization_plan.md](./codex_like_agent_runtime_optimization_plan.md) | Codex-like Agent Runtime：子智能体 manifest、Handoff V2、Trace/Replay、Context Artifact、工具 guardrail |
| [dynamic_context_management_design.md](./dynamic_context_management_design.md) | 五层上下文模型、JIT 动态组装、多智能体交接包设计 |
| [日志Kafka调用说明.md](./日志Kafka调用说明.md) | Kafka Topic 与日志消费 |
| [日志Kafka管理方案.md](./日志Kafka管理方案.md) | 日志 Kafka 方案设计 |
| [MCP服务访问指南.md](./MCP服务访问指南.md) | MCP SSE 接入说明 |
| [split_agent_rag_services.md](./split_agent_rag_services.md) | Agent API 与 Knowledge API 拆分部署说明 |

### 后端核心

| 文档 | 内容 |
| :--- | :--- |
| [docs_backend/README.md](./docs_backend/README.md) | 后端总览、架构、路由、运行方式 |
| [docs_backend/agent_engine_module.md](./docs_backend/agent_engine_module.md) | LangGraph 引擎、中间件链、ContextPruning、Workflow/Coordination 路由 |
| [docs_backend/rag_module.md](./docs_backend/rag_module.md) | RAG 引擎、DirectRetriever、**双轨入库**（legacy / self_ingestion）、Profile |
| [docs_backend/csh_ai_module.md](./docs_backend/csh_ai_module.md) | CSH-AI 智学助手、深度研究工作流 |
| [docs_backend/guest_module.md](./docs_backend/guest_module.md) | 访客 8083、IndexedDB + guest checkpoint |
| [docs_backend/user_isolation_module.md](./docs_backend/user_isolation_module.md) | ScopedStorage、ScopedSkillsProvider |
| [docs_backend/infrastructure_module.md](./docs_backend/infrastructure_module.md) | FastAPI 生命周期、DB、MinIO、Kafka、**双轨 Ingest Worker** |

### 后端扩展能力

| 文档 | 内容 |
| :--- | :--- |
| [docs_backend/agent_kb_binding_module.md](./docs_backend/agent_kb_binding_module.md) | 智能体-知识库动态绑定、管理 API |
| [docs_backend/model_management_module.md](./docs_backend/model_management_module.md) | 模型持久化管理与 Redis Pub/Sub 缓存同步 |
| [docs_backend/excel_data_analysis_tool_design.md](./docs_backend/excel_data_analysis_tool_design.md) | Excel 实验数据分析、DuckDB SQL、科研图表 |
| [groundwater_project_analysis.md](./groundwater_project_analysis.md) | 地下水项目分析助手：项目包授权、bootstrap 合同、DuckDB 查询、KML 汇总与质量/预测工具 |
| [提示词模板构建教程.md](./提示词模板构建教程.md) | 管理后台统一提示词管理与自研入库抽取 Schema YAML 写法 |
| [custom_learning_closed_loop_design.md](./custom_learning_closed_loop_design.md) | 自定义学习闭环：计划、节点、练习、风格映射与 Agent 团队 |

### 前端

| 文档 | 内容 |
| :--- | :--- |
| [docs_frontend/README.md](./docs_frontend/README.md) | 四端前端总览、路由、技术栈（RadAI / CSH-AI / FMCA / Guest） |
| [docs_frontend/ne_full_module.md](./docs_frontend/ne_full_module.md) | 专家模式（SSE、深度研究卡片、后台 task 流） |
| [docs_frontend/chat_module.md](./docs_frontend/chat_module.md) | V1 标准对话模块（历史参考） |
| [docs_frontend/admin_module.md](./docs_frontend/admin_module.md) | 内嵌管理后台（KB CRUD、入库后端选择、上传入库、关键词初始化） |
| [docs_frontend/infrastructure_module.md](./docs_frontend/infrastructure_module.md) | 路由守卫、请求封装、上传（含 custom-learning / learning-style 路由） |
| [docs_frontend/user_settings_requirements.md](./docs_frontend/user_settings_requirements.md) | 用户设置、主题偏好与个人资料需求 |
| [docs_frontend/mobile_desktop_guest_frontend_implementation_checklist.md](./docs_frontend/mobile_desktop_guest_frontend_implementation_checklist.md) | 移动端/桌面端/访客实施清单 |

### 算法与评测

| 文档 | 内容 |
| :--- | :--- |
| [知识库算法和架构.md](./知识库算法和架构.md) | DirectRetriever、Profile、hybrid 提词、GraphRAG 结论；**含 legacy / self_ingestion 双轨入库与检索**（算法主文档） |
| [入库实现方案.md](./入库实现方案.md) | 自研多模态 RAG 入库（`SelfIngestionPipeline`）完整设计与双轨兼容策略 |
| [self_ingestion_quickstart.md](./self_ingestion_quickstart.md) | 自研入库运维上手：开关、建库、Worker、验证与 P0 清单 |
| [提示词模板构建教程.md](./提示词模板构建教程.md) | 管理后台统一提示词管理使用说明；入库抽取 Schema 的 YAML 写法、字段解释、地下水管理完整示例与验证清单 |
| [入库实现方案审查.md](./入库实现方案审查.md) | 自研入库落地审计：Phase 1–5 进度、风险与上线检查清单（2026-06-29） |
| [eval_framework.md](./eval_framework.md) | HotpotQA、Paper KB Ragas、GraphRAG-Bench 评测框架总览 |
| [../plans/qa/00_architecture_diagrams.md](../plans/qa/00_architecture_diagrams.md) | **38 张** ASCII 架构图集（全栈拓扑/SSE/RAG/Worker/安全/部署/会话/关键词） |
| [../eval/graphrag_bench/README.md](../eval/graphrag_bench/README.md) | GraphRAG-Bench 官方 CR/ER 评测命令与 preset |
| [knowledge_base_recall_evaluation_plan.md](./knowledge_base_recall_evaluation_plan.md) | 召回评测指标、消融方案与已落地脚本索引 |
| [chunk_recall_optimization.md](./chunk_recall_optimization.md) | LightRAG 截断补丁 / Milvus fallback（补充 Path-C 说明） |

---

## 功能设计文档

| 文档 | 状态 | 说明 |
| :--- | :--- | :--- |
| [follow_up_suggestions_design.md](./follow_up_suggestions_design.md) | ✅ 已实现 | 回答后追问建议（SSE `follow_up_suggestions`） |
| [learning_style_assessment_design.md](./learning_style_assessment_design.md) | ✅ 已实现 | 四维学习偏好测评；`apply_to_ai` 已接入自定义学习 `style_snapshot`（主 Agent 问答尚未注入） |
| [html_ppt_skill_integration_plan.md](./html_ppt_skill_integration_plan.md) | ✅ 已实现 | html-ppt 技能与 `generate_html_ppt` 工具 |
| [chat_file_upload_design.md](./chat_file_upload_design.md) | ✅ 核心链路已实现 | `chat-files` API、BM25 索引、`chat_file_search`；当前按职责拆分为 `chat-files`、`my-files`、`files` 三类路由 |
| [md_ingestion_design.md](./md_ingestion_design.md) | ✅ 已实现 | Markdown 入库（`.md` 上传 + MDIngestor 直入库） |
| [md_ingestion_api.md](./md_ingestion_api.md) | ✅ 已实现 | Markdown 入库 API 调用说明 |
| [skill_dynamic_tool_loading_plan.md](./skill_dynamic_tool_loading_plan.md) | ✅ 已实现 | Skills 动态加载（ToolAssemblyPipeline + ScopedSkillsProvider） |
| [skills_upload_authorization_binding_design.md](./skills_upload_authorization_binding_design.md) | ✅ 核心链路已实现 | Skills ZIP 上传、版本发布、用户/角色授权与智能体绑定；跨进程缓存同步仍为增强项 |
| [mysql-model-management-plan.md](./mysql-model-management-plan.md) | ✅ 已实现 | 模型管理 MySQL 化（见 model_management_module） |
| [groundwater_project_analysis.md](./groundwater_project_analysis.md) | ✅ 已实现 | 地下水项目分析：项目实例包、管理 API、bootstrap + DuckDB 查询、KML 汇总与质量/预测工具 |
| [custom_learning_closed_loop_design.md](./custom_learning_closed_loop_design.md) | ✅ 第一期已实现 | 自定义学习闭环：计划生成、节点学习、探索式资源回写、学习风格映射；含 `CustomLearningAgentTeam` 多角色编排 |

---

## 实施方案（含历史记录）

| 文档 | 状态 | 说明 |
| :--- | :--- | :--- |
| [用户级隔离实施方案.md](./用户级隔离实施方案.md) | ⚠️ 大部分已实现 | 沙箱路径隔离、工件路由 `artifacts.py` 已落地；删除/重建会话仍有遗留问题 |
| [原生环境_非沙箱_用户隔离实施方案.md](./原生环境_非沙箱_用户隔离实施方案.md) | ✅ 已完成 | MinIO ScopedStorage 隔离 |
| [访客独立API端点实施方案.md](./访客独立API端点实施方案.md) | ✅ 已实现 | 含独立 `guest_http_server` |
| [anonyrag_import_eval_plan.md](./anonyrag_import_eval_plan.md) | 📋 规划 | AnonyRAG 导入评测（代码未落地） |

---

## 待办与路线图

**自研入库（self_ingestion）**：代码与文档已对齐；MySQL Schema 已于 2026-07-03 复验 PASSED；P0 端到端压测与三组业务回归仍待完成，见 [入库实现方案审查.md §6](./入库实现方案审查.md) 与 [self_ingestion_quickstart.md §9](./self_ingestion_quickstart.md)。

**本地 Compose 边界**：`docker-compose.yml` 已包含 Redis，并已提供 `prometheus/prometheus.yml` 与 `grafana/provisioning/`；不包含 Milvus、NebulaGraph。启动前需根据根目录 `.env.example` 配置 Compose 密钥，并在 `APP_ENV_FILE` 指向的应用环境文件中配置外部 Milvus、NebulaGraph 等服务。

[plans/ 内部规划文档](../plans/) — 历史任务拆解与算法演进记录（无统一索引；**认证愿景**见 `plans/认证方案.md`，**现行**见后端 auth 路由）。

---

## 公开 Skills（8 个）

位于 `backend/skills/public/`：`nuclear-electronics`、`deep-research`、`html-ppt`、`academic-search`、`excel-experiment-analysis`、`csh-hydrogeology`、`general-table-analysis`、`groundwater-project-analysis`。

---

© 2026 核电子学智能助手项目组
