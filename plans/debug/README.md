# Debug / 代码审查输出目录

本目录存放 **只读代码审查** 文档，**不修改 backend 源码**。  
与正式维护文档的关系：

| 目录 | 用途 |
|------|------|
| **[docs/](../../docs/README.md)** | 现行架构、模块说明、API 协议（2026-05-31 已与代码对照） |
| **本目录 `plans/debug/`** | 技术债、P0 清单、重复代码、文档漂移审计 |
| **[plans/](../../plans/README.md)** | 历史规划与长期愿景 |

**完整索引** → [REVIEW_INDEX.md](./REVIEW_INDEX.md)

---

## 推荐阅读顺序

1. **负责人 1 页** → [backend_custom_executive_summary.md](./backend_custom_executive_summary.md) 或 [EXECUTIVE_SUMMARY.md](./EXECUTIVE_SUMMARY.md)
2. **文档写了但代码没有** → [backend_custom_docs_vs_code_gaps.md](./backend_custom_docs_vs_code_gaps.md)（文首含 2026-05-31 已修 doc 列表）
3. **认证规划 vs 实现** → [audit_auth_implementation_vs_plans.md](./audit_auth_implementation_vs_plans.md) + [docs/docs_backend/sms_auth_module.md](../../docs/docs_backend/sms_auth_module.md)
4. **SSE 前后端契约** → [audit_frontend_backend_sse.md](./audit_frontend_backend_sse.md) + [docs/docs_backend/sse_events_module.md](../../docs/docs_backend/sse_events_module.md)
5. **按文件扫风险** → [backend_per_file_index.md](./backend_per_file_index.md)（筛 `high` / 行数 >1000）
6. **整改排期** → [refactoring_roadmap.md](./refactoring_roadmap.md) · P0 实施 → [backend_custom_p0_implementation_guide.md](./backend_custom_p0_implementation_guide.md)

---

## 文档索引（分类）

### 总览与索引

| 文档 | 说明 |
|------|------|
| [backend_code_review.md](./backend_code_review.md) | 总体架构、跨模块问题、P0–P2 清单 |
| [backend_per_file_index.md](./backend_per_file_index.md) | **487 个 .py 自动扫描表**（行数、except、print、pickle 等） |
| [refactoring_roadmap.md](./refactoring_roadmap.md) | **可执行整改路线图**（Sprint 0–4） |
| [CLOSURE_AND_REMAINING.md](./CLOSURE_AND_REMAINING.md) | 审查收尾与遗留项 |

### 逐文件（按模块）

| 文档 | 范围 | 文件数 |
|------|------|--------|
| [per_file_api_core_db_services.md](./per_file_api_core_db_services.md) | api / core / db / config / services / storage / … | 130 |
| [per_file_rag_custom.md](./per_file_rag_custom.md) | rag / custom | 103 |
| [per_file_deerflow_scripts_tests.md](./per_file_deerflow_scripts_tests.md) | deerflow / scripts / tests / skills / cli | 226 |

### 深度审查（专题）

| 文档 | 专题 |
|------|------|
| [deep_dive_v2_agent_api.md](./deep_dive_v2_agent_api.md) | agent.py / csh_agent 高重复、SSE |
| [deep_dive_rag_service.md](./deep_dive_rag_service.md) | service.py 体量与拆分 |
| [deep_dive_lightweight_qa_agent.md](./deep_dive_lightweight_qa_agent.md) | lightweight_qa_agent 函数级 |
| [deep_dive_stream_handler_sse.md](./deep_dive_stream_handler_sse.md) | stream_handler ↔ SSE |
| [audit_frontend_backend_sse.md](./audit_frontend_backend_sse.md) | agent-v2.ts ↔ 后端 |
| [deep_dive_useAgentChat.md](./deep_dive_useAgentChat.md) | 前端状态机、后台任务 |
| [deep_dive_ne_agent_and_research_flow.md](./deep_dive_ne_agent_and_research_flow.md) | 深研全流程 |
| [deep_dive_multi_kb_explore_tools.md](./deep_dive_multi_kb_explore_tools.md) | multi_kb / explore / tool_assembly |
| [sse_golden_samples.md](./sse_golden_samples.md) | SSE 黄金样本（契约测试建议） |
| [deep_dive_conversations_checkpoint.md](./deep_dive_conversations_checkpoint.md) | 会话与 checkpoint |
| [deep_dive_chat_files_index.md](./deep_dive_chat_files_index.md) | 对话文件、越权风险 |
| [audit_auth_implementation_vs_plans.md](./audit_auth_implementation_vs_plans.md) | 认证规划 vs 实现 |
| [deep_dive_my_files_user_file_service.md](./deep_dive_my_files_user_file_service.md) | 个人文件夹 + UserFile 双轨 |
| [deep_dive_context_pruning_vs_plan.md](./deep_dive_context_pruning_vs_plan.md) | ContextPruning vs 多轮上下文方案 |
| [deep_dive_kafka_worker_pipeline.md](./deep_dive_kafka_worker_pipeline.md) | Kafka + Worker 文档流水线 |
| [deep_dive_follow_up_suggestions.md](./deep_dive_follow_up_suggestions.md) | 追问建议 Follow-up |
| [deep_dive_document_upload_kafka_producer.md](./deep_dive_document_upload_kafka_producer.md) | 文档上传 Kafka 生产 |
| [audit_frontend_my_files_api.md](./audit_frontend_my_files_api.md) | 前端 my-files 契约 |
| [fixtures/sse/](./fixtures/sse/) | SSE 契约样本（11 类） |
| [cross_cutting_duplication.md](./cross_cutting_duplication.md) | 跨模块重复 |
| [architecture_gap.md](./architecture_gap.md) | vs [agent_architecture_redesign.md](../../docs/agent_architecture_redesign.md) |

