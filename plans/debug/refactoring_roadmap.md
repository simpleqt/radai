# Backend 质量整改路线图（可执行）

> 基于 [backend_code_review.md](./backend_code_review.md) + 逐文件审查 + 深度审查续篇。  
> **不修改代码**，仅排期建议。可按 Sprint 切分 PR。

---

## Sprint 0 — 安全热修（1–2 天）

| ID | 任务 | 文件 | 验收 |
|----|------|------|------|
| S0-1 | `retrieve_internal` 无 secret 则 403 | `api/routers/rag.py` | 单测：无 header → 403 |
| S0-2 | 移除/环境化 `SMS_SECRET` | `api/routers/auth_sms.py` | 密钥不在 git |
| S0-3 | 轮换 scripts 中 Neo4j/OpenAI 密钥 | `scripts/clean_*.py`, `openai_proxy.py` | git grep 无 sk- / neo4j 明文 |
| S0-4 | 生产禁用 `<dev_api_key>` | `db/init_db.py` | ENV=prod 不创建 |
| S0-5 | BM25 pickle 改签名或 msgpack | `rag/keywords/bm25_index.py` | 安全评审通过 |

---

## Sprint 1 — 重复代码收敛（1 周）

| ID | 任务 | 产出 |
|----|------|------|
| S1-1 | 抽取 `custom/api/v2/_common.py` | agent/csh 各 <300 行 |
| S1-2 | `core/streaming/think_tags.py` | 删除 6 处重复 |
| S1-3 | `core/uploads/allowed_types.py` | 4 处白名单统一 |
| S1-4 | `storage/client.py` 公开 API | 10 处私有 import 迁移 |
| S1-5 | 删除 `rag/service.py` 重复 Redis patch | 仅保留 917 或合并为 1 份 |
| S1-6 | 删除或实现 `standalone_query.py` | 无空模块 |

---

## Sprint 2 — God file 拆分（2 周）

| 优先级 | 文件 | 目标模块 |
|--------|------|----------|
| 1 | `storage/__init__.py` | client, paths, documents, chunks |
| 2 | `rag/service.py` | bootstrap, query_service, ingest_service |
| 3 | `api/routers/rag.py` | retrieve_routes, ingest_routes, admin_routes |
| 4 | `custom/tools/multi_kb_search_tool.py` | retriever 调用 + 薄 tool 包装 |
| 5 | `custom/api/v2/agent.py` | 仅 NE 差异 + 公共层 |

---

## Sprint 3 — 架构对齐（2 周）

| ID | 任务 |
|----|------|
| S3-1 | `kb_registry.yaml` + DB binding 读取，去除 UUID 硬编码 |
| S3-2 | `services/chat_files.py` — agent API 不再 import routers |
| S3-3 | `rag/facade.get_rag_instance()` 替代 `_get_rag_instance` |
| S3-4 | deerflow middleware → core re-export，标 `@deprecated` |
| S3-5 | 更新 agent_architecture_redesign.md 勘误 |

---

## Sprint 4 — 质量工程（持续）

| ID | 任务 |
|----|------|
| S4-1 | `pyproject.toml` + ruff + pytest CI |
| S4-2 | 核心单测：auth, retrieve_internal, think_tags, bm25 serialize |
| S4-3 | `tests/check_*.py` → `scripts/diagnostics/` |
| S4-4 | Redis 连接池（http_server middleware + auth） |
| S4-5 | 覆盖率门槛 40% → 60%（api/v2, rag/retriever） |

---

## 依赖关系

```text
┌─────────────┐
│ Sprint 0 安全 │
└─────────────┘

  Sprint 0 安全 ──────────────► S1

  S1 ──────────────► S2

  S2 ──────────────► S3

  S3 ──────────────► Sprint 4 测试

```

---

## 风险与回滚

| 变更 | 风险 | 缓解 |
|------|------|------|
| agent/csh 合并 | SSE 回归 | 黄金流快照测试 |
| rag/service 拆分 | 入库失败 | 分 PR；staging 全量入库 |
| internal API 收紧 | 子 agent 断链 | 先日志告警一周再 enforce |
| pickle 格式变更 | 索引失效 | 版本号 + 重建脚本 |

---

## 度量目标（8 周后）

| 指标 | 当前 | 目标 |
|------|------|------|
| >1000 行文件数 | 15+ | ≤5 |
| high 自动扫描文件 | 13 | 0 |
| pytest 用例数 | ~4 文件 | ≥80 cases |
| agent/csh 重复行 | ~1186 | <200 |
| 私有 storage import | 10 | 0 |
