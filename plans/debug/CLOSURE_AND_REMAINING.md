# 审查闭环与剩余范围

> `backend/services` 及相关链路的审查收尾说明（2026-05-30）。  
> **未改代码。**

---

## 1. 已完成审查（按模块）

| 模块 | 轮次 | 文档入口 |
|------|------|----------|
| `services/__init__.py` | 二、三 | [01_init_py.md](./services/01_init_py.md) |
| `chat_file_index.py` | 二–三、七 | [02](./services/02_chat_file_index_py.md), [pass3/02](./services/pass3/02_chat_file_index_pass3.md) |
| `chat_file_parser.py` | 二–三 | [03](./services/03_chat_file_parser_py.md) |
| `user_file_service.py` | 二–三、四 | [04](./services/04_user_file_service_py.md) |
| `startup_task_processor.py` | 二–三、四、六 | [05](./services/05_startup_task_processor_py.md) |
| `directory_service.py` | 二–三、六、八 | [06](./services/06_directory_service_py.md) |
| `chat_files.py` | 四–五 | [pass5/02](./services/pass5/02_routers_chat_files.md) |
| `my_files.py` | 四–五 | [pass5/03](./services/pass5/03_routers_my_files.md) |
| `files.py` | 四、六 | [pass6/01](./services/pass6/01_routers_files.md) |
| `uploads.py` + kb_docs | 六 | [pass6/02](./services/pass6/02_routers_uploads_and_kb_docs.md) |
| `directories.py` | 六、八 | [pass7/04](./services/pass7/04_directories_router_supplement.md) |
| 前端 ne-full | 五 | [pass5/01](./services/pass5/01_frontend_integration.md) |
| Agent 层 | 七–八 | [pass7/01](./services/pass7/01_agent_layer_file_integration.md), [pass8/01](./services/pass8/01_ne_full_feature_agent_review.md) |
| stream_handler 工件 | 八 | [pass8/03](./services/pass8/03_stream_handler_artifacts.md) |
| 设计文档漂移 | 七–八 | [pass7/02](./services/pass7/02_documentation_drift_audit.md), [pass8/02](./services/pass8/02_agent_architecture_crosswalk.md) |
| KB vs 会话边界 | 七 | [pass7/03](./services/pass7/03_rag_vs_chat_retrieval_boundary.md) |

---

## 2. 汇总产出物

| 文档 | 用途 |
|------|------|
| [REVIEW_INDEX.md](./REVIEW_INDEX.md) | 总导航 |
| [EXECUTIVE_SUMMARY.md](./EXECUTIVE_SUMMARY.md) | 管理层摘要 |
| [backend_services_fix_backlog.md](./backend_services_fix_backlog.md) | P0–P3 任务 |
| [backend_services_p0_pr_guide.md](./backend_services_p0_pr_guide.md) | PR 拆分 |
| [pass5/04_test_plan.md](./services/pass5/04_test_plan.md) | 测试计划 |

---

## 3. 刻意未深审范围

| 范围 | 原因 |
|------|------|
| `backend/rag/**` 全量 | 仅边界与 ingest 交叉；KB 有独立算法文档 |
| `ne_full_feature_agent` 每一行 | 第八遍结构/耦合已审，未行级覆盖 |
| `csh_full_feature_agent.py` | 与 NE 对称，问题类型类似 |
| `document_processing/**` | 另一套文档处理管线 |
| `guest.py` / 访客端 | 强制 lightweight，文件能力有限 |
| `core/agents/middlewares/**` | 引擎层，非 services |
| 性能压测 / 安全渗透 | 仅静态阅读 |

若需继续，建议优先级：

1. `csh_full_feature_agent.py` 差异对比 NE  
2. `document_processing` 与 `ChatFileParser` 重复度  
3. 基于 `pass5/04` 落地首批 pytest  

---

## 4. P0 统计（终版）

| # | 项 |
|---|-----|
| 1–6 | 见 [EXECUTIVE_SUMMARY](./EXECUTIVE_SUMMARY.md) |
| 7 | export-docx 无鉴权 |
| 8 | download-audit 路由顺序 |

---

## 5. 审查方法说明

- 静态阅读 + grep 调用链  
- 对照 plans/docs 设计文档  
- **未**运行测试、**未** ruff/mypy 全量、**未** 动态渗透  

---

## 6. 声明

本审查包用于排期与风险沟通；修复后应回归更新 pass3 条目的「是否仍成立」。

---

*审查闭环文档*
