# backend/services 修复任务清单（汇总）

> 由三轮 `services` 审查 + 第四遍集成/方案对照整理。  
> **仅任务规划，不含代码修改。**  
> 明细见：`plans/debug/services/`、`plans/debug/services/pass3/`

---

## 优先级说明

| 级别 | 含义 |
|------|------|
| P0 | 数据错误、安全风险、生产必炸 |
| P1 | 功能/一致性缺陷，影响主路径 |
| P2 | 可维护性、性能、体验 |
| P3 | 文档、风格、锦上添花 |

---

## P0 — 立即处理

| ID | 模块 | 任务 | 涉及文件 |
|----|------|------|----------|
| P0-07 | 路由 | **`POST /api/v2/files/export-docx` 加鉴权与 body 大小限制** | `files.py` |
| P0-08 | 路由 | **`/download-audit` 路由注册顺序**（置于 `/{file_id}` 之前） | `files.py` |
| P0-01 | 入库 | **三消费端互斥**：`ingest_worker` / `PendingIngestProcessor` / `StartupTaskProcessor` 只保留一种；文档写明部署约束 | `startup_task_processor.py`, `worker/*`, `uploads.py`, 运维文档 |
| P0-02 | 入库 | 缓存路径改为 `{file_id}_{file_name}` 或 UUID，消除同名覆盖 | `startup_task_processor.py`, `ingest_pending_processor.py` |
| P0-03 | 索引 | 实现 `split_text` 的 `chunk_overlap`，或删除参数并改文档 | `chat_file_index.py` |
| P0-04 | 索引 | `ChatFileIndexManager` 并发锁（按 thread 或全局）；文档注明多 worker 索引不共享 | `chat_file_index.py` |
| P0-05 | 目录 | `rename_directory` / `move_directory` 弃用 `func.replace` 子串替换，改前缀安全更新 | `directory_service.py` |
| P0-06 | 路由 | `my_files` 软删除：`DELETE session_files` + 清理各 session 的 `ChatFileIndex.remove_document` | `my_files.py`, `chat_file_index.py` |

---

## P1 — 下一迭代

| ID | 模块 | 任务 | 涉及文件 |
|----|------|------|----------|
| P1-01 | 索引 | BM25 与 TF-IDF 统一分词器 | `chat_file_index.py`, `bm25_index.py` 或 wrapper |
| P1-02 | 索引 | `add_document` 同 `file_id` 幂等（先 remove 再 add） | `chat_file_index.py` |
| P1-03 | 索引 | LRU 驱逐后：attach/打开会话时从 `metadata.content` 重建索引 | `my_files.py`, `chat_files.py`, manager |
| P1-04 | 解析 | 收紧扩展名：`.doc`/`.xls`/未知类型与 MinerU 白名单一致 | `chat_file_parser.py`, 两路由 `ALLOWED_*` |
| P1-05 | 解析 | `long_doc` 不全文入 DB JSON（仅存摘要+块数，正文仅 MinIO/索引） | `chat_file_parser.py`, `chat_files.py`, `my_files.py` |
| P1-06 | 用户文件 | 合并 `register_*` async/sync 为单 core；MinIO 登记补 `size_bytes` | `user_file_service.py` |
| P1-07 | 用户文件 | 列表 API 去掉 `local_abs_path`；会话文件列表改 JOIN `session_files` | `user_file_service.py`, `files.py` |
| P1-08 | 入库 | Startup 与 Worker 对 MD 文件路由一致（`process_md_content` vs multimodal） | `startup_task_processor.py`, `ingest_pending_processor.py` |
| P1-09 | 目录 | `DirectoryCRUD.create` 与 `DirectoryService` 事务边界统一（避免双 commit） | `directory_crud.py`, `directory_service.py` |
| P1-10 | 方案 | attach 时更新 `last_used_at`（设计 7.2 未实现） | `my_files.py`, 模型迁移 |
| P1-11 | Agent | 公开 `is_generic_summary_query`，禁止引用 `_chunks` | `chat_file_index.py`, `lightweight_qa_agent.py` |
| P1-12 | Agent | `agent.py` 按 lightweight_mode 修正长文档工具提示文案 | `agent.py`, `csh_agent.py` |
| P1-13 | 文档 | 同步 `chat_file_upload_design` / `personal_files_module` MinerU 与 BM25 状态 | docs/ |

---

## P2 — 质量与性能

| ID | 模块 | 任务 |
|----|------|------|
| P2-01 | 索引 | 关键词路部分命中时仍走 BM25 融合 |
| P2-02 | 索引 | `_expand_context` 按 `(file_id, chunk_index)` 扩展 |
| P2-03 | 索引 | 块数为 0 时 `manager.remove(thread_id)` |
| P2-04 | 解析 | 截断标志 `truncated: true`；大文件 base64 跳过 `clean_surrogates` |
| P2-05 | 解析 | 文本编码探测（UTF-8/GBK） |
| P2-06 | 用户文件 | `user_files` 唯一约束防重复登记；写入 `sha256` |
| P2-07 | 用户文件 | `write_download_audit` docstring 注明 session commit 契约 |
| P2-08 | 入库 | Redis 连接池复用；失败重试与 `retry_count` |
| P2-09 | 入库 | `_claim_pending_files` 真正 UPDATE claim |
| P2-10 | 目录 | `get_directory_tree` 计数单一真相源（`recalculate`） |
| P2-11 | 目录 | 虚拟日期节点 API 文档：不可 rename/delete |
| P2-12 | 路由 | `list_my_files` 总数用 `COUNT(*)` 非 `len(all())` |
| P2-13 | 路由 | `chat_files` 索引失败时回滚或标记 `parse_status` |
| P2-14 | 包 | 扩展 `services/__init__.py` 或包级 README 说明 import 约定 |

---

## P3 — 后续

| ID | 任务 |
|----|------|
| P3-01 | 统一 `chat-files` + `my-files` 上传路由（方案与 `personal_files_module.md`） |
| P3-02 | 配置外置：`max_threads`、阈值、轮询间隔 |
| P3-03 | 指标：索引驱逐、search 三路占比、解析 method 分布 |
| P3-04 | 单元测试矩阵（见各 `pass3` 文档「建议测试」） |
| P3-05 | 删除未使用 import（`threading`, `field`） |

---

## 建议实施顺序（2 周节奏示例）

**第 1 周（稳定性）**  
P0-01 → P0-02 → P0-05 → P0-06 → P0-03 → P0-04

**第 2 周（产品一致性）**  
P1-04 → P1-06 → P1-07 → P1-02 → P1-03 → P1-10

**并行（知识库）**  
P0-01 + P1-08 与运维确认 worker 部署方式

---

## 验收标准（抽样）

1. 两 worker 同时跑 ingest，同一 `file_id` 仅入库一次。  
2. 同名 PDF 并发入库，缓存内容不串。  
3. 重命名目录 `a`→`b` 不误改 `ab/xx` 路径。  
4. 个人库删除长文档后，同 session Agent 检索不到该文件。  
5. attach 长文档后重启 API（单 worker），绑定的 session 仍可检索（或明确文档说明需 re-attach）。  
6. 块边界关键词出现在 overlap 区域时可被检索命中。

---

*生成日期：2026-05-30*
