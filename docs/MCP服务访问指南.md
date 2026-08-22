# MCP 服务访问指南

> Model Context Protocol 接入说明。
> **最后同步**：2026-07-22（MCP 专用 Key 工具白名单 + 固定知识库纯文本检索）
> REST 查询接口输出标准 AgentEvent SSE；FastMCP SSE 端点使用 MCP SSE 协议，详见 [agent_development_guide.md](./agent_development_guide.md)。

---

## 概述

MCP 服务基于标准 `AgentService.run_stream()` 与边缘路由适配，提供：

- 双知识库并行检索（RRF 融合）
- 联网搜索（可选）
- 多轮上下文管理
- 按 Agent Profile / workflow / selected skills 展开的工具目录
- REST 端与前端 V2 一致的 SSE 事件流；MCP 端提供通用问答工具和固定知识库纯文本检索工具

---

## 访问方式

### 方式 1：主 API 挂载 SSE（推荐）

主 API 启动后自动挂载，无需额外进程。

| 项 | 值 |
| :--- | :--- |
| SSE 端点 | `http://localhost:8082/mcp/sse`（FastMCP，同时挂载 `/mcp/sse/sse` 与 `/mcp/sse/messages/`） |
| 认证 | 仅接受 API Key：`Authorization: Bearer <api_key>` 或 `X-API-Key: <api_key>`；不接受匿名访问和普通登录 Token |
| 适用 | Claude Desktop、Cursor 等 MCP 客户端 |

### MCP 专用 API Key

管理员可在“系统管理 → API Key → 创建新 Key”中将访问范围设为“仅 MCP 服务”。
此类 Key 只允许访问以下路径族：

- `/mcp/sse`（包括 `/mcp/sse/messages/`）；
- `/api/v1/mcp`（配置、目录、直接工具调用和查询接口）。

携带 MCP 专用 Key 访问其他接口时统一返回 HTTP 403 和
`api_key_scope_forbidden`。已有 Key 和新建的“全部 API”Key 保持原有权限。
部署该功能前需执行：

```bash
venv314/bin/python scripts/migrations/add_api_key_access_scope.py
venv314/bin/python scripts/migrations/add_api_key_mcp_allowed_tools.py
```

`mcp_allowed_tools` 为 `NULL` 时保持原有工具权限；设为工具名数组时，
`tools/list` 只展示白名单内工具，`tools/call` 也会再次强制校验。空数组表示
禁止调用任何 MCP 工具。

**Claude Desktop 配置示例：**

```json
{
  "mcpServers": {
    "nuclear-electronics": {
      "type": "sse",
      "url": "http://localhost:8082/mcp/sse",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

生产环境将 `localhost:8082` 替换为实际 API 地址。
客户端若使用逐行请求头格式，应填写
`Authorization=Bearer YOUR_API_KEY`。SSE 建连请求和后续消息 POST 请求都必须携带同一 API Key。

默认 MCP 服务配置来自 `backend/config/mcp/registry.yaml` 的 `services.default`：当前绑定 `agent_code=ne`、`workflow=full`、`user_id=mcp`，并默认开启 `use_search`。如需把 MCP 服务切换到 CSH 或 groundwater，应优先改该配置，而不是在客户端侧伪造工具参数。

### 方式 2：REST API 流式查询

| 项 | 值 |
| :--- | :--- |
| 端点 | `POST /api/v1/mcp/query` |
| 认证 | `Authorization: Bearer <token>` 或 `X-API-Key: <key>`（`verify_api_key`，与全站 API Key 一致） |

**请求示例：**

```bash
curl -N -X POST http://localhost:8082/api/v1/mcp/query \
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

该 REST 接口由 `backend/api/routers/mcp.py` 适配 `AgentService.run_stream()`，因此事件格式面向平台前端和通用 HTTP 客户端；Claude Desktop、Cursor 等 MCP 客户端应使用方式 1 的 FastMCP SSE 端点。

### 方式 3：独立 MCP 进程（可选）

```bash
venv314/bin/python -m backend.api.mcp_server
# 默认监听 :8083/sse
```

> **端口冲突**：独立 MCP 默认端口 **8083** 与访客 API (`guest_http_server`) 相同。二者不可同时占用 8083；生产环境建议仅使用主 API 的 `/mcp/sse`（方式 1）。

---

## 配置与工具目录查询

```bash
curl http://localhost:8082/api/v1/mcp/config \
  -H "Authorization: Bearer YOUR_API_KEY"
```

