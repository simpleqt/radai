# 内部规划文档索引 (plans/)

本目录存放**历史规划、任务拆解、部署运维与深度设计草案**。与面向读者的正式文档 [`docs/`](../docs/README.md) 互补：

| 目录 | 定位 |
| :--- | :--- |
| **`docs/`** | 当前架构说明、模块文档、已落地能力的维护文档 |
| **`docs/文档代码对照清单.md`** | 代码入口与主文档对照、维护检查表（改 RAG/Agent 时优先看） |
| **`plans/`** | 迭代规划、任务拆解、部署手册、算法演进全记录 |

> 阅读代码或排查线上问题时，**优先看 `docs/`**；需要了解「为什么这样设计」或历史决策时，再查本目录。

---

## 文档状态图例

| 标记 | 含义 |
| :--- | :--- |
| ✅ 已实现 | 核心能力已落地，文档作归档参考 |
| ⚠️ 部分实现 | 主干已完成，细节或运维项待补 |
| 📋 规划中 | 尚未开发或仅设计草案 |
| 📦 历史归档 | 任务已完成，文档保留过程记录 |
| 🔧 运维手册 | 部署/更新操作说明，需定期核对环境 |

---

## 文档同步说明（2026-05-31）

- **[认证方案.md](./认证方案.md)**：文首已加「规划 vs 代码」对照表；**现行认证**以 [docs/docs_backend/auth_module.md](../docs/docs_backend/auth_module.md)、[sms_auth_module.md](../docs/docs_backend/sms_auth_module.md) 为准（短信验证码为内存存储，非本文 Redis 设计）。

## 文档同步说明（2026-05-30）

以下文档已与代码及 `docs/知识库算法和架构.md` 对齐（含 **API/入库** 扫尾 2026-05-30）：**RAG 主文档**、**关键词落地方案**、**无 LightRAG 精细化检索**、**知识库优化方案**（路线图标注）、**eval_framework**、**api_quick_reference**、**architecture_overview**、**deployment_runbook**、**explore_learning**（及 plans 探索式学习方案状态）、**rag_module / agent_engine / csh_ai / explore** 模块文档、**ne_full / 前端 README**、**多轮工具上下文**（部分实现标注）、**deerflow 迁移**（归档注记）。历史方案中仍写 `PPR_MMR` 为生产默认处，以主文档 **MMR_ONLY** 为准。

---

## 一、当前有效文档（优先查阅）

### 运维与部署

| 文档 | 状态 | 说明 |
| :--- | :--- | :--- |
| [guest_frontend_部署与更新说明.md](./guest_frontend_部署与更新说明.md) | 🔧 运维 | <your_domain> 访客前端 + 8083 后端部署 |
| [DEPLOYMENT.md](./DEPLOYMENT.md) | 🔧 运维 | 基础设施清单；**已修正 Neo4j→NebulaGraph**；密钥勿抄文档明文 |
| [task_06_deployment_config.md](./task_06_deployment_config.md) | 📦 归档 | 多知识库任务链部署草案 → 见 deployment_runbook |

### 算法与 RAG（详细版）

| 文档 | 状态 | 说明 |
| :--- | :--- | :--- |
| [知识库算法和架构.md](./知识库算法和架构.md) | ✅ 维护中 | **完整演进记录**（~1500 行，含 GraphRAG full-ablation、关键词专项） |
| [docs/知识库算法和架构.md](../docs/知识库算法和架构.md) | ✅ 维护中 | **精简现行版**（生产 Profile、评测命令、§11.5–11.6 最新结论） |
| [eval/graphrag_bench/README.md](../eval/graphrag_bench/README.md) | ✅ 维护中 | GraphRAG-Bench 命令与 preset 速查 |
| [无LightRAG精细化检索方案.md](./无LightRAG精细化检索方案.md) | ✅ 已实现 | DirectRetriever 四路召回，见 `backend/rag/retriever/` |
| [知识库优化方案.md](./知识库优化方案.md) | 📋/部分 | 优化方向记录 |
| [kg2rag_deep_analysis.md](./kg2rag_deep_analysis.md) | 📦 参考 | KG2RAG 论文分析 |
| [chunk_recall_optimization.md](../docs/chunk_recall_optimization.md) | ✅ 已实现 | LightRAG 截断补丁；生产主路径见 docs 算法主文档 |
| [knowledge_base_recall_evaluation_plan.md](../docs/knowledge_base_recall_evaluation_plan.md) | ⚠️ 部分 | 指标/方案草案 + §2.4 已落地评测索引 |

