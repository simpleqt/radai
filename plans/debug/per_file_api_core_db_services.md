# 逐文件审查：api / core / db / config / services / storage / agents / integrations / kafka / worker / workflows

> 只读审查，共 **130** 个 `.py` 文件。与 [backend_per_file_index.md](./backend_per_file_index.md) 自动扫描交叉对照。

**严重度**: `high` | `medium` | `low` | `ok`

---

## api/（34 文件）

| 文件 | 行数≈ | 严重度 | 问题摘要 | 标签 |
|------|-------|--------|----------|------|
| `api/__init__.py` | 5 | ok | 薄封装 | — |
| `api/dependencies/__init__.py` | 19 | ok | — | — |
| `api/dependencies/auth.py` | 193 | medium | `datetime.now()` 无时区；每次认证新建 Redis 并 `aclose` | security, async |
| `api/http_server.py` | 827 | medium | 中间件 `except Exception: pass`；CORS 硬编码 IP/域名；与 mcp 重复 think-tag 剥离 | security, duplication |
| `api/guest_http_server.py` | 293 | low | 与主服务启动逻辑重复；预热 KB 硬编码 | duplication |
| `api/mcp_server.py` | 145 | low | think-tag 剥离与 http_server 重复 | duplication |
| `api/routers/__init__.py` | 9 | ok | — | — |
| `api/routers/agents.py` | 270 | low | 路由层业务偏多 | maintainability |
| `api/routers/artifacts.py` | 49 | ok | — | — |
| `api/routers/asr.py` | 92 | ok | — | — |
| `api/routers/auth.py` | 591 | medium | `verify_password` 宽泛 except 返回 False；CAS/登录复杂无单测 | security, testing |
| `api/routers/auth_sms.py` | **high** | **源码硬编码 `SMS_SECRET`**；验证码进程内 dict；`print` 日志 | security |
| `api/routers/chart_images.py` | 142 | medium | GET 无认证（capability URL）；import 时 `mkdir` 副作用 | security |
| `api/routers/chat_files.py` | 412 | low | 扩展名白名单与 my_files/uploads 重复；sync/async session 混用 | duplication, async |
| `api/routers/conversations.py` | 355 | medium | 流式路径宽泛 except；职责偏多 | async, maintainability |
| `api/routers/datasets.py` | 526 | medium | 多处 except Exception；Excel 与权限耦合在路由 | maintainability |
| `api/routers/directories.py` | 476 | low | 体量大未分层 | maintainability |
| `api/routers/document_ingestion.py` | 336 | low | `to_thread` 合理；错误映射可统一 | async |
| `api/routers/document_processing/__init__.py` | 32 | ok | — | — |
| `api/routers/document_processing/models.py` | 162 | ok | — | — |
| `api/routers/document_processing/document_routes.py` | 80 | ok | — | — |
| `api/routers/document_processing/task_routes.py` | 212 | low | API Key 与 session 认证模型不一致 | security |
| `api/routers/document_processing/upload_routes.py` | 330 | low | 同步 `open(wb)` 可能阻塞事件循环 | async |
| `api/routers/document_processing/chunked_upload_routes.py` | 302 | low | 分片逻辑复杂缺集成测试 | testing |
| `api/routers/extraction.py` | 444 | low | 端点结构重复可提取 | duplication |
| `api/routers/files.py` | 261 | low | import `storage._get_minio_client` 私有 API | naming |
| `api/routers/keywords.py` | 378 | low | 管理 API 密集 | maintainability |
| `api/routers/knowledge_base.py` | 312 | low | 与 documents 职责重叠 | duplication |
| `api/routers/knowledge_base_documents.py` | **high** | **~800 行 God 路由**；私有 MinIO；权限/入库/存储混杂 | maintainability, security |
| `api/routers/mcp.py` | 281 | low | MCP 认证路径需与主应用一致 | security |
| `api/routers/models.py` | 143 | low | `# TODO: 验证重名` 未实现 | maintainability |
| `api/routers/my_files.py` | 435 | low | 扩展名白名单第三处副本 | duplication |
| `api/routers/rag.py` | **high** | **~1494 行**；硬编码 KB UUID；内部 secret 未配置仍放行；线程/subagent 分支多 | maintainability, security |
| `api/routers/uploads.py` | 638 | medium | 体量大；白名单重复 | duplication |

