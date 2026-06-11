# backend/services 审查 — 执行摘要

**审查日期**：2026-05-30  
**范围**：`backend/services`（6 文件）+ 直接关联路由/前端/Worker  
**文档规模**：6 轮 · 30+ 篇 · 见 [REVIEW_INDEX.md](./REVIEW_INDEX.md)

---

## 一句话结论

会话级文件检索（`chat_file_index` / `chat_file_parser`）**可用但不宜直接上多实例生产**；用户文件与知识库入库存在 **P0 级数据/安全/路由** 问题，建议按 Hotfix 清单分 3 个发版修复。

---

## 必须立即处理（P0，7 项）

| # | 问题 | 位置 |
|---|------|------|
| 1 | 入库三消费端可能双跑（worker + StartupTaskProcessor） | worker + `startup_task_processor` |
| 2 | 入库缓存仅用 `file_name`，并发覆盖 | processor ×2 |
| 3 | `chunk_overlap` 未实现 | `chat_file_index` |
| 4 | 索引 Manager 无锁 + 多 worker 不共享 | `chat_file_index` |
| 5 | 目录 `func.replace` 误改路径 | `directory_service` |
| 6 | 软删除不清理 SessionFile/内存索引 | `my_files` + index |
| 7 | **`POST /api/v2/files/export-docx` 无鉴权** | `files.py` |

---

## Agent 层补充（第七遍）

| 问题 | 说明 |
|------|------|
| Lightweight 无 `chat_file_search` 工具 | `agent.py` 仍提示「请使用工具」 |
| 长文档三重上下文 | DB 前缀 + 内联检索 + 内存前缀 |
| 索引驱逐后 | Graph 无工具；DB 前缀仍注入 |

详见 [pass7/01_agent_layer_file_integration.md](./services/pass7/01_agent_layer_file_integration.md)。

---

## 高优先级（P1 精选，12 项）

- BM25/TF-IDF 分词不一致；`add_document` 非幂等  
- `long_doc` 全文进 MySQL JSON（≤50 万字符）  
- `list_files` / 个人库列表 SQL 与 `SessionFile` 不一致  
- MinIO 登记缺 `size_bytes`；`user_file_service` 重复代码  
- `chat_files` 索引失败仍 `strategy=indexed`  
- 前端 attach 不检查 `res.ok`；上传按文件名错配  
- `GET /files/download-audit` 路由顺序错误  
- `rename-category` 多段 `new_path` 语义错误  
- 入库 MD 路由 Startup vs Worker 不一致  
- `last_used_at` / 方案未落地  
- Lightweight 与 Graph 长文档提示/能力不一致  
- 设计文档写 MinerU 未接入（Parser 已支持）  

---

## 与产品方案差距

《个人文件夹文件管理与上传复用设计方案》场景 D「删除后再引用失效」——**当前未满足**（索引与 metadata 仍可用）。  
详见 [design_vs_implementation_gap.md](./services/design_vs_implementation_gap.md)。

---

## 建议发版节奏

| 版本 | 内容 | 文档 |
|------|------|------|
| Hotfix A | export-docx 鉴权 + 路由顺序 + 入库缓存名 + 消费端文档 | pass6/01, p0_pr_guide PR-1/2 |
| Hotfix B | 目录 replace + 软删索引 | p0_pr_guide PR-3/4 |
| 迭代 C | overlap + 锁 + 列表 SQL + 前端 attach | p0_pr_guide PR-5/6, pass5/01 |

---

## 测试债务

现有：**1** 个文件（`clean_surrogates`）。  
建议首期新增：**15+** 单元/集成用例 → [pass5/04_test_plan.md](./services/pass5/04_test_plan.md)。

---

## 不在本次范围

- `rag/service.py` 全量  
- `lightweight_qa_agent` 全文件（仅文件检索链）  
- `agent_architecture_redesign.md` 巨石 Agent 拆分（另一审查线）  
- 前端除 `ne-full` 外站点  

---

## 负责人分工建议

| 域 | 模块 |
|----|------|
| 对话附件 | services + chat_files + my_files + 前端 upload |
| 知识库 | uploads + worker + directory_service + kb_docs |
| 平台安全 | files export-docx、下载鉴权 |
| 基础设施 | 多 worker 部署说明、Redis、MinIO |

---

*供技术负责人排期；细节以各轮 markdown 为准。*
