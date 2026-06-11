# 跨模块重复与耦合矩阵

> 续审查：全库「复制粘贴边界」与私有 API 泄漏。

---

## 1. Think-tag / 推理流剥离（6+ 实现）

| 位置 | 函数名 | 输出 |
|------|--------|------|
| `custom/api/v2/agent.py` | `_split_thinking_segments` | visible + thinking + carry |
| `custom/api/v2/guest.py` | `_split_thinking_segments` | 同上 |
| `custom/tasks/runner.py` | `_split_thinking_segments` | 同上 |
| `api/http_server.py` | `_strip_thinking_segments` | visible + carry（无 thinking 通道） |
| `api/mcp_server.py` | `_strip_thinking_segments` | 同上 |
| `api/routers/mcp.py` | `_strip_thinking_segments` | 同上 |
| `custom/agents/lightweight_qa_agent.py` | `_strip_thinking_markup` | 单次剥离 |

**问题**：

- 语义不一致：`split` vs `strip`；SSE 文档要求 `thinking` 事件，但 agent.py 中 split **未接入**流循环（死代码）。
- 修复一处模型标签格式需改 6 处。

**建议**：`backend/core/streaming/think_tags.py` 单一实现 + 单元测试（跨 chunk 边界）。

---

## 2. MinIO 私有 API 引用（10 处）

`_get_minio_client` / `_normalize_minio_object_name` 定义于 `storage/__init__.py`，被以下模块直接 import：

| 模块 |
|------|
| `api/routers/files.py` |
| `api/routers/uploads.py` |
| `api/routers/chart_images.py` |
| `api/routers/knowledge_base_documents.py` |
| `core/storage/scoped_storage.py` |
| `services/startup_task_processor.py` |
| `worker/components/ingest_pending_processor.py` |
| `worker/components/result_persister.py` |
| `custom/services/explore_learning.py` |

**建议**：`storage/client.py` 公开 `get_minio_client()`；`storage/paths.py` 公开 `normalize_object_name()`。

---

## 3. 文件扩展名白名单（4 套）

| 文件 | 变量 | 扩展集 |
|------|------|--------|
| `api/routers/chat_files.py` | `ALLOWED_EXTENSIONS` | set |
| `api/routers/my_files.py` | `ALLOWED_EXTENSIONS` | set（与 chat 类似） |
| `api/routers/uploads.py` | `ALLOWED_EXTENSIONS` | dict（含 MIME） |
| `api/routers/datasets.py` | `_ALLOWED_EXTENSIONS` | xlsx/xls/csv only |

**风险**：新增类型（如 `.webp`）需改多处，易漏一致性问题。

**建议**：`backend/core/uploads/allowed_types.py` 单一来源。

---

## 4. 密码哈希（2 份）

- `api/routers/auth.py` → `hash_password` / `verify_password`
- `api/routers/auth_sms.py` → `hash_password`

---

## 5. core ↔ deerflow 重复（维护双轨）

| 能力 | core | deerflow | 相似度 |
|------|------|----------|--------|
| ToolErrorHandlingMiddleware | ✅ | ✅ | ~90% |
| model_config | ✅ | ✅ | ~90% |
| tracing factory | ✅ | ✅ | ~83% |
| features | ✅ | ✅ | ~82% |
| skills parser/types/loader | ✅ | ✅ | 48–68% |
| task_tool | ~449 行 | **1149 行** | 架构分叉 |
| guardrails middleware | 484 行完整 | 98 行薄封装 | core 更完整 |

**策略**：新功能只加 `core/`；deerflow 保留 re-export 兼容层，设删除时间表。

---

## 6. 智能体实现重复

| 对 | 相似内容 |
|----|----------|
| `ne_full_feature_agent` ↔ `csh_full_feature_agent` | KB_IDS、超时常量、stream 委托、ToolAssembly |
| `agent.py` ↔ `csh_agent.py` | 91.3% 行相似 |
| `router.py` ↔ `csh_router.py` | 路由分支结构 |
| `prompts.py` ↔ `csh_prompts.py` | 领域 prompt 差异 |

---

## 7. KB UUID 硬编码源（需统一 registry）

| 文件 |
|------|
| `custom/agents/ne_full_feature_agent.py` |
| `custom/agents/csh_full_feature_agent.py` |
| `custom/config/kb_config.py` |
| `rag/retrieval_profile.py` |
| `custom/tools/multi_kb_search_tool.py` |
| `api/routers/rag.py`（预热/默认） |
| `api/guest_http_server.py` |

---

## 8. 日志器不一致

| 模式 | 示例位置 |
|------|----------|
| `get_logger(__name__)` | 多数 custom/api |
| `logging.getLogger(__name__)` | rag/service patches、guardrails、follow_up_suggestions |
| `print` | scripts/*、部分 `__main__`、Kafka handler |

---

## 9. 依赖方向违规（应用层 → 路由层）

| 调用方 | 被调方 | 问题 |
|--------|--------|------|
| `custom/api/v2/agent.py` | `api.routers.chat_files.get_file_meta` | API v2 依赖 v1 路由函数 |
| `multi_kb_search_tool` | `rag_service._get_rag_instance` | 工具层访问私有方法 |

**建议**：下沉到 `services/chat_files.py`、`rag/facade.py`。