### 代码审查（只读）

| 文档 | 状态 | 说明 |
| :--- | :--- | :--- |
| [debug/README.md](./debug/README.md) | 🔧 维护中 | P0 清单、SSE 审计、文档 vs 代码差距；**不修代码** |
| [debug/backend_custom_docs_vs_code_gaps.md](./debug/backend_custom_docs_vs_code_gaps.md) | 🔧 | 与 `docs/docs_backend` 对照；2026-05-31 已修 doc 见文首表 |

### Agent 与前端

| 文档 | 状态 | 说明 |
| :--- | :--- | :--- |
| [探索式学习功能方案.md](./探索式学习功能方案.md) | ✅ 已实现 | 一期已上线；现行见 [docs/explore_learning.md](../docs/explore_learning.md) |
| [多轮工具调用上下文管理重构方案.md](./多轮工具调用上下文管理重构方案.md) | ⚠️ 部分实现 | `ContextPruningMiddleware` 已接入；统一归档方案待落地 |
| [子智能体工作流前端展示完整方案.md](./子智能体工作流前端展示完整方案.md) | ✅ 已实现 | `SubagentWorkflowSwitcher` + `SubagentWorkflowTimeline` |
| [deerflow_agent_migration.md](./deerflow_agent_migration.md) | 📦 归档 | V2 Agent 迁移已完成 |
| [个人文件夹文件管理与上传复用设计方案.md](./个人文件夹文件管理与上传复用设计方案.md) | ⚠️ 部分实现 | `my_files` API + `MyFilesView` + `MyFilesPickerDialog` |
| [s-dag.md](./s-dag.md) | 📋 规划 | 跨库 S-DAG 编排（未独立落地；现行见 docs 算法主文档） |

### 认证与平台

| 文档 | 状态 | 说明 |
| :--- | :--- | :--- |
| [认证方案.md](./认证方案.md) | 📋 长期规划 | 统一认证网关愿景；**现行**见 [auth_module](../docs/docs_backend/auth_module.md)、[sms_auth_module](../docs/docs_backend/sms_auth_module.md)（2026-05-31 文首对照表） |
| [项目需求方案.md](./项目需求方案.md) | 📋 产品愿景 | 智能体平台总体需求（非当前实现清单） |

---

## 二、多知识库改造（已完成，归档）

2026-03 多知识库架构升级任务链，**已全部落地**（NebulaGraph、KBManager、多库 API 等）。

| 文档 | 说明 |
| :--- | :--- |
| [多知识库实现方案.md](./多知识库实现方案.md) | 总体方案与 NebulaGraph 选型 |
| [task_01_database_design.md](./task_01_database_design.md) | MySQL 知识库表设计 |
| [task_02_nebula_storage.md](./task_02_nebula_storage.md) | NebulaGraph 存储适配 |
| [task_03_knowledge_base_manager.md](./task_03_knowledge_base_manager.md) | 知识库管理器 |
| [task_04_rag_service_modification.md](./task_04_rag_service_modification.md) | RAG Service 多库改造 |
| [task_05_api_routes.md](./task_05_api_routes.md) | 知识库 API |
| [task_06_deployment_config.md](./task_06_deployment_config.md) | 📦 部署配置（归档；含历史密码示例，以 `.env` 为准） |
| [task_06_keyword_multi_kb_design.md](./task_06_keyword_multi_kb_design.md) | 多库关键词索引 |
| [task_07_content_filter_multi_kb_design.md](./task_07_content_filter_multi_kb_design.md) | 多库内容过滤 |
| [multi_kb_keyword_index_update.md](./multi_kb_keyword_index_update.md) | 关键词索引更新（归档；Milvus 命名已注记） |
| [md_ingestion_file_type_routing.md](./md_ingestion_file_type_routing.md) | ✅ MD 直入库路由 |
| [kg2rag_deep_analysis.md](./kg2rag_deep_analysis.md) | 📦 论文对照（非生产配置） |
| [knowledge_base_management_integrated.md](./knowledge_base_management_integrated.md) | 📦 历史 API 整合草案 |
| [s-dag.md](./s-dag.md) | 📋 跨库推理编排规划 |
| [关键词提取落地方案.md](./关键词提取落地方案.md) | 关键词提取 |
| [knowledge_base_architecture.md](./knowledge_base_architecture.md) | 早期 KB 架构 |
| [知识库管理接口说明.md](./知识库管理接口说明.md) | 📦 已校正；现行见 `docs/api_quick_reference.md` |

