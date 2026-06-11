# 逐文件审查：rag / custom

> 只读审查：**103** 个 `.py`（rag 52 + custom 51）

---

## rag/ 根目录

| 文件 | 行≈ | 严重度 | 问题摘要 | 标签 |
|------|-----|--------|----------|------|
| `__init__.py` | 小 | ok | 导出 RagService | — |
| `standalone_query.py` | **0** | **high** | **空文件**，死代码/未完成 | dead-code |
| `service.py` | 3354 | **high** | God file；**`_patch_lightrag_redis_connection_pool_loop_local` 重复定义**（844/917 行）；LightRAG/Milvus/Redis 补丁+查询+嵌入全家桶 | god-file, monkey-patch |
| `retrieval_profile.py` | 中 | low | 硬编码 KB UUID，与 kb_config 重复 | hardcoded-config |
| `document_processor.py` | 中 | medium | `sys.path.insert`；同步 session；访问 `_rag` 私有字段 | encapsulation |

---

## rag/content_filter/

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `__init__.py` | ok | — |
| `models.py` | ok | dataclass 清晰 |
| `manager.py` | low | 补私有 `_embedding_func` |
| `per_kb_filter.py` | medium | ~625 行编排复杂 |
| `ppr_filter.py` | medium | **1252 行 God file**，PPR+线程池 |
| `reranker.py` | low | 同步 httpx 瓶颈 |
| `mmr_reranker.py` | ok | — |

---

## rag/keywords/

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `__init__.py` | ok | — |
| `models.py` | ok | — |
| `bm25_index.py` | medium | 自研分词精度有限；**pickle.loads** | security |
| `extractor.py` | medium | ~951 行近 God file；ThreadPoolExecutor |
| `index_manager.py` | low | 单例多 loop 注意 |
| `per_kb_index.py` | medium | ~868 行；Redis/Milvus/Nebula 混合 |

---

## rag/knowledge_base/

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `__init__.py` | ok | — |
| `manager.py` | **high** | **1675 行 God file**；KB CRUD+RAG 缓存+索引+过滤；`rag: Any` |

---

## rag/raganything/（15 文件）

| 文件 | 行≈ | 严重度 | 问题摘要 |
|------|-----|--------|----------|
| `__init__.py` | 小 | ok | vendor fork 元数据 |
| `base.py` | 小 | ok | DocStatus |
| `batch.py` | 中 | low | Mixin 初始化顺序 |
| `batch_parser.py` | 405 | low | CLI print |
| `chunking.py` | 243 | ok | — |
| `config.py` | 320 | low | env 默认值分散 |
| `enhanced_markdown.py` | 中 | low | optional WeasyPrint |
| `md_ingestor.py` | 610 | medium | 入库管线需集成测试 |
| `modalprocessors.py` | 1978 | **high** | God file；重复 process_* 模板 |
| `parser.py` | 2089 | **high** | God file；print 调试；fork 维护成本高 |
| `processor.py` | 2187 | **high** | God file；Mixin 难导航 |
| `prompt.py` | 369 | ok | — |
| `query.py` | 1135 | medium | God file；吞 except |
| `raganything.py` | 中 | medium | 多 Mixin 初始化链 |
| `retry_utils.py` | 小 | ok | tenacity |
| `utils.py` | 423 | low | 可拆分 |

---

## rag/retriever/（7 文件）

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `__init__.py` | ok | — |
| `config.py` | low | 策略矩阵大 |
| `chunk_searcher.py` | ok | Path-C 单一 |
| `direct_retriever.py` | low | 四路并行 ~376 行 |
| `entity_recall.py` | low | Path-A/B |
| `fusion.py` | ok | RRF 纯函数 |
| `graph_expander.py` | medium | 直连 Nebula pool，绕开 storage 抽象 |

---

## rag/storages/、sync/、utils/

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `storages/__init__.py` | ok | — |
| `storages/nebula_storage.py` | medium | **1107 行**；模块级 ThreadPoolExecutor 无关闭钩子 |
| `sync/__init__.py` | ok | — |
| `sync/rag_index_sync_manager.py` | medium | 单例多 worker 风险 |
| `sync/rag_index_sync_scheduler.py` | medium | 单例；类型注解不全 |
| `utils/chunk_query_fallback.py` | low | monkey-patch |
| `utils/entity_name_normalizer.py` | ok | 纯函数 |
| `utils/lightrag_chunk_patch.py` | medium | 486 行；私有 API |
| `utils/lightrag_no_truncation_patch.py` | medium | 升级易碎 |
| `utils/lightrag_token_truncation_patch.py` | medium | 与 no_truncation 互斥需文档 |

