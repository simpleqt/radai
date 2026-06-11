# 项目文档索引

本目录集中存放「核电子学智能助手 (RadAI)」与「成理水文地质智学助手 (CSH-AI)」的设计说明、模块文档与运维指南。

> 项目根目录 [README.md](../README.md) 提供快速入门；**详细架构与模块说明以本文档为准**。

---

## 项目概览

| 组件 | 路径 | 端口 | 说明 |
| :--- | :--- | :--- | :--- |
| 主 API | `backend/api/http_server.py` | **8082** | 全功能 FastAPI（RAG、Agent、知识库、MCP、数据集） |
| 访客 API | `backend/api/guest_http_server.py` | **8083** | 精简服务（登录 + 问答 + 会话，独立 checkpoint） |
| RadAI 前端 | `frontend/` | **5173** | 核电子学智能助手 |
| CSH-AI 前端 | `frontend_csh_ai/` | **5174** | 成理水文地质智学助手 |
| 访客前端 | `guest/` | **8085** | 访客/演示模式 |
| 评测框架 | [`eval/`](../eval/README.md) | — | HotpotQA、Paper KB Ragas、GraphRAG-Bench 官方指标、消融实验 |

**技术栈**：Python 3.14 + FastAPI + LangGraph + LightRAG；Vue 3 + TypeScript + Vite 8 + Tailwind CSS 4；MySQL、Milvus、NebulaGraph、MinIO、Redis、Kafka。

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
│                     前端三端 (Vue 3)                             │
│  RadAI :5173  │  CSH-AI :5174  │  Guest :8085                   │
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
└─────────────┬────────────────────────────────────────────────┘
              │
┌─────────────▼──────┬──────────┬──────────┬──────────┬─────────┐
│ MySQL │ Milvus │ NebulaGraph │ MinIO │ Redis │ Kafka │ SQLite CP│
└───────┴────────┴─────────────┴───────┴───────┴───────┴─────────┘
```

面试详解 → [plans/qa/02_agent_system.md](../plans/qa/02_agent_system.md)

---

## 快速运行

```bash
# 安装依赖
make install

# 初始化数据库
make init-db

# 启动后端
make run-api              # 主 API → http://<api_host>:<api_port>
make run-guest-api        # 访客 API → http://<guest_api_host>:<guest_port>
make run-worker           # Kafka 任务 Worker
make run-ingest-worker    # 文档入库 Worker

# 启动前端
bash run_frontends.sh     # RadAI:5173 + CSH-AI:5174
cd guest && npm run dev   # 访客前端:8085