### backend/custom 专项

| 文档 | 说明 |
|------|------|
| [backend_custom_code_review.md](./backend_custom_code_review.md) | custom 总览 P0–P3 |
| [backend_custom_docs_vs_code_gaps.md](./backend_custom_docs_vs_code_gaps.md) | **docs_backend 与代码矛盾**（维护 doc 时必读） |
| [backend_custom_sse_and_api_contract_gaps.md](./backend_custom_sse_and_api_contract_gaps.md) | SSE/API 契约 |
| [backend_custom_p0_fix_checklist.md](./backend_custom_p0_fix_checklist.md) | P0 清单 |
| [backend_custom_p0_implementation_guide.md](./backend_custom_p0_implementation_guide.md) | P0 PR 步骤 |
| [backend_custom_github_issues.md](./backend_custom_github_issues.md) | Issue 模板 |
| [backend_custom_issues.csv](./backend_custom_issues.csv) | 可导入 Tracker |
| [backend_custom_executive_summary.md](./backend_custom_executive_summary.md) | 1 页执行摘要 |
| [backend_custom_per_file_review_detailed.md](./backend_custom_per_file_review_detailed.md) | 52 文件逐文件深度 |
| [backend_custom_deep_dive_multi_kb_search_tool.md](./backend_custom_deep_dive_multi_kb_search_tool.md) | multi_kb 单文件 |
| [backend_custom_deep_dive_html_ppt_tool.md](./backend_custom_deep_dive_html_ppt_tool.md) | html_ppt 单文件 |
| [backend_custom_p1_implementation_guide_detailed.md](./backend_custom_p1_implementation_guide_detailed.md) | P1 详细实施 |
| [backend_custom_round7_excel_tools_bundle.md](./backend_custom_round7_excel_tools_bundle.md) | **第七轮** Excel 工具包 |
| [backend_custom_deep_dive_research_draft_and_tool_assembly.md](./backend_custom_deep_dive_research_draft_and_tool_assembly.md) | 第七轮：草案 + tool_assembly |
| [backend_custom_round7_tasks_streaming_misc.md](./backend_custom_round7_tasks_streaming_misc.md) | 第七轮：任务/路由/guest |
| [backend_custom_round8_academic_search_tool.md](./backend_custom_round8_academic_search_tool.md) | **第八轮** 学术检索全文件 |
| [backend_custom_round8_chat_files_and_explore.md](./backend_custom_round8_chat_files_and_explore.md) | 第八轮：对话文件索引 + Explore |
| [backend_custom_round8_ne_vs_csh_agents.md](./backend_custom_round8_ne_vs_csh_agents.md) | 第八轮：NE/CSH Agent 对照与深研编排 |

### services/ 子目录

逐服务 Pass 审查 → [services/README.md](./services/README.md)

---

## 如何使用

1. **修代码前**：查 P0 清单与 [backend_custom_docs_vs_code_gaps.md](./backend_custom_docs_vs_code_gaps.md)，避免与文档同步 PR 冲突。  
2. **修代码后**：更新 [docs/文档代码对照清单.md](../../docs/文档代码对照清单.md) 与对应 `docs_backend/*_module.md`；在本目录差距表勾选 §12。  
3. **只改文档**：以 `backend/` 与 OpenAPI `/docs` 为准；审查稿中的「建议修正」可能已在 2026-05-31 doc PR 完成。

---

## 统计摘要

| 项 | 值 |
|----|-----|
| 扫描基准日 | 2026-05-30 |
| Python 源文件 | **487** |
| 自动扫描 | `high` 13 · `medium` 21 · `low` 227 · `ok` 226 |
| 人工 P0 示例 | 内部 API 鉴权、SMS 密钥硬编码、pickle、scripts 密码 |

---

## 审查方法

- 静态正则 + `wc -l` 全量扫描  
- 子模块人工精读（api、rag、custom、deerflow）  
- 未默认运行 pytest / 覆盖率（可按 [services/pass5/04_test_plan.md](./services/pass5/04_test_plan.md) 补充）

---

*与 [docs/CHANGELOG_文档同步_20260531.md](../../docs/CHANGELOG_文档同步_20260531.md) 联动维护。*
