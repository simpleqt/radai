# 部署指南

> **文档类型**：🔧 运维手册  
> **补充**：访客站点部署见 [guest_frontend_部署与更新说明.md](./guest_frontend_部署与更新说明.md)；快速命令见根目录 `Makefile`。  
> **RAG 检索环境变量与排障**（`USE_DIRECT_RETRIEVER`、Profile、multi_kb）→ [docs/docs_backend/deployment_runbook.md](../docs/docs_backend/deployment_runbook.md) §7–8 · [docs/知识库算法和架构.md](../docs/知识库算法和架构.md)

本文档描述 Backend 服务及其集成 RAG 模块所需的部署架构和环境配置。

## 1. 部署架构

整个系统采用模块化部署，核心 Backend 服务运行在本地或容器中，依赖服务（LLM, MinerU）作为外部服务接入。

### 服务清单

| 服务名称 | 类型 | 地址/端口 | 描述 |
| :--- | :--- | :--- | :--- |
| **Backend Service** | Application | `0.0.0.0:8082` | 主 API：RAG、Agent、知识库、MCP |
| **Guest API** | Application | `0.0.0.0:8083` | 访客专用精简 API（见 `guest_http_server.py`） |
| **MySQL** | Database | `<minio_server_ip>:3306` | 存储元数据、用户数据、任务状态 |
| **MinIO** | Object Storage | `<minio_server_ip>:<minio_port>` | 存储原始文档、解析后的图片和中间文件 |
| **MinerU API** | External Service | `202.115.138.31:8009` | 提供 PDF/文档解析服务 (OCR, Layout Analysis) |
| **MiniMax Service** | LLM Service | `202.115.141.46:8080` | 提供大语言模型推理 (主要 LLM) |
| **VLM Service** | External Service | `202.115.138.31:8011` | 提供视觉模型服务 (Qwen3-VL-8B) |
| **Kafka** | Message Queue | `<kafka_server_ip>:9092` | 异步任务消息队列 |
| **Milvus** | Vector DB | `MILVUS_URI`（如 `:19530`） | 实体 `keyword_index`、chunk 向量 |
| **NebulaGraph** | Graph DB | `NEBULA_HOST`:`NEBULA_GRAPH_PORT`（默认 9669） | 每 KB 独立 Space，Path-D 图扩展 |
| **Redis** | Cache/KV | `REDIS_HOST`:`REDIS_PORT` | LightRAG KV、BM25、Token |

### 架构图

```text
┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ end            │  │ 用户             │  │ Back           │
└────────────────┘  └────────────────┘  └────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ Nginx                                │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ Backend                              │
└──────────────────────────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
         (HTTP)
           │
           ▼
         (HTTP)
           │
           ▼
         (HTTP)
           │
           ▼
┌────────────────┐  ┌─────────────────┐  ┌────────────────┐  ┌────────────────────┐  ┌─────────────────────┐  ┌────────────────┐  ┌─────────────────────┐  ┌────────────────┐  ┌─────────────────────┐
│ (Kafka :9092)  │  │ (Milvus :19530) │  │ (MinIO :<minio_port>)  │  │ MinerU API (:8009) │  │ MiniMax LLM (:8080) │  │ (MySQL :3306)  │  │ (NebulaGraph :9669) │  │ (Redis :6379)  │  │ VLM Service (:8011) │
└────────────────┘  └─────────────────┘  └────────────────┘  └────────────────────┘  └─────────────────────┘  └────────────────┘  └─────────────────────┘  └────────────────┘  └─────────────────────┘

```

## 2. 环境配置

### 2.1 依赖安装
请参考 `backend/README.md` 安装 Python 依赖。整合 RAG 后，需要额外安装 LightRAG 和相关库：

```bash
pip install -r requirements.txt
```

### 2.2 环境变量配置 (.env)

在 `.env` 文件中配置以下关键参数：

