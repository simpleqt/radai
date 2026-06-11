# Backend 逐文件扫描索引（自动）

**文件总数**: 487  |  **生成方式**: 静态正则扫描

**严重度**: `high` 安全/超大文件 | `medium` 复杂度/裸except/TODO | `low` 轻微 | `ok` 无明显信号

**列**: 行数 | Exc=except Exception | 裸except | print | TODO | pickle | asyncio.run | global | UUID | dev-key


## `deerflow/` (148 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `sandbox/tools.py` | 1345 | **high** | 19 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/builtins/task_tool.py` | 1149 | **medium** | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `client.py` | 953 | **low** | 1 | 0 | 6 | 0 | 0 | 2 | 0 | 0 | 0 |
| `subagents/executor.py` | 751 | **low** | 3 | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 |
| `community/aio_sandbox/aio_sandbox_provider.py` | 638 | **low** | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/lead_agent/prompt.py` | 549 | **low** | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/memory/updater.py` | 469 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/infoquest/infoquest_client.py` | 404 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/openai_codex_provider.py` | 396 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/app_config.py` | 381 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| `agents/memory/prompt.py` | 363 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/factory.py` | 360 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `community/aio_sandbox/local_backend.py` | 355 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/lead_agent/agent.py` | 348 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/claude_provider.py` | 348 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sandbox/local/local_sandbox.py` | 348 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/paths.py` | 341 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `utils/file_conversion.py` | 309 | **low** | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/uploads_middleware.py` | 289 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/llm_error_handling_middleware.py` | 286 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/vllm_provider.py` | 258 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `runtime/runs/worker.py` | 258 | **medium** | 3 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 |
| `config/extensions_config.py` | 256 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| `tools/builtins/invoke_acp_agent_tool.py` | 256 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/patched_minimax.py` | 250 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/memory_middleware.py` | 248 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/loop_detection_middleware.py` | 244 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/aio_sandbox/aio_sandbox.py` | 232 | **low** | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/view_image_middleware.py` | 225 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/memory/queue.py` | 219 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| `models/credential_loader.py` | 219 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `runtime/runs/manager.py` | 210 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sandbox/search.py` | 210 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/sandbox_audit_middleware.py` | 204 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `uploads/manager.py` | 202 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/memory/storage.py` | 200 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `models/factory.py` | 196 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/builtins/tool_search.py` | 193 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/checkpointer/provider.py` | 191 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| `agents/middlewares/clarification_middleware.py` | 191 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `runtime/store/provider.py` | 188 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| `skills/installer.py` | 183 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/tool_error_handling_middleware.py` | 159 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/aio_sandbox/remote_backend.py` | 156 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `mcp/oauth.py` | 150 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/tracing_config.py` | 149 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `utils/network.py` | 139 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/title_middleware.py` | 138 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `mcp/cache.py` | 138 | **low** | 1 | 0 | 0 | 0 | 0 | 1 | 4 | 0 | 0 |
| `agents/middlewares/dangling_tool_call_middleware.py` | 135 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/image_search/tools.py` | 135 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `runtime/stream_bridge/memory.py` | 133 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/patched_openai.py` | 132 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/tools.py` | 132 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/agents_config.py` | 125 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sandbox/local/local_sandbox_provider.py` | 119 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `mcp/tools.py` | 113 | **low** | 2 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `runtime/store/async_provider.py` | 113 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `skills/loader.py` | 112 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/checkpointer/async_provider.py` | 105 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/subagents_config.py` | 102 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `sandbox/sandbox_provider.py` | 101 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| `agents/middlewares/todo_middleware.py` | 100 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/builtins/present_file_tool.py` | 100 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/thread_data_middleware.py` | 99 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/ddg_search/tools.py` | 99 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/aio_sandbox/backend.py` | 98 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `guardrails/middleware.py` | 98 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `skills/parser.py` | 97 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `reflection/resolvers.py` | 95 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `skills/validation.py` | 95 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/builtins/view_image_tool.py` | 95 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/infoquest/tools.py` | 93 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sandbox/sandbox.py` | 93 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `subagents/registry.py` | 89 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sandbox/middleware.py` | 88 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/sandbox_config.py` | 83 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `utils/readability.py` | 83 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/memory_config.py` | 82 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| `runtime/serialization.py` | 78 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `subagents/builtins/deep_research.py` | 76 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/subagent_limit_middleware.py` | 75 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/summarization_config.py` | 74 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| `community/firecrawl/tools.py` | 73 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `runtime/stream_bridge/base.py` | 72 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sandbox/exceptions.py` | 71 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `mcp/client.py` | 68 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/patched_deepseek.py` | 65 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/tavily/tools.py` | 62 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/builtins/setup_agent_tool.py` | 62 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/features.py` | 61 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/deferred_tool_filter_middleware.py` | 60 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/skills_config.py` | 59 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/memory/__init__.py` | 57 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `guardrails/provider.py` | 56 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/builtins/clarification_tool.py` | 56 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `skills/types.py` | 54 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tracing/factory.py` | 54 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/title_config.py` | 53 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| `agents/thread_state.py` | 52 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `runtime/stream_bridge/async_provider.py` | 52 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/acp_config.py` | 51 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `config/guardrails_config.py` | 48 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 0 | 0 |
| `subagents/builtins/general_purpose.py` | 48 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `subagents/builtins/bash_agent.py` | 47 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/checkpointer_config.py` | 46 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| `config/stream_bridge_config.py` | 46 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| `sandbox/local/list_dir.py` | 46 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sandbox/security.py` | 45 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/jina_ai/jina_client.py` | 43 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `community/aio_sandbox/sandbox_info.py` | 41 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `runtime/__init__.py` | 39 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/token_usage_middleware.py` | 37 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/model_config.py` | 37 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/tool_search_config.py` | 35 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| `runtime/store/__init__.py` | 31 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/jina_ai/tools.py` | 30 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `uploads/__init__.py` | 29 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/__init__.py` | 28 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `runtime/store/_sqlite_utils.py` | 28 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `subagents/config.py` | 28 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `guardrails/builtin.py` | 23 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sandbox/file_operation_lock.py` | 23 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `runtime/runs/schemas.py` | 21 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `runtime/stream_bridge/__init__.py` | 21 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/tool_config.py` | 20 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `subagents/builtins/__init__.py` | 18 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/__init__.py` | 17 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/aio_sandbox/__init__.py` | 15 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `runtime/runs/__init__.py` | 15 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `guardrails/__init__.py` | 14 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `mcp/__init__.py` | 14 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `skills/__init__.py` | 14 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/builtins/__init__.py` | 13 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `subagents/__init__.py` | 12 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/checkpointer/__init__.py` | 9 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sandbox/__init__.py` | 8 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/token_usage_config.py` | 7 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/lead_agent/__init__.py` | 3 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/ddg_search/__init__.py` | 3 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `community/image_search/__init__.py` | 3 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/__init__.py` | 3 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `reflection/__init__.py` | 3 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sandbox/local/__init__.py` | 3 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/__init__.py` | 3 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tracing/__init__.py` | 3 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/__init__.py` | 1 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `__init__.py` | 0 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `scripts/` (66 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `migrate_all_to_ivf_pq.py` | 445 | **low** | 3 | 0 | 55 | 0 | 0 | 0 | 0 | 0 | 0 |
| `migrate_all_to_ivf_pq_parallel.py` | 435 | **low** | 4 | 0 | 48 | 0 | 0 | 0 | 0 | 0 | 0 |
| `test_ivf_pq_index.py` | 426 | **low** | 3 | 0 | 72 | 0 | 0 | 0 | 0 | 0 | 0 |
| `clean_knowledge_base_by_filename.py` | 425 | **low** | 5 | 0 | 63 | 0 | 0 | 1 | 0 | 0 | 0 |
| `refactor_entity_names.py` | 416 | **low** | 4 | 0 | 15 | 0 | 0 | 1 | 0 | 0 | 0 |
| `migrate_test1_to_ivf_pq.py` | 395 | **low** | 2 | 0 | 53 | 0 | 0 | 0 | 0 | 0 | 0 |
| `clean_by_file_name.py` | 330 | **low** | 6 | 0 | 39 | 0 | 0 | 1 | 0 | 0 | 0 |
| `restore_chunks_from_milvus.py` | 327 | **low** | 7 | 0 | 17 | 0 | 0 | 1 | 0 | 0 | 0 |
| `sync_kb_to_mysql_with_minio.py` | 326 | **low** | 4 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `sync_minio_to_mysql.py` | 312 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sync_neo4j_to_nebula.py` | 311 | **low** | 0 | 0 | 22 | 0 | 0 | 0 | 0 | 0 | 0 |
| `build_entity_source_index.py` | 310 | **low** | 5 | 0 | 70 | 0 | 0 | 0 | 0 | 0 | 0 |
| `trace_chunk_recall.py` | 275 | **low** | 2 | 0 | 39 | 0 | 0 | 1 | 0 | 0 | 0 |
| `check_entity_source_id.py` | 274 | **low** | 1 | 0 | 58 | 0 | 0 | 1 | 0 | 0 | 0 |
| `migrate_to_ivf_pq_fast.py` | 260 | **low** | 2 | 0 | 40 | 0 | 0 | 0 | 0 | 0 | 0 |
| `openai_proxy.py` | 255 | **low** | 0 | 0 | 12 | 0 | 0 | 0 | 1 | 0 | 0 |
| `sync_knowledge_base_to_mysql.py` | 247 | **low** | 3 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `check_milvus_memory_usage.py` | 245 | **low** | 3 | 0 | 69 | 0 | 0 | 0 | 0 | 0 | 0 |
| `diagnose_entity_deep.py` | 223 | **low** | 1 | 0 | 40 | 0 | 0 | 1 | 0 | 0 | 0 |
| `clear_llm_cache.py` | 219 | **low** | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 |
| `diagnose_entity_conflicts.py` | 212 | **low** | 2 | 0 | 52 | 0 | 0 | 1 | 0 | 0 | 0 |
| `openai_https_proxy.py` | 211 | **low** | 0 | 0 | 11 | 0 | 0 | 0 | 1 | 0 | 0 |
| `sync_missing_entities.py` | 211 | **low** | 0 | 0 | 15 | 0 | 0 | 0 | 0 | 0 | 0 |
| `diagnose_chunk_recall.py` | 205 | **low** | 2 | 0 | 48 | 0 | 0 | 1 | 0 | 0 | 0 |
| `diagnose_entity_names.py` | 201 | **low** | 2 | 0 | 50 | 0 | 0 | 1 | 0 | 0 | 0 |
| `test_milvus_fallback_detailed.py` | 193 | **low** | 1 | 0 | 62 | 0 | 0 | 1 | 0 | 1 | 0 |
| `check_chunk_data_existence.py` | 189 | **low** | 3 | 0 | 40 | 0 | 0 | 1 | 0 | 1 | 0 |
| `diagnose_entity_source_correct.py` | 189 | **low** | 3 | 0 | 35 | 0 | 0 | 1 | 0 | 0 | 0 |
| `delete_doc_by_file_path.py` | 186 | **low** | 5 | 0 | 25 | 0 | 0 | 1 | 0 | 0 | 0 |
| `diagnose_entity_source_id.py` | 184 | **low** | 1 | 0 | 28 | 0 | 0 | 1 | 0 | 0 | 0 |
| `migrate_enhance_directories.py` | 183 | **low** | 3 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `nebula_schema.py` | 178 | **low** | 0 | 0 | 10 | 0 | 0 | 0 | 0 | 0 | 0 |
| `check_and_move_processed_files_v3.py` | 171 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `create_user_file_isolation_tables.py` | 167 | **low** | 2 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `check_chunks_vdb.py` | 164 | **low** | 4 | 0 | 50 | 0 | 0 | 1 | 0 | 1 | 0 |
| `test_milvus_fallback.py` | 164 | **low** | 1 | 0 | 52 | 0 | 0 | 1 | 0 | 1 | 0 |
| `check_milvus_schema.py` | 163 | **low** | 3 | 0 | 43 | 0 | 0 | 1 | 0 | 1 | 0 |
| `rebuild_all_ivf_pq.py` | 158 | **low** | 1 | 0 | 20 | 0 | 0 | 0 | 0 | 0 | 0 |
| `check_index_status.py` | 156 | **low** | 1 | 0 | 34 | 0 | 0 | 0 | 0 | 0 | 0 |
| `test_no_truncation.py` | 155 | **low** | 1 | 0 | 52 | 0 | 0 | 1 | 0 | 1 | 0 |
| `verify_rag_status.py` | 148 | **low** | 5 | 0 | 21 | 0 | 0 | 0 | 0 | 0 | 0 |
| `apply_lightrag_retry_patch.py` | 145 | **low** | 2 | 0 | 10 | 0 | 0 | 0 | 0 | 0 | 0 |
| `test_token_truncation.py` | 143 | **low** | 1 | 0 | 46 | 0 | 0 | 1 | 0 | 1 | 0 |
| `test_actual_retrieval.py` | 140 | **low** | 1 | 0 | 36 | 0 | 0 | 1 | 0 | 1 | 0 |
| `build_keyword_index.py` | 121 | **low** | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `create_kb_directories_table.py` | 112 | **low** | 2 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `migrate_document_date_columns.py` | 107 | **low** | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 |
| `test_index_syntax.py` | 106 | **low** | 1 | 0 | 20 | 0 | 0 | 0 | 0 | 0 | 0 |
| `migrate_models_to_db.py` | 100 | **low** | 2 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 |
| `check_milvus_dynamic_fields.py` | 99 | **low** | 2 | 0 | 18 | 0 | 0 | 1 | 0 | 1 | 0 |
| `cleanup_discarded_by_name.py` | 95 | **low** | 1 | 0 | 10 | 0 | 0 | 1 | 0 | 0 | 0 |
| `migrate_add_category_path.py` | 93 | **low** | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `clear_parse_cache.py` | 87 | **low** | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `clear_query_cache_only.py` | 87 | **low** | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `cleanup_discarded.py` | 86 | **low** | 1 | 0 | 8 | 0 | 0 | 1 | 0 | 0 | 0 |
| `fix_entity_name.py` | 76 | **low** | 1 | 0 | 7 | 0 | 0 | 1 | 0 | 0 | 0 |
| `restore_test1_backup.py` | 65 | **low** | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| `init_kafka.py` | 57 | **low** | 2 | 0 | 14 | 0 | 0 | 1 | 0 | 0 | 0 |
| `cleanup_test1_backups.py` | 52 | **low** | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| `test_model_tool_calling.py` | 44 | **low** | 1 | 0 | 7 | 0 | 0 | 1 | 0 | 0 | 0 |
| `run_insert_docx.py` | 29 | **low** | 1 | 0 | 4 | 0 | 0 | 1 | 0 | 0 | 0 |
| `inspect_conversations.py` | 27 | **low** | 0 | 0 | 5 | 0 | 0 | 0 | 0 | 0 | 0 |
| `run_init_raganything.py` | 19 | **low** | 0 | 0 | 1 | 0 | 0 | 1 | 0 | 0 | 0 |
| `list_tables.py` | 16 | **low** | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| `list_gateway_models.py` | 14 | **low** | 1 | 0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| `__init__.py` | 0 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `core/` (61 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `guardrails/ocr_validators.py` | 500 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/factory.py` | 495 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `guardrails/middleware.py` | 484 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/task_tool.py` | 449 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/ocr/ocr_provider.py` | 439 | **low** | 4 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `guardrails/output_validators.py` | 415 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/base_llm.py` | 394 | **low** | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/ocr/mineru_ocr.py` | 392 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/ocr/base_ocr.py` | 390 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `guardrails/content_filters.py` | 387 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `guardrails/input_validators.py` | 387 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `guardrails/schemas.py` | 365 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/context_pruning.py` | 297 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/patched_deepseek_openai.py` | 246 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/factory.py` | 224 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `skills/scoped_skills_provider.py` | 202 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/app_config.py` | 197 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| `models/context_limits.py` | 169 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `prompts/system_template.py` | 164 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/loop_detection.py` | 162 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/paths.py` | 161 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `tools/minio_tools.py` | 149 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/tool_guardrail.py` | 146 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/tracing_config.py` | 134 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `agents/middlewares/clarification.py` | 117 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/view_image.py` | 109 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/extensions_config.py` | 107 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/dangling_tool_call.py` | 105 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/context/types.py` | 104 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/tool_error_handling.py` | 94 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/config_compat.py` | 92 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `storage/scoped_storage.py` | 88 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/state.py` | 78 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/model_cache.py` | 76 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 3 | 0 | 0 |
| `skills/parser.py` | 73 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/subagent_limit.py` | 64 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `skills/loader.py` | 64 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `guardrails/__init__.py` | 62 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `skills/native_skill_tools.py` | 60 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/features.py` | 55 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/allowlist.py` | 54 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/tool_registry.py` | 53 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/__init__.py` | 49 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `utils/reflection.py` | 48 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/asr_config.py` | 47 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tracing.py` | 43 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/guardrail_provider.py` | 42 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `skills/skill_runtime_state.py` | 42 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `thread_key.py` | 42 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/model_config.py` | 41 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/builtin_tools.py` | 40 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `skills/types.py` | 37 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `auth/identity.py` | 26 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `skills/__init__.py` | 19 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/ocr/__init__.py` | 19 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/__init__.py` | 16 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/thread_key.py` | 10 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `prompts/vision.py` | 6 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/__init__.py` | 5 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `prompts/__init__.py` | 5 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/middlewares/__init__.py` | 1 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `rag/` (53 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `service.py` | 3354 | **high** | 62 | 0 | 0 | 0 | 0 | 0 | 7 | 0 | 0 |
| `raganything/processor.py` | 2187 | **high** | 23 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/parser.py` | 2089 | **high** | 30 | 0 | 9 | 0 | 0 | 2 | 0 | 0 | 0 |
| `raganything/modalprocessors.py` | 1978 | **high** | 17 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `knowledge_base/manager.py` | 1675 | **high** | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 |
| `content_filter/ppr_filter.py` | 1252 | **medium** | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/query.py` | 1135 | **high** | 16 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `storages/nebula_storage.py` | 1107 | **high** | 28 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `keywords/extractor.py` | 951 | **low** | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `keywords/per_kb_index.py` | 868 | **high** | 16 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/raganything.py` | 674 | **medium** | 11 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `content_filter/per_kb_filter.py` | 625 | **low** | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/md_ingestor.py` | 610 | **medium** | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sync/rag_index_sync_manager.py` | 567 | **medium** | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/enhanced_markdown.py` | 534 | **low** | 4 | 0 | 6 | 0 | 0 | 0 | 0 | 0 | 0 |
| `content_filter/reranker.py` | 488 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `utils/lightrag_chunk_patch.py` | 486 | **low** | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/batch_parser.py` | 467 | **low** | 3 | 0 | 5 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/utils.py` | 423 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/batch.py` | 405 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `keywords/index_manager.py` | 391 | **low** | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `retriever/direct_retriever.py` | 376 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/prompt.py` | 369 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `retriever/entity_recall.py` | 358 | **low** | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/retry_utils.py` | 338 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `retriever/graph_expander.py` | 337 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `retriever/fusion.py` | 327 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/config.py` | 320 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `utils/lightrag_no_truncation_patch.py` | 311 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `document_processor.py` | 299 | **low** | 1 | 0 | 14 | 0 | 0 | 1 | 0 | 0 | 0 |
| `retriever/config.py` | 246 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/chunking.py` | 243 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `utils/lightrag_token_truncation_patch.py` | 226 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `keywords/bm25_index.py` | 223 | **high** | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| `content_filter/mmr_reranker.py` | 195 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `retriever/chunk_searcher.py` | 173 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `content_filter/manager.py` | 163 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `retrieval_profile.py` | 161 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 0 |
| `sync/rag_index_sync_scheduler.py` | 151 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `utils/chunk_query_fallback.py` | 122 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `content_filter/models.py` | 106 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `keywords/models.py` | 101 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `utils/entity_name_normalizer.py` | 45 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `retriever/__init__.py` | 36 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `content_filter/__init__.py` | 35 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `keywords/__init__.py` | 28 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sync/__init__.py` | 15 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/base.py` | 12 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `raganything/__init__.py` | 11 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `storages/__init__.py` | 11 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `knowledge_base/__init__.py` | 9 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `__init__.py` | 3 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `standalone_query.py` | 0 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `custom/` (52 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `api/v2/agent.py` | 1354 | **medium** | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/lightweight_qa_agent.py` | 1349 | **medium** | 13 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `api/v2/csh_agent.py` | 1243 | **medium** | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/multi_kb_search_tool.py` | 1223 | **medium** | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 |
| `agents/ne_full_feature_agent.py` | 1040 | **medium** | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 |
| `agents/streaming/stream_handler.py` | 987 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/acdemic_search_tool.py` | 973 | **low** | 2 | 0 | 12 | 0 | 0 | 3 | 0 | 0 | 0 |
| `agents/csh_full_feature_agent.py` | 965 | **low** | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |
| `tools/html_ppt_tool.py` | 949 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `services/explore_learning.py` | 676 | **low** | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/excel_data_analysis_tool.py` | 588 | **medium** | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/excel_engine.py` | 524 | **medium** | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/streaming/stream_utils.py` | 507 | **medium** | 11 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/excel_chart_generator.py` | 468 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tasks/manager.py` | 395 | **medium** | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `services/follow_up_suggestions.py` | 382 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `tools/deep_research_report_tool.py` | 366 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tasks/runner.py` | 356 | **low** | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `api/v2/explore.py` | 271 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/support/tool_assembly.py` | 249 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `api/v2/guest.py` | 204 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/support/artifact_utils.py` | 200 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/research_draft_agent.py` | 177 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/support/csh_prompts.py` | 176 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/support/prompts.py` | 176 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/support/explore_prompts.py` | 172 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/streaming/image_url_registry.py` | 155 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/async_web_search_tool.py` | 142 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/csh_router.py` | 117 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/router.py` | 113 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/chat_file_search_tool.py` | 101 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/request_limits.py` | 87 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `api/guest_conversations.py` | 73 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/support/follow_up_prompts.py` | 62 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/deep_research_orchestrator_agent.py` | 59 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/streaming/stream_timeouts.py` | 34 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/streaming/__init__.py` | 33 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/kb_config.py` | 33 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 0 |
| `workflow/research_state.py` | 33 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `api/v2/__init__.py` | 16 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/__init__.py` | 15 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `agents/support/__init__.py` | 14 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `utils/checkpointer.py` | 13 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `workflow/__init__.py` | 13 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tasks/__init__.py` | 4 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `__init__.py` | 1 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `api/__init__.py` | 1 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `config/__init__.py` | 1 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `middlewares/__init__.py` | 1 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `services/__init__.py` | 1 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tools/__init__.py` | 1 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `utils/__init__.py` | 0 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `api/` (34 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `routers/rag.py` | 1494 | **high** | 22 | 0 | 0 | 0 | 0 | 0 | 1 | 2 | 0 |
| `http_server.py` | 827 | **high** | 19 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/knowledge_base_documents.py` | 800 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/uploads.py` | 638 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/auth.py` | 591 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/datasets.py` | 526 | **medium** | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/directories.py` | 476 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/extraction.py` | 444 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/my_files.py` | 435 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/chat_files.py` | 412 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/keywords.py` | 378 | **low** | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/conversations.py` | 355 | **low** | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/document_ingestion.py` | 336 | **low** | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/document_processing/upload_routes.py` | 330 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/knowledge_base.py` | 312 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/document_processing/chunked_upload_routes.py` | 302 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `guest_http_server.py` | 293 | **low** | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/mcp.py` | 282 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |
| `routers/agents.py` | 270 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/files.py` | 261 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/document_processing/task_routes.py` | 212 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/auth_sms.py` | 199 | **low** | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| `dependencies/auth.py` | 193 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/document_processing/models.py` | 162 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `mcp_server.py` | 145 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/models.py` | 143 | **medium** | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 |
| `routers/chart_images.py` | 142 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/asr.py` | 92 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/document_processing/document_routes.py` | 80 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/artifacts.py` | 49 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/document_processing/__init__.py` | 32 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `dependencies/__init__.py` | 19 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `routers/__init__.py` | 9 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `__init__.py` | 5 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `db/` (21 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `crud/directory_crud.py` | 386 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/__init__.py` | 366 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `crud/knowledge_base_crud.py` | 361 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `crud/file_info_crud.py` | 279 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `init_datasets.py` | 260 | **low** | 4 | 0 | 4 | 0 | 0 | 1 | 0 | 0 | 0 |
| `models/knowledge_base.py` | 259 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `crud/task_crud.py` | 246 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/llm_model.py` | 220 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `init_db.py` | 206 | **high** | 2 | 0 | 26 | 0 | 0 | 0 | 0 | 0 | 6 |
| `crud/extraction_crud.py` | 195 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `crud/document_crud.py` | 169 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `connection.py` | 167 | **low** | 3 | 0 | 1 | 0 | 0 | 0 | 6 | 0 | 0 |
| `crud/agent_crud.py` | 158 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `crud/dataset_crud.py` | 146 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `init_agent_bindings.py` | 130 | **low** | 0 | 0 | 1 | 0 | 0 | 1 | 0 | 3 | 0 |
| `init_knowledge_base.py` | 116 | **low** | 0 | 0 | 9 | 0 | 0 | 1 | 0 | 0 | 0 |
| `models/agent.py` | 109 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `models/dataset.py` | 95 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `crud/__init__.py` | 27 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `redis.py` | 22 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `__init__.py` | 21 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `worker/` (9 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `components/task_consumer.py` | 269 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `components/ingest_pending_processor.py` | 189 | **low** | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `components/result_persister.py` | 138 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `components/result_publisher.py` | 105 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `components/doc_processing.py` | 72 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `ingest_worker.py` | 70 | **low** | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `task_worker.py` | 58 | **low** | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| `__init__.py` | 0 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `components/__init__.py` | 0 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `kafka/` (7 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `consumer.py` | 773 | **low** | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `admin.py` | 441 | **medium** | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `client.py` | 436 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `schemas.py` | 334 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `__init__.py` | 181 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `exceptions.py` | 87 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `log_schemas.py` | 59 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `config/` (6 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `settings.py` | 741 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `handlers.py` | 561 | **medium** | 8 | 0 | 2 | 0 | 0 | 0 | 1 | 0 | 0 |
| `filters.py` | 268 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `logging.py` | 264 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `loggers.py` | 148 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `__init__.py` | 3 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `services/` (6 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `user_file_service.py` | 674 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `directory_service.py` | 572 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `chat_file_index.py` | 519 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `chat_file_parser.py` | 399 | **low** | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `startup_task_processor.py` | 260 | **low** | 6 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `__init__.py` | 12 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `tests/` (6 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `test_bm25_issue.py` | 76 | **low** | 0 | 0 | 5 | 0 | 0 | 0 | 0 | 0 | 0 |
| `check_redis_bm25.py` | 60 | **low** | 1 | 0 | 11 | 0 | 0 | 0 | 0 | 0 | 0 |
| `test_surrogate_cleaning.py` | 47 | **low** | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| `test_live_index.py` | 41 | **low** | 0 | 0 | 5 | 0 | 0 | 0 | 0 | 0 | 0 |
| `check_metadata_chinese.py` | 34 | **low** | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| `test_permissions.py` | 22 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `agents/` (4 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `base/base_agent.py` | 459 | **low** | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `base/agent_factory.py` | 407 | **low** | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| `nuclear_electronics_agent.py` | 315 | **low** | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `base/__init__.py` | 10 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `skills/` (4 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `public/data-analysis/scripts/analyze.py` | 566 | **medium** | 9 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 |
| `public/github-deep-research/scripts/github_api.py` | 331 | **low** | 7 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| `public/image-generation/scripts/generate.py` | 132 | **low** | 2 | 0 | 5 | 0 | 0 | 0 | 0 | 0 | 0 |
| `public/video-generation/scripts/generate.py` | 116 | **low** | 1 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |

## `workflows/` (4 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `base/workflow.py` | 82 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `base/node.py` | 37 | **low** | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `base/state.py` | 22 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `base/__init__.py` | 5 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `integrations/` (3 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `llm.py` | 255 | **low** | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `vlm.py` | 253 | **low** | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `__init__.py` | 8 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `__init__.py/` (1 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `__init__.py` | 10 | **ok** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## `cli.py/` (1 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `cli.py` | 152 | **low** | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 |

## `storage/` (1 文件)

| 文件 | 行 | 严重度 | Exc | 裸 | prt | TODO | pkl | arun | glb | uuid | dev |
|------|-----|--------|-----|-----|-----|------|-----|------|-----|------|-----|
| `__init__.py` | 1000 | **medium** | 14 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