`/api/v1/mcp/config` 返回 REST 流式查询地址、传输类型和认证头，供外部客户端生成连接配置。

```bash
curl "http://localhost:8082/api/v1/mcp/tools?agent_code=ne&workflow=full&selected_skills=html-ppt,academic-search" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

`/api/v1/mcp/tools` 会调用 `backend/application/mcp/tool_catalog.py`，按 `agent_code`、`policy_name`、`domain`、`workflow` 和 `selected_skills` 展开 `ToolPolicy` 可见工具，返回 MCP-like 工具描述、输入 schema 和治理注解。未指定 `agent_code` 时必须显式传 `domain`。

---

## 可用工具

| 工具 | 说明 |
| :--- | :--- |
| `query_knowledge_base` | 核电子学知识库检索问答 |
| `list_available_tools` | 列出当前 MCP 服务绑定 Agent 的 policy 工具目录 |
| `query_nuclear_electronics_text` | 固定检索“核电子学期刊知识库”，直接返回原始文字片段，不生成答案且不返回图片内容 |

- `query_knowledge_base` 参数：
  - `query` (string，必填) — 用户问题。
  - `knowledge_bases` (string 或 string[]，可选) — 按知识库名称选择当前
    Agent 已绑定的知识库；不传、传 `null` 或传空数组时查询全部可访问绑定库。
  - `thread_id` (string，可选) — 连续对话使用的 MCP 会话 ID。
- `list_available_tools` 参数：`workflow` (string，可选)、`selected_skills` (array，可选)。
- `query_nuclear_electronics_text` 参数：`query` (string，必填)、`limit`
  (integer，1–20，默认 10)。该工具固定使用知识库
  `ca8f3763-f956-492f-a373-e2dfc3e89d60`，忽略检索管线产生的提示词和图片集合，
  并过滤图片块、图片标记、图片链接及 base64 图片数据。

**按名称选择知识库：**

```json
{
  "query": "SiPM 前置放大器设计需要关注哪些指标？",
  "knowledge_bases": "核电子学期刊"
}
```

也可以传多个名称：

```json
{
  "query": "核电子学前端电路有哪些关键模块？",
  "knowledge_bases": ["核电子学知识库", "核电子学期刊"]
}
```

名称必须精确匹配当前 Agent 的有效绑定。未绑定、已停用、重名或无权访问的
知识库不会被查询；其中未绑定、不可用或重名会直接返回参数错误，权限仍由
Agent Runtime 的知识库访问策略校验。

---

## 健康检查

```bash
# API 文档
curl http://localhost:8082/docs

# MCP 流式测试
curl -N -X POST http://localhost:8082/api/v1/mcp/query \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "测试"}'

# MCP SSE 建连测试（连接保持打开属于正常现象）
curl -N http://localhost:8082/mcp/sse/ \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Accept: text/event-stream"
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
| MCP 返回 401 | 确认使用有效 API Key；MCP 不接受普通登录 Token，且 GET/POST 都必须携带 Key |
| REST 返回 401/403 | 验证 API Key 或登录 Token |
| 无检索结果 | 检查知识库入库状态、Milvus/Nebula 连接 |
| 8083 冲突 | 停止 guest API 或 MCP 独立进程之一 |

---

## 相关文档

| 文档 | 说明 |
| :--- | :--- |
| [agent_development_guide.md](./agent_development_guide.md) | AgentEvent、Profile、Workflow 与工具装配指南 |
| [agent_engine_module.md](./docs_backend/agent_engine_module.md) | Agent 引擎 |
| [coordinated_agent_workflows.md](./coordinated_agent_workflows.md) | Plan/Solve、多智能体并行与交替工作流扩展规范 |

---

## 更新记录

| 日期 | 变更 |
| :--- | :--- |
| 2026-04-23 | REST 流式响应与 V2 事件格式统一 |
| 2026-04-22 | 升级为双库检索实现 |
| 2026-05-27 | 文档改用 localhost 占位符；补充端口冲突说明 |
| 2026-05-31 | 认证说明对齐 `dependencies/auth.py`；同步日期 |
| 2026-06-08 | MCP 执行链路同步为 AgentService + 标准事件协议 |
| 2026-07-09 | 补充 `/api/v1/mcp/tools` 与 FastMCP `list_available_tools` |
| 2026-07-22 | 新增 Key 级工具白名单及核电子学期刊知识库纯文本检索工具 |

---

© 2026 核电子学智能助手项目组
