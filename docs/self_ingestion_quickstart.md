# 自研入库（self_ingestion）快速上手指南

> **适用对象**：运维、后端开发、管理后台操作员  
> **最后更新**：2026-07-09  
> **完整设计**：[入库实现方案.md](./入库实现方案.md) · **落地审计**：[入库实现方案审查.md](./入库实现方案审查.md)

---

## 1. 前置条件

自研入库与 legacy LightRAG 入库**长期双轨并存**，互不复写索引。启用 self 链路需同时满足：

| 层级 | 条件 |
| :--- | :--- |
| 全局开关 | `SELF_INGESTION_ENABLED=true`、`SELF_RETRIEVER_ENABLED=true`（`backend/.env`） |
| 知识库配置 | `ingestion_backend=self_ingestion`、`retrieval_backend=self_ingestion`；`hybrid` 为运行时灰度能力，当前公开 KB 创建/切换接口主要暴露 legacy/self |
| KB 级授权 | `self_ingestion_enabled=true`、`self_retriever_enabled=true` |
| 白名单（可选） | `SELF_INGESTION_KB_ALLOWLIST` / `SELF_RETRIEVER_KB_ALLOWLIST` 为空表示不限制；非空时仅列出的 `kb_id` 或 `kb_code` 可用 |

**阻塞策略**：KB 标记为 self 但开关/白名单未满足时，任务进入 **blocked**（`kb_pending_files.status=3`），**不会**回退 legacy，避免污染旧索引。

基础设施：MySQL、Milvus、NebulaGraph、Redis、MinIO 均需可达；LLM/VLM 用于实体关系抽取（默认与 legacy 共用网关模型）。

---

## 2. 数据库迁移（首次部署）

在**目标环境** MySQL 上执行（幂等）：

```bash
cd /home/inspur/myenv/langchian_project
venv314/bin/python scripts/migrations/run_all_self_ingestion_migrations.py
venv314/bin/python scripts/migrations/verify_self_ingestion_schema.py
```

验收要点：

- `knowledge_bases` 含 `ingestion_backend`、`retrieval_backend`、`self_ingestion_enabled`、`self_retriever_enabled`
- 存在 `kb_self_ingest_jobs`、`kb_self_chunks`
- `kb_self_ingest_jobs` 含 `staging_uri`、`retry_count`、`max_retry`、`commit_log_json`、`metrics_json`
- **不存在** 已废弃的 MySQL 图谱/质量/blocks 表（`kb_self_entities`、`kb_self_relations`、`kb_self_quality_*`、`kb_self_blocks` 等）

在**目标环境**执行迁移后，应看到类似输出：

```text
PASSED: schema looks complete
self_ingestion knowledge bases: 2
obsolete self-ingestion tables removed: ['kb_self_blocks']
```

当前 ORM 运行时只直接写 `kb_self_ingest_jobs` 与 `kb_self_chunks`；block/page/bbox/section_path 等块级溯源随 chunk 写入 `extra_json` 和 staging payload。实体、关系、证据、candidate/dropped 质量明细不再进入 MySQL 图谱表。

---

## 3. 创建 self_ingestion 知识库

### 方式 A：管理后台（推荐）

1. 登录 RadAI 管理后台 `/admin/knowledge-bases`
2. 新建知识库 → **入库后端** 选「自研入库」→ **检索后端** 选「自研检索 v2」
3. 保存后确认 KB 详情页显示 `ingestion_backend=self_ingestion`

### 方式 B：API

```bash
curl -X POST "http://localhost:8082/api/v1/knowledge_bases" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_name": "自研测试库",
    "kb_code": "self_ingest_test",
    "ingestion_backend": "self_ingestion",
    "retrieval_backend": "self_ingestion",
    "self_ingestion_enabled": true,
    "self_retriever_enabled": true
  }'
```

创建后可将该 `kb_code` 绑定到智能体（`PUT /api/v1/agents/{code}/kbs`），详见 [agent_kb_binding_module.md](./docs_backend/agent_kb_binding_module.md)。

---

## 4. 启动入库 Worker

```bash
make run-ingest-worker
# 或
bash scripts/ingest_worker_service.sh start
```

Worker 轮询 `kb_pending_files` 中 `status=1` 的记录，经 `PendingIngestUseCase` 分流：