# 测试
make test
```

环境变量配置见 `backend/.env`；后端运行时与模型配置见 `backend/config.yaml`。

---

## 模块文档

### 架构与运维

| 文档 | 内容 |
| :--- | :--- |
| [项目介绍与系统架构.md](./项目介绍与系统架构.md) | 当前项目介绍、智能体整体框架、前后端实现框架与功能全景图 |
| [agent_platform_standardization_plan.md](./agent_platform_standardization_plan.md) | 智能体平台规范化建设方案：Profile、Workflow、Policy、RuntimeContext、AgentEvent |
| [coordinated_agent_workflows.md](./coordinated_agent_workflows.md) | Plan/Solve、多智能体并行与交替工作流扩展规范 |
| [日志Kafka调用说明.md](./日志Kafka调用说明.md) | Kafka Topic 与日志消费 |
| [日志Kafka管理方案.md](./日志Kafka管理方案.md) | 日志 Kafka 方案设计 |
| [MCP服务访问指南.md](./MCP服务访问指南.md) | MCP SSE 接入说明 |

### 后端核心

| 文档 | 内容 |
| :--- | :--- |
| [docs_backend/README.md](./docs_backend/README.md) | 后端总览、架构、路由、运行方式 |
| [docs_backend/agent_engine_module.md](./docs_backend/agent_engine_module.md) | LangGraph 引擎、中间件链、ContextPruning、Workflow/Coordination 路由 |
| [docs_backend/rag_module.md](./docs_backend/rag_module.md) | RAG 引擎、DirectRetriever、Profile（生产 domain→MMR_ONLY） |
| [docs_backend/csh_ai_module.md](./docs_backend/csh_ai_module.md) | CSH-AI 智学助手、深度研究工作流 |
| [docs_backend/guest_module.md](./docs_backend/guest_module.md) | 访客 8083、IndexedDB + guest checkpoint |
| [docs_backend/user_isolation_module.md](./docs_backend/user_isolation_module.md) | ScopedStorage、ScopedSkillsProvider |
| [docs_backend/infrastructure_module.md](./docs_backend/infrastructure_module.md) | FastAPI 生命周期、DB、MinIO、Kafka |

### 后端扩展能力

| 文档 | 内容 |
| :--- | :--- |
| [docs_backend/agent_kb_binding_module.md](./docs_backend/agent_kb_binding_module.md) | 智能体-知识库动态绑定、管理 API |
| [docs_backend/model_management_module.md](./docs_backend/model_management_module.md) | 模型持久化管理与 Redis Pub/Sub 缓存同步 |
| [docs_backend/excel_data_analysis_tool_design.md](./docs_backend/excel_data_analysis_tool_design.md) | Excel 实验数据分析、DuckDB SQL、科研图表 |

### 前端

| 文档 | 内容 |
| :--- | :--- |
| [docs_frontend/README.md](./docs_frontend/README.md) | 三端前端总览、路由、技术栈 |
| [docs_frontend/ne_full_module.md](./docs_frontend/ne_full_module.md) | 专家模式（SSE、深度研究卡片、后台 task 流） |
| [docs_frontend/chat_module.md](./docs_frontend/chat_module.md) | V1 标准对话模块（历史参考） |
| [docs_frontend/admin_module.md](./docs_frontend/admin_module.md) | 内嵌管理后台（KB CRUD、上传入库、关键词初始化） |
| [docs_frontend/infrastructure_module.md](./docs_frontend/infrastructure_module.md) | 路由守卫、请求封装、上传 |
| [docs_frontend/mobile_desktop_guest_frontend_implementation_checklist.md](./docs_frontend/mobile_desktop_guest_frontend_implementation_checklist.md) | 移动端/桌面端/访客实施清单 |

### 算法与评测

| 文档 | 内容 |
| :--- | :--- |
| [知识库算法和架构.md](./知识库算法和架构.md) | DirectRetriever、Profile、hybrid 提词、GraphRAG 结论（算法主文档，2026-05-30） |
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
| [html_ppt_skill_integration_plan.md](./html_ppt_skill_integration_plan.md) | ✅ 已实现 | html-ppt 技能与 `generate_html_ppt` 工具 |
| [chat_file_upload_design.md](./chat_file_upload_design.md) | ⚠️ 大部分实现 | `chat-files` API、BM25 索引、`chat_file_search`；单路由合并仍待做 |
| [md_ingestion_design.md](./md_ingestion_design.md) | ✅ 已实现 | Markdown 入库（`.md` 上传 + MDIngestor 直入库） |
| [md_ingestion_api.md](./md_ingestion_api.md) | ✅ 已实现 | Markdown 入库 API 调用说明 |
| [skill_dynamic_tool_loading_plan.md](./skill_dynamic_tool_loading_plan.md) | ⚠️ 大部分已实现 | Skills 动态加载（ToolAssemblyPipeline + ScopedSkillsProvider） |
| [mysql-model-management-plan.md](./mysql-model-management-plan.md) | ✅ 已实现 | 模型管理 MySQL 化（见 model_management_module） |

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

[下一步开发需求.md](./下一步开发需求.md) — 记录已落地能力与剩余优先级。

[plans/ 内部规划文档](../plans/README.md) — 历史任务拆解、部署手册、算法演进全记录（**认证愿景**见 `plans/认证方案.md`，**现行**见后端 auth 路由）。

[plans/debug/](../plans/debug/README.md) — **只读代码审查**输出（P0 清单、SSE 契约审计、文档 vs 代码差距）。

---

## 公开 Skills（7 个）

位于 `backend/skills/public/`：`nuclear-electronics`、`deep-research`、`html-ppt`、`academic-search`、`excel-experiment-analysis`、`csh-hydrogeology`、`general-table-analysis`。

---

© 2026 核电子学智能助手项目组
