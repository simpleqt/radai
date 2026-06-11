# backend/services 逐文件审查索引

| 文件 | 审查文档 | 行数 | 严重度最高 |
|------|----------|------|------------|
| `__init__.py` | [01_init_py.md](./01_init_py.md) | 12 | P2 |
| `chat_file_index.py` | [02_chat_file_index_py.md](./02_chat_file_index_py.md) | 520 | P0 |
| `chat_file_parser.py` | [03_chat_file_parser_py.md](./03_chat_file_parser_py.md) | 400 | P1 |
| `user_file_service.py` | [04_user_file_service_py.md](./04_user_file_service_py.md) | 675 | P1 |
| `startup_task_processor.py` | [05_startup_task_processor_py.md](./05_startup_task_processor_py.md) | 261 | P0 |
| `directory_service.py` | [06_directory_service_py.md](./06_directory_service_py.md) | 573 | P1 |

**总览报告**: [backend_services_code_review.md](./backend_services_code_review.md)

**审查原则**: 仅文档输出，不修改 `backend/services` 源码。

## 第三遍（调用链 / 跨模块 / 架构）

聚焦端到端数据流、与路由/worker 冲突、运维清单：

| 文件 | 第三遍报告 |
|------|------------|
| `__init__.py` | [pass3/01_init_py_pass3.md](./pass3/01_init_py_pass3.md) |
| `chat_file_index.py` | [pass3/02_chat_file_index_pass3.md](./pass3/02_chat_file_index_pass3.md) |
| `chat_file_parser.py` | [pass3/03_chat_file_parser_pass3.md](./pass3/03_chat_file_parser_pass3.md) |
| `user_file_service.py` | [pass3/04_user_file_service_pass3.md](./pass3/04_user_file_service_pass3.md) |
| `startup_task_processor.py` | [pass3/05_startup_task_processor_pass3.md](./pass3/05_startup_task_processor_pass3.md) |
| `directory_service.py` | [pass3/06_directory_service_pass3.md](./pass3/06_directory_service_pass3.md) |

### 第三遍跨模块要点（新增）

1. **入库三消费端**：`ingest_worker` / `PendingIngestProcessor` / `StartupTaskProcessor` 互斥部署（见 pass3/05）。
2. **会话索引生命周期**：`chat_files` 上传即索引 vs `my_files` 绑定时索引；软删除不清理内存索引（pass3/02、04）。
3. **`list_active_files` SQL**：按 `user_files.thread_id` 过滤，个人库 `thread_id=NULL` 可能列不出（pass3/04）。
4. **目录 `func.replace`**：rename/move 存在子串误替换路径风险（pass3/06）。

## 第四遍（集成层 / 方案对照 / 任务清单）

| 文档 | 说明 |
|------|------|
| [pass4/00_integration_layer.md](./pass4/00_integration_layer.md) | `chat_files` / `my_files` / `files` / worker / Agent |
| [pass4/07_agent_and_sse.md](./pass4/07_agent_and_sse.md) | `lightweight_qa_agent` 检索链 |
| [design_vs_implementation_gap.md](./design_vs_implementation_gap.md) | 与《个人文件夹…设计方案》差距 |
| [../backend_services_fix_backlog.md](../backend_services_fix_backlog.md) | **P0–P3 修复任务汇总（推荐排期入口）** |

## 第五遍（路由细审 / 前端 / 测试 / PR 拆分）

| 文档 | 说明 |
|------|------|
| [pass5/01_frontend_integration.md](./pass5/01_frontend_integration.md) | ne-full 与 API 集成 |
| [pass5/02_routers_chat_files.md](./pass5/02_routers_chat_files.md) | `chat_files.py` 端点矩阵 |
| [pass5/03_routers_my_files.md](./pass5/03_routers_my_files.md) | `my_files.py` 端点矩阵 |
| [pass5/04_test_plan.md](./pass5/04_test_plan.md) | pytest / 集成 / E2E 计划 |
| [../backend_services_p0_pr_guide.md](../backend_services_p0_pr_guide.md) | P0 拆 PR 与回归清单 |
| [../REVIEW_INDEX.md](../REVIEW_INDEX.md) | **全审查总索引** |

## 第六遍（files / uploads / kb_docs / 执行摘要）

| 文档 | 说明 |
|------|------|
| [pass6/01_routers_files.md](./pass6/01_routers_files.md) | `files.py` 鉴权与路由 |
| [pass6/02_routers_uploads_and_kb_docs.md](./pass6/02_routers_uploads_and_kb_docs.md) | 入库与 KB 文档 API |
| [../EXECUTIVE_SUMMARY.md](../EXECUTIVE_SUMMARY.md) | 管理层一页摘要 |

## 第七遍（Agent / 文档漂移 / RAG 边界）

| 文档 | 说明 |
|------|------|
| [pass7/01_agent_layer_file_integration.md](./pass7/01_agent_layer_file_integration.md) | Agent 双路径与 services |
| [pass7/02_documentation_drift_audit.md](./pass7/02_documentation_drift_audit.md) | 设计文档过时项 |
| [pass7/03_rag_vs_chat_retrieval_boundary.md](./pass7/03_rag_vs_chat_retrieval_boundary.md) | 勿与 KB 关键词方案混淆 |
| [pass7/04_directories_router_supplement.md](./pass7/04_directories_router_supplement.md) | directories 路由 |

## 第八遍（NEFullFeature / 架构文档 / 闭环）

| 文档 | 说明 |
|------|------|
| [pass8/01_ne_full_feature_agent_review.md](./pass8/01_ne_full_feature_agent_review.md) | 巨石 agent 现状 |
| [pass8/02_agent_architecture_crosswalk.md](./pass8/02_agent_architecture_crosswalk.md) | vs `agent_architecture_redesign.md` |
| [pass8/03_stream_handler_artifacts.md](./pass8/03_stream_handler_artifacts.md) | 工件与下载 |
| [../CLOSURE_AND_REMAINING.md](../CLOSURE_AND_REMAINING.md) | 审查范围闭环 |
