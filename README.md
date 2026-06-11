# 核电子学智能助手 (RadAI)

RAG + 多智能体平台，面向核电子学领域知识问答、文档管理与深度研究；并扩展支持成理水文地质智学助手 (CSH-AI) 与访客演示模式。

**完整文档** → [`docs/README.md`](docs/README.md)

---

## 产品线

| 产品 | 前端 | 端口 | 后端 API |
| :--- | :--- | :--- | :--- |
| RadAI 核电子学智能助手 | `frontend/` | 5173 | 主 API `:8082` → `/api/v2/agent` |
| CSH-AI 水文地质智学助手 | `frontend_csh_ai/` | 5174 | 主 API `:8082` → `/api/v2/csh_agent` |
| Guest 访客演示 | `guest/` | 8085 | 访客 API `:8083` |

---

## 技术栈

Python 3.14 · FastAPI · LangGraph · LightRAG · Vue 3 · Vite 8 · Tailwind CSS 4

基础设施：MySQL · Milvus · NebulaGraph · MinIO · Redis · Kafka · SQLite (Checkpointer)

---

## 快速开始

```bash
# 1. 安装依赖
make install

# 2. 配置环境变量（复制既有 backend/.env 或参考 plans/task_06_deployment_config.md）
# 编辑 LLM、MySQL、Milvus、MinIO、Redis 等

# 3. 初始化数据库
make init-db

# 4. 启动后端
make run-api              # http://localhost:8082/docs

# 5. 启动前端
bash run_frontends.sh     # RadAI:5173 + CSH-AI:5174
```

访客模式：

```bash
make run-guest-api        # 访客 API :8083
cd guest && npm run dev   # 访客前端 :8085
```

---

## 常用命令

```bash
make help                 # 查看所有命令
make test                 # 运行测试
make run-worker           # Kafka 任务 Worker
make run-ingest-worker    # 文档入库 Worker
make lint                 # 代码检查
```

---

## 项目结构

```text
backend/          # API、Agent、RAG、DB、Worker、Skills
frontend/         # RadAI 前端
frontend_csh_ai/  # CSH-AI 前端
guest/            # 访客前端
docs/             # 项目文档
eval/             # RAG 评测框架
```

---

## 文档导航

| 文档 | 说明 |
| :--- | :--- |
| [docs/README.md](docs/README.md) | 文档总索引 |
| [docs/developer_guide.md](docs/developer_guide.md) | 开发者指南 |
| [docs/api_quick_reference.md](docs/api_quick_reference.md) | API 速查 |
| [docs/architecture_overview.md](docs/architecture_overview.md) | 系统架构总览 |
| [docs/知识库算法和架构.md](docs/知识库算法和架构.md) | RAG 检索算法、Profile、评测结论（生产主文档） |
| [docs/文档代码对照清单.md](docs/文档代码对照清单.md) | 文档↔代码维护索引 |
| [docs/docs_backend/README.md](docs/docs_backend/README.md) | 后端模块文档 |
| [docs/docs_frontend/README.md](docs/docs_frontend/README.md) | 前端模块文档 |
| [docs/docs_backend/deployment_runbook.md](docs/docs_backend/deployment_runbook.md) | 部署运维手册 |
| [docs/下一步开发需求.md](docs/下一步开发需求.md) | 路线图与待办 |
| [plans/debug/README.md](plans/debug/README.md) | 只读代码审查与文档差距追踪（不修代码） |

---

© 2026 核电子学智能助手项目组