---

## custom/ 全目录

### custom/agents/

| 文件 | 行≈ | 严重度 | 问题摘要 |
|------|-----|--------|----------|
| `__init__.py` | 15 | ok | 导出 |
| `router.py` | 中 | ok | 路由清晰 |
| `csh_router.py` | 中 | low | 与 router 镜像 |
| `ne_full_feature_agent.py` | 1040 | medium | God file；硬编码 KB_IDS；import 时设 DEER_FLOW_CONFIG_PATH |
| `csh_full_feature_agent.py` | 965 | medium | 与 NE 重复；复用 NEAgentStreamHandler 命名误导 |
| `lightweight_qa_agent.py` | 1349 | **high** | God file；大量 except；type: ignore 循环导入 |
| `deep_research_orchestrator_agent.py` | 59 | ok | 薄包装 |
| `research_draft_agent.py` | 177 | ok | — |
| `support/*` (8) | 小~200 | ok~low | tool_assembly 吞 skill 加载错误 |
| `streaming/stream_handler.py` | 987 | medium | God file；NE* 命名被 CSH 复用 |
| `streaming/stream_utils.py` | 507 | low | think-tag 与 agent/runner 重复 |
| `streaming/stream_timeouts.py` | 34 | ok | — |
| `streaming/image_url_registry.py` | 155 | low | 并发写入需确认 |

### custom/api/

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `guest_conversations.py` | low | N+1 checkpoint 查询 |
| `v2/agent.py` | **high** | **1354 行**；与 csh_agent 镜像 |
| `v2/csh_agent.py` | **high** | **1243 行**；双份维护 |
| `v2/explore.py` | medium | noqa BLE001 过宽 |
| `v2/guest.py` | low | 与主站重叠 |

### custom/tools/

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `request_limits.py` | ok | ContextVar 合理 |
| `chat_file_search_tool.py` | ok | — |
| `async_web_search_tool.py` | low | 外部 API 超时文档 |
| `acdemic_search_tool.py` | medium | **拼写 acdemic**；973 行；`__main__` print |
| `multi_kb_search_tool.py` | **high** | **1223 行**；硬编码 KB；`_get_rag_instance` |
| `deep_research_report_tool.py` | low | MinIO/DB 耦合 |
| `excel_engine.py` | medium | DuckDB 缓存无 LRU 上限 |
| `excel_chart_generator.py` | low | matplotlib 重 |
| `excel_data_analysis_tool.py` | medium | SQL 注入式 prompt 风险 |
| `html_ppt_tool.py` | medium | ~949 行；同步 MinIO |

### custom/services、tasks、config、utils、workflow

| 文件 | 严重度 | 问题摘要 |
|------|--------|----------|
| `services/explore_learning.py` | medium | ~676 行；BLE001 |
| `services/follow_up_suggestions.py` | low | logging 不一致 |
| `tasks/manager.py` | medium | DB 轮询 SSE 延迟 |
| `tasks/runner.py` | medium | think-tag 三处重复 |
| `config/kb_config.py` | low | UUID 与 retrieval_profile 重复 |
| `utils/checkpointer.py` | low | 返回类型注解可能不准 |
| `workflow/research_state.py` | ok | 状态机清晰 |

---

## God file 清单（rag+custom，>1000 行）

| 路径 | 行≈ |
|------|-----|
| `rag/service.py` | 3354 |
| `rag/raganything/processor.py` | 2187 |
| `rag/raganything/parser.py` | 2089 |
| `rag/raganything/modalprocessors.py` | 1978 |
| `rag/knowledge_base/manager.py` | 1675 |
| `custom/api/v2/agent.py` | 1354 |
| `custom/agents/lightweight_qa_agent.py` | 1349 |
| `rag/content_filter/ppr_filter.py` | 1252 |
| `custom/api/v2/csh_agent.py` | 1243 |
| `custom/tools/multi_kb_search_tool.py` | 1223 |
| `rag/raganything/query.py` | 1135 |
| `rag/storages/nebula_storage.py` | 1107 |
| `custom/agents/ne_full_feature_agent.py` | 1040 |

---

## 本模块优先动作

1. 删除或实现 `standalone_query.py`  
2. 删除 `service.py` 重复 Redis patch 函数  
3. 抽取 `api/v2/_agent_sse_common.py` 合并 NE/CSH  
4. 单一 `kb_registry.yaml` 替代分散 UUID  
