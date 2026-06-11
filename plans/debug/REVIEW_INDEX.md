# Backend 代码审查总索引

> 只读审查汇总（2026-05-30 ~ 继续）。**不修改 backend 源码**。  
> **正式文档**（现行行为说明）→ [docs/README.md](../../docs/README.md) · 文档同步 [CHANGELOG_文档同步_20260531.md](../../docs/CHANGELOG_文档同步_20260531.md)

---

## 如何使用

1. **文档与代码谁为准** → 生产行为以 **代码 + [docs/知识库算法和架构.md](../../docs/知识库算法和架构.md)** 为准；本目录标 **差距/技术债**  
2. **文档漂移** → [backend_custom_docs_vs_code_gaps.md](./backend_custom_docs_vs_code_gaps.md)（含 2026-05-31 已修 doc 列表）  
3. **快速扫风险** → [backend_per_file_index.md](./backend_per_file_index.md) 筛 `high`  
4. **总览架构债** → [backend_code_review.md](./backend_code_review.md)  
5. **排期整改** → [refactoring_roadmap.md](./refactoring_roadmap.md)

---

## 文档清单

### 总览与全量索引

| 文档 | 说明 |
|------|------|
| [backend_code_review.md](./backend_code_review.md) | 架构、P0–P2、双轨 core/deerflow |
| [backend_per_file_index.md](./backend_per_file_index.md) | **487** 个 .py 自动扫描表 |
| [refactoring_roadmap.md](./refactoring_roadmap.md) | Sprint 0–4 路线图 |
| [architecture_gap.md](./architecture_gap.md) | vs agent_architecture_redesign.md |
| [cross_cutting_duplication.md](./cross_cutting_duplication.md) | 重复矩阵 |

### 逐模块文件级

| 文档 | 范围 | 文件数 |
|------|------|--------|
| [per_file_api_core_db_services.md](./per_file_api_core_db_services.md) | api, core, db, config, services, storage, worker… | 130 |
| [per_file_rag_custom.md](./per_file_rag_custom.md) | rag, custom | 103 |
| [per_file_deerflow_scripts_tests.md](./per_file_deerflow_scripts_tests.md) | deerflow, scripts, tests | 226 |

### 深度审查（专题）

| 文档 | 专题 |
|------|------|
| [deep_dive_v2_agent_api.md](./deep_dive_v2_agent_api.md) | agent.py / csh_agent 91% 重复 |
| [deep_dive_lightweight_qa_agent.md](./deep_dive_lightweight_qa_agent.md) | 轻量 QA 1349 行拆解 |
| [deep_dive_stream_handler_sse.md](./deep_dive_stream_handler_sse.md) | StreamHandler vs SSE |
| [deep_dive_rag_service.md](./deep_dive_rag_service.md) | rag/service 3354 行 |
| [deep_dive_ne_agent_and_research_flow.md](./deep_dive_ne_agent_and_research_flow.md) | 全功能 Agent + 深研 |
| [deep_dive_useAgentChat.md](./deep_dive_useAgentChat.md) | 前端状态机/恢复 |
| [deep_dive_multi_kb_explore_tools.md](./deep_dive_multi_kb_explore_tools.md) | multi_kb / explore / tools |
| [deep_dive_conversations_checkpoint.md](./deep_dive_conversations_checkpoint.md) | 会话 + checkpoint |
| [deep_dive_chat_files_index.md](./deep_dive_chat_files_index.md) | 对话文件 + 索引 |
| [audit_frontend_backend_sse.md](./audit_frontend_backend_sse.md) | agent-v2.ts 对齐 |
| [audit_auth_implementation_vs_plans.md](./audit_auth_implementation_vs_plans.md) | 认证规划 vs 实现 |
| [deep_dive_my_files_user_file_service.md](./deep_dive_my_files_user_file_service.md) | 个人文件夹双轨模型 |
| [deep_dive_context_pruning_vs_plan.md](./deep_dive_context_pruning_vs_plan.md) | ContextPruning vs 多轮方案 |
| [deep_dive_kafka_worker_pipeline.md](./deep_dive_kafka_worker_pipeline.md) | Kafka Worker 文档流水线 |
| [sse_golden_samples.md](./sse_golden_samples.md) | SSE 契约测试样本 |
| [deep_dive_follow_up_suggestions.md](./deep_dive_follow_up_suggestions.md) | 追问建议服务 + 并发单例风险 |
| [deep_dive_document_upload_kafka_producer.md](./deep_dive_document_upload_kafka_producer.md) | 文档上传 → Kafka 生产端 |
| [audit_frontend_my_files_api.md](./audit_frontend_my_files_api.md) | 前端个人文件夹 API 对齐 |
| [fixtures/sse/](./fixtures/sse/) | SSE 黄金样本（11 类已齐） |

---

## 全库 P0 问题汇总（合并）

| ID | 领域 | 问题 |
|----|------|------|
| — | 安全 | `rag_retrieve_internal` 无 secret 时仍放行 |
| — | 安全 | `init_db` <dev_api_key> |
| — | 安全 | `auth_sms` 硬编码 SMS_SECRET + 内存验证码 |
| — | 安全 | scripts 明文 Neo4j/OpenAI 密钥 |
| — | 安全 | **get_file_meta 无 user_id 校验（越权读文件）** |
| — | 并发 | **FollowUpSuggestionService 单例 tool_summary 串线（FU-2）** |
| — | 部署 | **batch 文档上传写 tempfile，Worker 多机不可用（UP-2）** |
| — | 代码 | `rag/service.py` 重复 Redis patch 函数 |
| — | 代码 | `rag/standalone_query.py` 空文件 |

---

## 统计摘要

| 指标 | 值 |
|------|-----|
| Python 文件 | 487 |
| 扫描 high | 13 |
| God file (>1000 行) | 15+ |
| 测试文件 | 4 |
| 审查文档数 | 20+ |

---

## 建议下一步审查（未做）

- `api/routers/my_files.py` + `user_file_service.py` 细读  
- `core/agents/middlewares/context_pruning.py` vs plans/多轮工具调用  
- `kafka` + `worker` 入库链路端到端  
- `frontend_csh_ai` 全量 diff（除 agent-v2/useAgentChat 已部分覆盖）  
- 性能 profiling（无运行，仅静态）

---

## 维护说明

- 修复某 issue 后，在对应文档条目下备注 `Fixed in PR #xxx`  
- 重大架构变更后重新跑 `backend_per_file_index.md` 生成脚本（见 backend 根目录审查会话记录）