---

## agents/（4 文件）

| 文件 | 行数≈ | 严重度 | 问题摘要 | 标签 |
|------|-------|--------|----------|------|
| `agents/base/__init__.py` | 10 | ok | — | — |
| `agents/base/agent_factory.py` | 407 | low | 工厂与 RAG 耦合 | maintainability |
| `agents/base/base_agent.py` | 459 | low | 与 `core/agents` 边界重叠；注释掉的 import | duplication |
| `agents/nuclear_electronics_agent.py` | 315 | low | 末尾 except 兜底 | maintainability |

---

## config/（6 文件）

| 文件 | 行数≈ | 严重度 | 问题摘要 | 标签 |
|------|-------|--------|----------|------|
| `config/__init__.py` | 3 | ok | — | — |
| `config/settings.py` | 741 | medium | 单文件承载全部 env，难审计默认值/密钥 | security, maintainability |
| `config/logging.py` | 264 | low | 与 loggers/handlers 职责交叉 | maintainability |
| `config/loggers.py` | 148 | ok | — | — |
| `config/filters.py` | 268 | low | contextvars 测试需清理 | testing |
| `config/handlers.py` | 561 | medium | Kafka handler 宽泛 except；失败用 `print` | maintainability |

---

## core/（61 文件）

### core/agents/

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `__init__.py` | ok | — |
| `config_compat.py` | low | except 兼容旧配置 |
| `context/types.py` | ok | — |
| `factory.py` | low | 与 deerflow 工厂并行 |
| `features.py` | ok | — |
| `state.py` | ok | — |
| `thread_key.py` | ok | re-export |
| `tool_registry.py` | ok | — |
| `middlewares/__init__.py` | ok | — |
| `middlewares/allowlist.py` | ok | — |
| `middlewares/clarification.py` | ok | — |
| `middlewares/context_pruning.py` | medium | 标准 logging 非 get_logger；JIT 复杂 |
| `middlewares/dangling_tool_call.py` | ok | — |
| `middlewares/guardrail_provider.py` | ok | — |
| `middlewares/loop_detection.py` | low | 启发式需单测 |
| `middlewares/subagent_limit.py` | ok | — |
| `middlewares/tool_error_handling.py` | ok | 与 deerflow ~90% 重复 |
| `middlewares/tool_guardrail.py` | low | 静默 except |
| `middlewares/view_image.py` | ok | — |

### core/auth, config, guardrails, models, prompts, skills, storage, tools

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `auth/identity.py` | ok | — |
| `config/__init__.py` | ok | — |
| `config/app_config.py` | low | 全局单例 + mtime 热加载 |
| `config/extensions_config.py` | ok | — |
| `config/model_config.py` | ok | — |
| `config/paths.py` | low | 全局 `_paths` 可变单例 |
| `config/tracing_config.py` | low | 全局 tracing 单例 |
| `guardrails/__init__.py` | ok | — |
| `guardrails/content_filters.py` | low | 规则集大 |
| `guardrails/input_validators.py` | low | 同上 |
| `guardrails/middleware.py` | medium | 484 行；标准 logging |
| `guardrails/ocr_validators.py` | low | 500 行 |
| `guardrails/output_validators.py` | low | — |
| `guardrails/schemas.py` | ok | — |
| `models/__init__.py` | ok | — |
| `models/asr_config.py` | ok | — |
| `models/base_llm.py` | low | Provider 抽象厚 |
| `models/context_limits.py` | ok | — |
| `models/factory.py` | medium | except 回退链长；与 deerflow 重叠 |
| `models/model_cache.py` | low | 进程内缓存无 TTL 文档 |
| `models/patched_deepseek_openai.py` | low | 随 SDK 维护 |
| `prompts/__init__.py` | ok | — |
| `prompts/system_template.py` | low | except 读模板失败不透明 |
| `prompts/vision.py` | ok | — |
| `skills/*` (7 文件) | ok~low | 与 deerflow skills 48–68% 重复 |
| `storage/scoped_storage.py` | low | except 吞存储错误 |
| `thread_key.py` | ok | 输入校验好 |
| `tools/builtin_tools.py` | ok | — |
| `tools/minio_tools.py` | low | 与 storage 重叠 |
| `tools/task_tool.py` | medium | ~449 行；except；子 agent 复杂 |
| `tools/ocr/*` (4 文件) | low~medium | 同步 IO + 宽泛 except |
| `tracing.py` | ok | 与 deerflow ~83% 重复 |
| `utils/reflection.py` | ok | deerflow 侧仍有 PEP695 版本 |

