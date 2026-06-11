# 逐文件审查：deerflow / scripts / tests / skills / 根模块

> 只读审查：**226** 个 `.py`（deerflow 148、scripts 66、tests 6、skills 4、根 2）

---

## backend 根

| 文件 | 严重度 | 问题摘要 | 标签 |
|------|--------|----------|------|
| `__init__.py` | info | `sys.path.insert` 可能掩盖包结构 | imports |
| `cli.py` | medium | `subprocess.run` 无 `check=`；`run-api` 忽略 `--host` 硬编码 `0.0.0.0` | subprocess, cli |

---

## deerflow/ 与 core 重复（摘要）

约 **20+** 模块与 `backend/core/` 并存，相似度 43–90%：

| deerflow | core | 备注 |
|----------|------|------|
| `agents/middlewares/tool_error_handling_middleware.py` | `tool_error_handling.py` | ~90% |
| `config/model_config.py` | `model_config.py` | ~90% |
| `tracing/factory.py` | `tracing.py` | ~83% |
| `agents/features.py` | `features.py` | ~82% |
| `skills/types.py` | `types.py` | ~68% |
| `tools/builtins/task_tool.py` | `tools/task_tool.py` | **1149 vs ~449 行**，架构分叉 |

**混合引用**: `deerflow/tools/builtins/task_tool.py` 运行时 import `backend.core.skills.*`

---

## deerflow/ 按子目录（148 文件）

### 严重 / 重点关注

| 文件 | 行≈ | 严重度 | 问题摘要 |
|------|-----|--------|----------|
| `sandbox/tools.py` | 1345 | medium | 19 处宽泛 except；4 type:ignore；global 锁 |
| `tools/builtins/task_tool.py` | 1149 | medium | 依赖 core.skills；7 处 except；硬编码 IP 过滤 |
| `client.py` | 953 | low | 集成过多职责；全局 config |
| `community/aio_sandbox/aio_sandbox_provider.py` | 638 | low | 7 处 except |
| `subagents/executor.py` | 751 | low | global 任务表；asyncio.run 注释完善 |
| `agents/lead_agent/prompt.py` | 549 | low | 5 处 except 吞模板错误 |
| `runtime/runs/worker.py` | 中 | low | `TODO Phase 2` rollback 为 pass |
| `sandbox/local/local_sandbox.py` | 中 | medium | subprocess 无 check= |
| `reflection/resolvers.py` | 中 | medium | PEP695 泛型；core 已简化 |

### config/（16 文件）— 共性

| 模式 | 说明 |
|------|------|
| global 单例 | `app_config`, `extensions_config`, `paths`, `checkpointer_config` 等几乎全部 lazy global |
| 双份维护 | `app_config` 381 行 vs core 197 行；`paths` 341 vs 161 |

