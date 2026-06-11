# MCP 服务访问指南

> Model Context Protocol 接入说明。  
> **最后同步**：2026-06-08（`http_server.py` 挂载 `/mcp/sse` + `api/routers/mcp.py`）  
> SSE 事件格式与标准 AgentEvent 一致 → [agent_platform_standardization_plan.md](./agent_platform_standardization_plan.md) §11

---

## 概述

MCP 服务基于标准 `AgentService.run_stream()` 与边缘路由适配，提供：

- 双知识库并行检索（RRF 融合）
- 联网搜索（可选）
- 多轮上下文管理
- 与前端 V2 一致的 SSE 事件流

---

## 访问方式

### 方式 1：主 API 挂载 SSE（推荐）

主 API 启动后自动挂载，无需额外进程。

| 项 | 值 |
| :--- | :--- |
| SSE 端点 | `http://<api_host>:<api_port>/mcp/sse` |
| 适用 | Claude Desktop、Cursor 等 MCP 客户端 |

**Claude Desktop 配置示例：**

```json
{
  "mcpServers": {
    "nuclear-electronics": {
      "type": "sse",
      "url": "http://<api_host>:<api_port>/mcp/sse"
    }
  }
}
```

生产环境将 `localhost:8082` 替换为实际 API 地址。

### 方式 2：REST API 流式查询

| 项 | 值 |
| :--- | :--- |
| 端点 | `POST /api/v1/mcp/query` |
| 认证 | `Authorization: Bearer <token>` 或 `X-API-Key: <key>`（`verify_api_key`，与全站 API Key 一致） |

**请求示例：**

```bash
curl -N -X POST http://<api_host>:<api_port>/api/v1/mcp/query \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "什么是核探测器？"}'
```

**响应事件：**

| type | 说明 |
| :--- | :--- |
| `message.delta` | 文本片段 |
| `workflow.step` | 状态更新 / 工作流事件 |
| `reasoning.delta` | 推理过程（如启用） |
| `error` | 错误 |

### 方式 3：独立 MCP 进程（可选）

```bash
venv314/bin/python -m backend.api.mcp_server
# 默认监听 :8083/sse
```

> **端口冲突**：独立 MCP 默认端口 **8083** 与访客 API (`guest_http_server`) 相同。二者不可同时占用 8083；生产环境建议仅使用主 API 的 `/mcp/sse`（方式 1）。

---

## 配置查询

```bash
curl http://<api_host>:<api_port>/api/v1/mcp/config \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

## 可用工具

| 工具 | 说明 |
| :--- | :--- |
| `query_knowledge_base` | 核电子学知识库检索问答 |

参数：`query` (string) — 用户问题

---

## 健康检查

```bash
# API 文档
curl http://<api_host>:<api_port>/docs

# MCP 流式测试
curl -N -X POST http://<api_host>:<api_port>/api/v1/mcp/query \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "测试"}'
```

**日志：**

```bash
tail -f backend/logs/api.log
tail -f backend/logs/app.error.log
```

---

## 故障排查

| 现象 | 排查 |
| :--- | :--- |
| 无法连接 | 确认 `make run-api` 已启动；检查 8082 端口 |
| 401/403 | 验证 API Key 或 Token |
| 无检索结果 | 检查知识库入库状态、Milvus/Nebula 连接 |
| 8083 冲突 | 停止 guest API 或 MCP 独立进程之一 |

---

## 相关文档

| 文档 | 说明 |
| :--- | :--- |
| [agent_platform_standardization_plan.md](./agent_platform_standardization_plan.md) | SSE 事件协议 |
| [agent_engine_module.md](./docs_backend/agent_engine_module.md) | Agent 引擎 |
| [agent_platform_standardization_plan.md](./agent_platform_standardization_plan.md) | 平台规范化方案 |

---

## 更新记录

| 日期 | 变更 |
| :--- | :--- |
| 2026-04-23 | REST 流式响应与 V2 事件格式统一 |
| 2026-04-22 | 升级为双库检索实现 |
| 2026-05-27 | 文档改用 localhost 占位符；补充端口冲突说明 |
| 2026-05-31 | 认证说明对齐 `dependencies/auth.py`；同步日期 |
| 2026-06-08 | MCP 执行链路同步为 AgentService + 标准事件协议 |

---

© 2026 核电子学智能助手项目组