```ini
# ==================== 基础配置 ====================
ENV=production
LOG_LEVEL=INFO
SERVER_PORT=8082

# ==================== 数据库与存储 ====================
MYSQL_HOST=<minio_server_ip>
MYSQL_PORT=3306
MYSQL_USER=doc_agent
MYSQL_DATABASE=document_processor

MINIO_ENDPOINT=http://<minio_server_ip>:<minio_port>
MINIO_BUCKET_NAME=documents

KAFKA_BOOTSTRAP_SERVERS=<kafka_server_ip>:9092

# ==================== RAG / 检索（生产） ====================
RAG_WORK_DIR=/data/rag_storage
USE_DIRECT_RETRIEVER=true
# RAG_RETRIEVAL_PROFILE=domain
# RAG_KB_RETRIEVAL_PROFILE_MAP={}

# Milvus（settings: milvus_uri / milvus_user / milvus_password）
MILVUS_URI=http://localhost:19530

# NebulaGraph（生产图库，非 Neo4j）
NEBULA_HOST=<minio_server_ip>
NEBULA_GRAPH_PORT=9669
NEBULA_USER=root
NEBULA_PASSWORD=<勿提交仓库>

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# ==================== 外部 AI 服务 ====================
# 见 backend/.env.example 或团队密钥管理；模型亦可在 MySQL llm_models + /api/v1/models 配置
LLM_PROVIDER=minimax
MINERU_API_URL=http://<mineru-host>:8009
```

> ⚠️ 历史版本曾写 Neo4j / `GRAPH_STORAGE_TYPE=Neo4JStorage`，**现行代码为 NebulaGraph**（`backend/rag/storages/nebula_storage.py`）。检索 Profile 与评测变量见 [deployment_runbook](../docs/docs_backend/deployment_runbook.md) §7。

## 3. 部署步骤 (Docker Compose)

为了快速启动本地依赖服务（Redis, Milvus），可以使用以下 `docker-compose.yml`：

```yaml
version: '3.5'

services:
  etcd:
    container_name: milvus-etcd
    image: quay.io/coreos/etcd:v3.5.0
    environment:
      - ETCD_AUTO_COMPACTION_MODE=revision
      - ETCD_AUTO_COMPACTION_RETENTION=1000
      - ETCD_QUOTA_BACKEND_BYTES=4294967296
      - ETCD_SNAPSHOT_COUNT=50000
    volumes:
      - ${DOCKER_VOLUME_DIRECTORY:-.}/volumes/etcd:/etcd
    command: etcd -advertise-client-urls=http://<etcd_host>:2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd

  minio:
    container_name: milvus-minio
    image: minio/minio:RELEASE.2023-03-20T20-16-18Z
    environment:
      MINIO_ACCESS_KEY: ${MINIO_ACCESS_KEY}
      MINIO_SECRET_KEY: ${MINIO_SECRET_KEY}
    ports:
      - "9001:9001"
      - "9000:9000"
    volumes:
      - ${DOCKER_VOLUME_DIRECTORY:-.}/volumes/minio:/minio_data
    command: minio server /minio_data --console-address ":9001"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 30s
      timeout: 20s
      retries: 3

  standalone:
    container_name: milvus-standalone
    image: milvusdb/milvus:v2.3.1
    command: ["milvus", "run", "standalone"]
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000
    volumes:
      - ${DOCKER_VOLUME_DIRECTORY:-.}/volumes/milvus:/var/lib/milvus
      - ${DOCKER_VOLUME_DIRECTORY:-.}/volumes/milvus/configs/user.yaml:/milvus/configs/user.yaml
    ports:
      - "19530:19530"
      - "9091:9091"
    depends_on:
      - "etcd"
      - "minio"

  redis:
    image: redis:7.0
    container_name: rag-redis
    ports:
      - "6379:6379"
    volumes:
      - ./volumes/redis:/data
    command: redis-server --requirepass redis_password_123

  # NebulaGraph 通常部署在独立集群，不在此 compose 中；本地调试可参考 Nebula 官方 compose
```

启动命令：

```bash
docker-compose up -d
# 或项目根目录：make run-api / make run-all
```

## 4. 验证部署

1. 检查 Docker 容器（Milvus、Redis）为 Up；NebulaGraph 远程连通。
2. `curl http://<api_host>:<api_port>/health` — 主 API。
3. `curl http://<guest_api_host>:<guest_port>/health` — 访客 API（若启用）。
4. 上传 PDF 或 `.md`，确认 `PendingFile` 入库完成；Milvus / Nebula / Redis 有数据。
5. `POST /api/v1/rag/retrieve` 或 Paper KB 评测 smoke（见 deployment_runbook §7）。
