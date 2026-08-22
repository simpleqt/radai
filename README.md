# RADAI 智能体平台

RADAI 是从原单体仓库拆分出来的**独立智能体平台**:智能体运行时、会话、技能、
题库/学情/课程评价,以及全部前端应用。**知识库能力已拆分至独立的
[omnigraph](https://github.com/simpleqt/omnigraph) 知识服务**(自研入库 + 自研检索),
本仓库不再包含 RAG/知识库实现,全部通过 HTTP 访问:

- 知识库管理/上传/文档/关键词/目录/入库管理:经 `backend/api/routers/knowledge_proxy.py`
  反向代理到 `KNOWLEDGE_SERVICE_BASE_URL`(默认 `http://127.0.0.1:8086`)
- 检索工具(`multi_kb_search`)、MCP 文本检索、图片检索、会话暂存清理:
  经 `X-Internal-Secret` 保护的 omnigraph 内部接口
  (`backend/application/knowledge/internal_client.py`)

## 部署前提

1. 部署并启动 omnigraph 知识服务(见其仓库 README)
2. 本服务与 omnigraph 配置**相同的非空** `INTERNAL_API_SECRET`
3. `.env` 中设置 `KNOWLEDGE_SERVICE_BASE_URL=http://127.0.0.1:8086`
4. 共享同一 MySQL/Redis/MinIO 实例

## 快速开始

```bash
cp .env.example .env      # 填写连接信息与 INTERNAL_API_SECRET
make install
make init-db
make run-api              # 智能体 API (8082)
make run-guest-api        # 访客 API (8083)
```

> 原 `run-worker` / `run-ingest-worker` 已随知识库管线迁移至 omnigraph。

## 结构

```
backend/    智能体平台服务(agent runtime/会话/技能/题库/MCP/前端 API)
frontend*/  主站与各院系定制前端(csh/fmca/landslide/audit)
guest/      访客前端
deploy/     docker-test 编排与发布脚本
docs/       设计文档(docs/split_agent_rag_services.md 描述两服务拆分部署)
```

## 开发

```bash
make check-imports   # 校验内部导入闭包(拆分完整性门禁)
make test            # pytest(智能体侧测试)
```

## 来源

由 RADAI 单体仓库拆分而来(2026-08-22),知识库侧见
[omnigraph](https://github.com/simpleqt/omnigraph)。