---

## db/（21 文件）

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `__init__.py` | ok | — |
| `connection.py` | medium | except 关闭资源；NullPool 需运维文档 |
| `redis.py` | low | 每次 `get_redis()` 新建客户端 |
| `models/__init__.py` | medium | 单文件聚合全部 ORM |
| `models/agent.py` | ok | — |
| `models/dataset.py` | ok | — |
| `models/knowledge_base.py` | low | 关系复杂 |
| `models/llm_model.py` | ok | — |
| `crud/*` (8 文件) | ok~low | 结构清晰；directory_crud 偏大 |
| `init_db.py` | **high** | `<dev_api_key>`；admin 无密码；`USE {db}` 依赖配置可信 |
| `init_datasets.py` | low | except 种子失败难排查 |
| `init_agent_bindings.py` | ok | — |
| `init_knowledge_base.py` | ok | — |

---

## services/（6）、storage/（1）、integrations/（3）、kafka/（7）、worker/（9）、workflows/（4）

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `services/__init__.py` | ok | — |
| `services/chat_file_index.py` | medium | ~519 行；缺 BM25 单测 |
| `services/chat_file_parser.py` | medium | 多处 except；解析分支多 |
| `services/directory_service.py` | low | ~572 行业务集中 |
| `services/startup_task_processor.py` | low | 与 HTTP lifespan 耦合 |
| `services/user_file_service.py` | medium | 大量 raw SQL |
| `storage/__init__.py` | **high** | **~1000 行单体**；`_get_minio_client` 被当公共 API；多处 pass |
| `integrations/__init__.py` | ok | — |
| `integrations/llm.py` | low | httpx 生命周期不清晰 |
| `integrations/vlm.py` | low | 与 llm 结构重复 |
| `kafka/__init__.py` | ok | — |
| `kafka/exceptions.py` | ok | — |
| `kafka/log_schemas.py` | ok | — |
| `kafka/schemas.py` | ok | Pydantic 完整 |
| `kafka/client.py` | low | 生命周期与 worker 对齐 |
| `kafka/admin.py` | low | 缺自动化测试 |
| `kafka/consumer.py` | medium | ~773 行；默认 handler 占位 sleep |
| `worker/task_worker.py` | ok | — |
| `worker/ingest_worker.py` | low | `while True` 无优雅退出文档 |
| `worker/components/*` (5) | ok~low | ingest 依赖 storage 私有函数 |
| `workflows/base/*` (4) | ok | LangGraph 封装清晰 |

---

## 本模块 P0 项

1. `auth_sms.py` — `SMS_SECRET` 迁环境变量；验证码改 Redis+TTL  
2. `routers/rag.py` — `retrieve_internal` fail-closed  
3. `init_db.py` — 禁止生产默认 dev key  
4. `storage/__init__.py` — 拆分并公开稳定 MinIO API  