实现代码参考：`backend/rag/knowledge_base/`、`backend/rag/storages/nebula_storage.py`、`backend/api/routers/knowledge_base*.py`。

任务文档 `task_01`～`task_07` 文首已加注「归档」；检索现行行为以 [docs/知识库算法和架构.md](../docs/知识库算法和架构.md) 为准，勿将任务稿中的 PPR 默认配置当作生产设置。

---

## 三、API 与入库

| 文档 | 状态 | 说明 |
| :--- | :--- | :--- |
| [docs/api_quick_reference.md](../docs/api_quick_reference.md) | ✅ 维护中 | **REST 速查（优先）** |
| [知识库管理接口说明.md](./知识库管理接口说明.md) | 📦 已校正 | RAG/KB 接口；Neo4j→Nebula；标注未实现路由 |
| [document_ingestion_api.md](./document_ingestion_api.md) | 🔧 参考 | `/api/v1/ingestion/*`；已去明文 API Key |
| [API.md](./API.md) | 📦 历史 | 文档处理 Worker 路由 |
| [api_reference.md](./api_reference.md) | 📦 历史 | 任务五 KB API 草案 |
| [md_ingestion_file_type_routing.md](./md_ingestion_file_type_routing.md) | ✅/参考 | `.md` 路由规则 |

正式 MD 入库设计 → [`docs/md_ingestion_design.md`](../docs/md_ingestion_design.md)、[`docs/md_ingestion_api.md`](../docs/md_ingestion_api.md)。

---

## 四、其他资源

| 文件 | 说明 |
| :--- | :--- |
| [AGENTS.md](./AGENTS.md) | Agent 开发说明（可能与仓库根规则重复） |
| [AI中枢开放平台架构图.html](./AI中枢开放平台架构图.html) | 产品架构图（HTML 内已注：可能与现行架构不一致） |
| [backend架构图.html](./backend架构图.html) | 后端架构图（同上；图库以 NebulaGraph 为准） |
| [论文.md](./论文.md) / [2502.06864v1.pdf](./2502.06864v1.pdf) | 论文笔记与 PDF |

---

## 五、与 `docs/` 的重复关系

| 主题 | `plans/` | `docs/`（现行摘要） |
| :--- | :--- | :--- |
| 知识库检索算法 | 完整演进 + 消融历史 | 生产配置 + 评测命令 + 最新结论 |
| 访客 API | 部署运维细节 | [guest_module.md](../docs/docs_backend/guest_module.md) |
| 用户隔离 | 任务拆解过程 | [用户级隔离实施方案.md](../docs/用户级隔离实施方案.md) |
| 下一步工作 | — | [下一步开发需求.md](../docs/下一步开发需求.md) |

**维护建议**：算法结论变更时，先更新 `plans/知识库算法和架构.md` 全记录，再同步精简至 `docs/知识库算法和架构.md`；GraphRAG 评测 preset/命令变更时同步 `eval/graphrag_bench/README.md`。

---

© 2026 核电子学智能助手项目组
