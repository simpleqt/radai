# 运行时依赖版本矩阵

## 统一基线

API、Worker 和本地测试统一使用项目根目录的 `venv314`：

```bash
./venv314/bin/python -m pip install -r requirements.txt
./venv314/bin/python -m backend.config.dependency_versions
```

项目支持 Python 3.12、3.13、3.14；当前验收环境为 Python 3.14.2。

| 类别 | 包 | 固定版本 | 作用 |
|---|---|---:|---|
| Web | `fastapi` | 0.135.3 | API 框架 |
| Web | `starlette` | 0.52.1 | ASGI/路由运行时 |
| Web | `uvicorn` | 0.44.0 | API/Worker 进程入口 |
| Web | `prometheus-fastapi-instrumentator` | 7.1.0 | 指标中间件 |
| LangChain | `langchain` | 1.3.12 | Agent 高层编排 |
| LangChain | `langchain-core` | 1.4.9 | 消息、Runnable、工具协议 |
| LangChain | `langchain-openai` | 1.3.4 | OpenAI 模型适配 |
| LangChain | `langchain-deepseek` | 1.0.1 | DeepSeek 模型适配 |
| LangChain | `langchain-community` | 0.4.1 | 社区工具与集成 |
| LangChain | `langchain-text-splitters` | 1.1.1 | 文本切分 |
| LangGraph | `langgraph` | 1.2.8 | 状态图与流式执行 |
| LangGraph | `langgraph-checkpoint` | 4.1.1 | Checkpoint 协议 |
| LangGraph | `langgraph-checkpoint-sqlite` | 3.1.0 | 本地 Checkpoint |
| LangGraph | `langgraph-checkpoint-redis` | 0.4.0 | 共享 Checkpoint |
| LangGraph | `langgraph-prebuilt` | 1.1.0 | 预置 Agent 节点 |
| LangGraph | `langgraph-sdk` | 0.4.2 | LangGraph SDK |
| MCP | `langchain-mcp-adapters` | 0.2.2 | MCP 工具适配 |
| 数据引擎 | `duckdb` | 1.5.3 | Excel/表格查询工具 |

## 运行时校验

`backend.config.dependency_versions` 是启动校验的机器可读基线。API 和两个
Worker 在开始接受任务前会校验 Python 版本及上表关键包；不匹配时直接失败，
避免旧 LangGraph 与新代码混用造成静默流式协议变化。

`requirements-langchain-rollback-2026-07-09.txt` 仅作为历史回滚记录，不得用于
启动当前服务。