- `legacy_lightrag` → `RAGService`
- `self_ingestion` → `SelfIngestionPipeline.ingest_pending_file()`
- `blocked` → `kb_pending_files.status=3`，错误写入 `error_message`，不回退 legacy

相关环境变量：

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `INGEST_WORKER_POLL_INTERVAL` | `2` | 轮询间隔（秒） |
| `INGEST_WORKER_BATCH_SIZE` | `5` | 每批处理数 |

---

## 5. 上传与触发入库

与管理后台 legacy 库流程相同：

1. 在 KB 详情页上传 `.md` / `.txt` / `.pdf` / `.docx`（或分块上传大文件）
2. 点击「入库」→ `POST /api/v1/uploads/ingest` → `kb_pending_files.status` 变为 `1`
3. Worker 消费后：`status=2` 成功，`status=3` 失败或 blocked

**MD 文件**：self 库走 `markdown_parser.py` → BlockBuilder，不走 legacy `MDIngestor`。

Pending file 状态：

| 值 | 含义 |
| :--- | :--- |
| `0` | 已上传，待启动入库 |
| `1` | 已启动，等待或正在处理 |
| `2` | 完成；同内容 `skip_existing` 也会置为完成 |
| `3` | 失败或 blocked |

自研 job 状态使用小写枚举：`pending`、`processing`、`committing`、`completed`、`partial_success`、`failed`、`repairing`。细粒度阶段见 `kb_self_ingest_jobs.stage`，包括 `parsing`、`chunking`、`extracting`、`embedding`、`staging`、`committing_mysql`、`committing_milvus`、`committing_redis`、`committing_nebula`、`marking_visible`、`committed`。

---

## 5.1 Staging / Commit 机制

self 链路先把完整中间态保存到 staging，再由 `CommitWriter` 统一提交四类索引：

```text
ParserRouter / BlockBuilder / 抽取 / Embedding
  -> MinIO staging/{kb_id}/{job_id}/payload.json
  -> kb_self_ingest_jobs.staging_uri
  -> CommitWriter
```

当前提交顺序：

1. MySQL 写 `kb_self_chunks` 为 `staged` / `is_visible=false`。
2. Milvus `{kb_code}_chunks_v2` 写 chunk 向量，仍不可见。
3. MySQL 标记新版本 `visible`，旧版本 `superseded`，并更新 `kb_documents` 与 `kb_pending_files.status=2`。
4. 并行补 Milvus chunk 可见、Milvus `{kb_code}_entities_v2`、Redis BM25/metadata、NebulaGraph v2。
5. 第 4 步若部分失败，job 为 `partial_success`，可由 repair worker 从 staging 重放失败阶段。

---

## 6. 验证入库结果

### 6.1 MySQL

```sql
-- 任务状态
SELECT job_id, doc_id, status, stage, error_message
FROM kb_self_ingest_jobs
WHERE kb_id = '<kb_id>'
ORDER BY created_at DESC
LIMIT 5;

-- Chunk 数量
SELECT COUNT(*) FROM kb_self_chunks WHERE kb_id = '<kb_id>' AND is_visible = 1;
```

可见 chunk 应满足 `is_visible=1`、`is_active=1`、`doc_status in ('visible','active')`。Embedding 失败且配置允许降级时，`embedding_status` 可能为 `mysql_only`，此时向量路不一定可用，但 MySQL/Redis 路仍可用于回退检索。

### 6.2 管理 API（需管理员）

| 端点 | 用途 |
| :--- | :--- |
| `GET /api/v1/admin/self-ingestion/health` | 队列深度、MySQL/Milvus/Redis/Nebula/Embedding/LLM/VLM 组件状态 |
| `GET /api/v1/admin/self-ingestion/jobs` | 任务列表 |
| `GET /api/v1/admin/self-ingestion/jobs/stats` | 统计 |
| `POST /api/v1/admin/self-ingestion/jobs/{job_id}/repair` | 单任务修复 |
| `POST /api/v1/admin/self-ingestion/repair/scan` | 扫描并修复可重放任务 |
| `POST /api/v1/admin/self-ingestion/kb/{kb_id}/switch-backend` | 切换 KB 后端与开关 |
| `POST /api/v1/admin/self-ingestion/shadow-rebuild` | 将 legacy 文档 shadow 入队到 self |
| `GET /api/v1/health/self-ingestion` | 健康检查别名（无需 admin 前缀） |