| 文件 | 严重度 |
|------|--------|
| `config/app_config.py` | low（dup-core, global-state, import 时 load_dotenv） |
| 其余 config/*.py | info~low（多数 global 单例） |

### agents/middlewares/（17 文件）

| 文件 | 严重度 | core 重复 |
|------|--------|-----------|
| `tool_error_handling_middleware.py` | info | ~90% |
| `dangling_tool_call_middleware.py` | info | ~72% |
| `loop_detection_middleware.py` | info | ~62% |
| `llm_error_handling_middleware.py` | low | core 无完整对应 |
| `memory_middleware.py` 等 deerflow 独有 | info | — |

### 其余 deerflow 子包（逐文件严重度 bulk）

| 子目录 | 文件数 | ok/info | low | medium |
|--------|--------|---------|-----|--------|
| `agents/` (除上表) | ~25 | 多数 | 3 | 0 |
| `community/` | ~17 | 14 | 3 | 0 |
| `guardrails/` | 4 | 3 | 0 | 0 |
| `mcp/` | 5 | 3 | 2 | 0 |
| `models/` | 9 | 8 | 1 | 0 |
| `runtime/` | 12 | 10 | 2 | 0 |
| `sandbox/` (除 tools) | 10 | 9 | 0 | 1 |
| `skills/` | 6 | 6 | 0 | 0 |
| `subagents/` | 8 | 7 | 1 | 0 |
| `tools/` (除 task_tool) | 7 | 7 | 0 | 0 |
| `tracing/` | 2 | 2 | 0 | 0 |
| `uploads/` | 2 | 2 | 0 | 0 |
| `utils/` | 3 | 1 | 2 | 0 |

> **deerflow 148 文件中约 120+ 为 info/ok**（静态扫描无显著信号）；风险集中在 5 个超大文件与 config 全局单例。

---

## scripts/（66 文件）

### 【P0】硬编码密钥

| 文件 | 严重度 | 问题 |
|------|--------|------|
| `scripts/clean_by_file_name.py` | **high** | 硬编码 Neo4j 密码 `qwertyuiop@222neo4j` |
| `scripts/clean_knowledge_base_by_filename.py` | **high** | 同上 |
| `scripts/openai_proxy.py` | **high** | 硬编码 `TARGET_API_KEY` |
| `scripts/openai_https_proxy.py` | **high** | 硬编码 API Key + 内网 URL |

### 硬编码路径 `<server_path>/...`（medium，共 20+ 文件）

`check_milvus_memory_usage.py`, `cleanup_discarded.py`, `delete_doc_by_file_path.py`, `inspect_conversations.py`, `list_tables.py`, `migrate_*.py`, `restore_test1_backup.py`, `run_insert_docx.py`, `test_ivf_pq_index.py`, 等。

### 裸 except / 宽泛 except（medium/low）

`check_and_move_processed_files_v3.py`（裸 except）, `migrate_all_to_ivf_pq_parallel.py`, `rebuild_all_ivf_pq.py`, `build_entity_source_index.py`, 等。

### scripts 中相对干净（info）

`build_keyword_index.py`, `clear_llm_cache.py`, `clear_parse_cache.py`, `create_kb_directories_table.py`, `list_gateway_models.py`, `run_init_raganything.py`, `sync_minio_to_mysql.py`, 等。

---

## tests/（6 文件）

| 文件 | 严重度 | 问题 |
|------|--------|------|
| `test_permissions.py` | ok | 标准 pytest 异步 |
| `test_surrogate_cleaning.py` | low | sys.path 注入；print |
| `check_metadata_chinese.py` | medium | **非 pytest**；硬编码路径；应迁 scripts/ |
| `check_redis_bm25.py` | medium | 诊断脚本 |
| `test_bm25_issue.py` | medium | 命名像测试实为脚本 |
| `test_live_index.py` | medium | 依赖 live 环境 |

---

## skills/public/*/scripts（4 文件）

| 文件 | 严重度 | 问题 |
|------|--------|------|
| `data-analysis/scripts/analyze.py` | low | 566 行；9 处 except |
| `github-deep-research/scripts/github_api.py` | low | 7 处 except；print |
| `image-generation/scripts/generate.py` | low | print |
| `video-generation/scripts/generate.py` | low | print |

---

## 全库自动扫描统计（487 文件）

| 严重度 | 数量 | 说明 |
|--------|------|------|
| ok | 226 | 无显著静态信号 |
| low | 227 | 轻微：行数/少量 except/print |
| medium | 21 | 复杂度、TODO、裸 except |
| high | 13 | 超大文件、pickle、dev-key、空文件等 |

详见 [backend_per_file_index.md](./backend_per_file_index.md) 完整表格。

---

## 优先动作（deerflow/scripts）

1. **立即**轮换 Neo4j/OpenAI 硬编码密钥并移出 Git 历史  
2. 确立 deerflow → core 单向迁移，停止双份 middleware/config  
3. 拆分 `sandbox/tools.py`、`task_tool.py`  
4. `cli.py` 修复 `--host`；subprocess `check=True`  
5. 将 `tests/check_*.py` 迁至 `scripts/`  
