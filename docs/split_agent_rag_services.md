# 智能体平台(radai)与知识服务(omnigraph)拆分部署

原单体仓库已拆分为两个独立项目:

- **radai**(本仓库):智能体平台。端口 `8082`(主 API)/`8083`(访客 API)。
  承载 `/api/v2/agent-runtime`、会话、技能、题库、MCP 与全部前端。
- **omnigraph**(独立仓库):知识服务。端口 `8086`,仅监听内网。
  只保留自研入库(self ingestion)与自研检索(self retrieval);
  **LightRAG/legacy 链路已全部移除**,存量 `legacy_lightrag` 知识库一律阻塞。

## 调用契约

radai 的知识库访问全部走 HTTP:

1. **管理面代理**:`knowledge_proxy.py` 将
   `/api/v1/{knowledge_bases,kb_documents,uploads,documents,keywords,directories,ingestion,admin/self-ingestion,health/self-ingestion,rag,prompts/templates}/**`
   流式转发到 `KNOWLEDGE_SERVICE_BASE_URL`(默认 `http://127.0.0.1:8086`),
   原样透传用户凭据。
2. **内部检索面**:`multi_kb_search` 工具、MCP 文本检索、图片检索工具、会话暂存清理
   调用 omnigraph 的 `X-Internal-Secret` 保护接口
   (`/api/v1/rag/retrieve_internal`、`/internal/multi-kb-search`、
   `/internal/image-search`、`/internal/staging/delete-prefix`)。

两个服务必须设置**相同且非空**的 `INTERNAL_API_SECRET`。例如:

```dotenv
INTERNAL_API_SECRET=replace-with-a-long-random-secret
KNOWLEDGE_SERVICE_BASE_URL=http://127.0.0.1:8086
```

## 启动顺序

```bash
# 1) 先启动 omnigraph(见其仓库)
cd /path/to/omnigraph && make run-api && make run-ingest-worker

# 2) 再启动 radai
cd /path/to/radai
scripts/run_agent_api.sh       # 或 make run-api
make run-guest-api
```

omnigraph 进程启动慢是预期行为;它应长期运行。radai 的 Python 文件变更由
`--reload` 自动重启,且不会影响 omnigraph 的预热缓存。

## 已移除的单体能力(在 radai 侧)

- `backend/rag/**`、`backend/worker/**`、`backend/kafka/**`、知识库路由与 schema
- legacy 文档抽取 Kafka 管线(document_processing/task_worker/outbox)
- LightRAG 全部依赖(lightrag-hku/pymilvus/nebula3/faiss/hanlp 已从 requirements 移除)

轻量文本工具(分词/BM25/媒体块/图片预处理)已 vendor 到
`backend/core/text`、`backend/core/vision`,无需知识服务依赖。

## 行为变化

- 存量 `legacy_lightrag` 知识库:不可检索/入库,任务阻塞并提示下线
- `/api/v1/prompts/templates/**` 与 `/api/v1/admin/tasks`(legacy 任务管理)经代理转发;
  LightRAG 写接口(insert/insert_docx/reset/delete_doc/process_multimodal)已删除
- 管理后台的系统健康不再探测 Kafka/Nebula(归属 omnigraph)