### 6.3 检索 smoke

```bash
curl -X POST "http://localhost:8082/api/v1/rag/retrieve" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "与上传文档相关的关键词",
    "kb_id": "<kb_id>",
    "top_k": 5
  }'
```

self 库应经 `SelfDirectRetrieverV2` 四路召回；响应中可查看 `source_backend` 等溯源字段。

四路索引要点：

| 路径 | 主存储 | 验收点 |
| :--- | :--- | :--- |
| Path-A 实体向量 | Milvus `{kb_code}_entities_v2` | 实体状态为 `active/weak_evidence` 且可见 |
| Path-B 关键词 | Redis BM25 inverted postings + MySQL chunk 回退 | Redis corpus 有 chunk；删除/重入库后不残留旧 chunk |
| Path-C chunk 向量 | Milvus `{kb_code}_chunks_v2` | `is_visible=true`、`commit_status=committed`、`doc_status` 可见 |
| Path-D 图扩展 | NebulaGraph `entity_v2/relation_v2/evidence_v2` | `relation_v2` 可按 nGQL 命中，MySQL 图谱表不是主路径 |

### 6.4 删除不可召回

删除文档后，再次检索应不再返回该文档 chunk（软删除 + 可见性过滤）。

---

## 7. 常见问题

| 现象 | 可能原因 | 处理 |
| :--- | :--- | :--- |
| 入库一直 blocked | 全局开关未开或不在白名单 | 检查 `.env` 与 `SELF_INGESTION_KB_ALLOWLIST` |
| status=3 无明细 | Worker 未运行或 MinIO 文件缺失 | 查 `ingest_worker` 日志、`kb_pending_files.error_message` |
| job 为 `partial_success` | MySQL 已可见，但 Milvus/Redis/Nebula 某一路失败 | 调用 `/admin/self-ingestion/jobs/{job_id}/repair` 或 `/repair/scan` |
| Milvus 无向量但 MySQL 有 chunk | embedding 配置缺失、失败或外部索引部分失败 | 检查 `embedding_status`、`metrics_json.external_failures`、repair worker |
| 检索无结果 | `retrieval_backend` 仍为 legacy | KB 详情改为 `self_ingestion` 并确认 `SELF_RETRIEVER_ENABLED` |
| legacy 库被误走 self | 不应发生；检查 `ingestion_backend` 字段 | 历史库应为 `legacy_lightrag` |

---

## 8. 单元测试（无需外网 MySQL）

```bash
venv314/bin/python -m pytest \
  backend/tests/test_self_ingestion_schema_extraction.py \
  backend/tests/test_self_ingestion_repair_worker.py \
  backend/tests/test_kb_backend_isolation.py \
  backend/tests/test_retrieval_pipeline_self_routing.py \
  backend/tests/test_redis_bm25_index.py \
  backend/tests/test_multimodal_parsers.py \
  -q
```

2026-07-03 本地执行 `test_kb_backend_isolation` + `test_self_ingestion_schema_extraction`：**40 passed**。

常用运维/回归脚本：

```bash
venv314/bin/python scripts/eval_self_extraction.py --mode llm
venv314/bin/python scripts/check_self_ingestion_consistency.py
venv314/bin/python scripts/repair_self_ingestion.py
venv314/bin/python scripts/reingest_self_kb.py
venv314/bin/python scripts/benchmark_milvus_visibility.py
```

---

## 9. 上线前 P0 清单（摘自审查报告）

- [ ] 目标环境执行迁移 + `verify_self_ingestion_schema.py` PASSED
- [ ] 在测试 KB 完成 MD + PDF 实际上传入库 E2E
- [ ] 四路召回联调（Milvus entities/chunks、Redis BM25、NebulaGraph v2 有命中）
- [ ] 验证 `partial_success` 可从 `staging_uri` repair
- [ ] 三组业务回归：旧库 legacy 入库/查询、新库 self 入库/查询、旧库在新入口兼容查询
- [ ] 删除文档后不可召回

详细项见 [入库实现方案审查.md §6](./入库实现方案审查.md)。
