# 第四遍：集成层审查（路由 / Worker / Agent）

> `backend/services` 本身无 HTTP 入口；缺陷常在**调用方**。本文档审查与 services 紧耦合的集成代码（**不修改源码**）。

---

## 1. 集成拓扑

```text
┌───────────────────────────────────────────────────────────────┐
│                              前端                               │
│ useFileUpload  │  MyFilesView / Picker                        │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                            API 路由                             │
│ chat_files.py  │  my_files.py  │  files.py  │  uploads.py     │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                           services                            │
│ ChatFileParser  │  ChatFileIndexManager  │  user_file_service │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                            Worker                             │
│ ingest_worker                                                 │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                             Agent                             │
│ lightweight_qa_agent  │  chat_file_search_tool                │
└───────────────────────────────────────────────────────────────┘

连接:
  useFileUpload ──────► chat_files.py
  MyFilesView / Picker ──────► my_files.py
  chat_files.py ──────► ChatFileParser
  chat_files.py ──────► ChatFileIndexManager
  my_files.py ──────► ChatFileParser
  my_files.py ──────► ChatFileIndexManager
  files.py ──────► user_file_service
  uploads.py ──(status 0→1)──► ingest_worker
  ingest_worker ──────► PendingIngestProcessor
  StartupTaskProcessor ──(可选)──► PendingIngestProcessor
  lightweight_qa_agent ──────► ChatFileIndexManager
  chat_file_search_tool ──────► ChatFileIndexManager

```

---

## 2. `chat_files.py` 审查

| 项 | 结论 | 级别 |
|----|------|------|
| 上传即 `SessionFile` + `UserFile` | ✓ 符合方案场景 A | — |
| `long_doc` 上传时 `add_document` | ✓ | — |
| 索引失败仅 `logger.error`，仍 commit | 用户以为已索引，实际无 | **P1** |
| `get_files_by_thread` 用 JOIN `session_files` | ✓ 正确模式 | — |
| `list_chat_files` 同 JOIN | ✓ | — |
| DELETE 解绑 + `remove_document` | ✓ 优于 my_files | — |
| `get_file_meta` 同步 session 工厂 | 在 async 栈外可用，注意连接池 | P2 |
| 失败上传仍生成随机 `file_id` 在错误项 | 无 DB 污染 | — |
| 多模态 base64 写入 metadata | 体积风险 | P1（跨 parser） |

**建议**：索引构建与 DB commit 同一事务语义（失败则 `parse_status=failed` 或回滚 metadata 中 strategy）。

---

## 3. `my_files.py` 审查

| 项 | 结论 | 级别 |
|----|------|------|
| 上传不入 `SessionFile` | 符合「仅个人库」 | — |
| 上传不建索引 | 符合 attach 时建索引设计 | — |
| attach 时 `get_or_create` + `add_document` | ✓ | — |
| attach 未更新 `last_used_at` | 与方案 7.2 不符 | **P1** |
| 软删除 `session_id=""` | 脏数据 | **P0** |
| 软删除不清理索引 | Agent 仍可搜 | **P0** |
| `list_my_files` total=`len(scalars().all())` | 全表加载，性能差 | **P2** |
| `file_type` 过滤用 `display_name.like` | 未用 mime，易漏 | P2 |
| 上传路径 `chat_files/{user_id}/...` | 与个人库命名混用 chat_files 前缀 | P3 |

**与 `chat_files` 双轨差异**（产品需知）：

| 能力 | chat_files | my_files |
|------|------------|----------|
| 上传即绑会话 | ✓ | ✗（需 attach） |
| 上传即索引 long_doc | ✓ | ✗ |
| 删除行为 | 仅解绑 session | 软删 user_file + 污染 session_files |

---

## 4. `files.py`（文件中心 v2）审查

| 项 | 结论 |
|----|------|
| 使用 `user_file_service` 鉴权 | ✓ |
| `list_files(thread_id)` 依赖 `user_files.thread_id` | 个人库文件可能列不出（**P1**，见 pass3/04） |
| MinIO 下载同步 generator | 可能阻塞 event loop（**P2**，路由层） |
| 审计依赖 `get_async_session` 自动 commit | ✓ |

---

## 5. 入库 Worker 链审查

| 组件 | 行为 |
|------|------|
| `uploads.py` | 上传 status=0；`POST /ingest` → status=1 |
| `ingest_worker.py` | 轮询 status=1，调 `PendingIngestProcessor` |
| `StartupTaskProcessor` | 可选，同样扫 status=1 |
| Redis 锁 | 三端相同 key `kb_ingest_lock:{file_id}` |

**P0**：三端互斥部署（见 fix backlog P0-01）。

**差异**：`PendingIngestProcessor` 支持 MD 直传；`StartupTaskProcessor` 无。

---

## 6. Agent 集成审查

### `lightweight_qa_agent.py`

| 项 | 级别 |
|----|------|
| 内联 `_do_file_search` + 可能注入 `chat_file_search` 工具 | P2 重复检索 |
| `file_idx._chunks` 私有访问 | P1 |
| `_is_generic_summary_query` 私有 import | P1 |
| 通用问句硬编码英文 QUERY | 产品可接受 |

### `chat_file_search_tool.py`

| 项 | 级别 |
|----|------|
| `get(thread_id)` 无则提示无索引 | ✓ |
| `top_k` clamp 1–10 | ✓ |
| `is_kw_match` 用 score>=0.5 启发式 | P3 |
| 二次 `clean_document_images` | 冗余但无害 |

### `artifact_utils.py`

| 项 | 级别 |
|----|------|
| `register_minio_artifact_for_thread_sync` | ✓ 合理 |
| 登记失败降级，可能 file_id 仍为 path | P2 前端下载 404 |

---

## 7. `directories.py` / `knowledge_base_documents.py`

| 项 | 级别 |
|----|------|
| 直接使用 `directory_service.crud` | P2 封装穿透 |
| 树接口依赖 `get_directory_tree` 计数 | P1 计数不准时 UI 错 |

---

## 8. 集成层新增测试建议

1. **E2E**：my_files 上传 long_doc → attach → Agent search 命中。  
2. **E2E**：软删除后 search 无结果。  
3. **E2E**：chat_files 索引失败时 strategy 不为 indexed。  
4. **并发**：双 attach 同 file_id 同 session，块数不翻倍（幂等）。  
5. **ingest**：双 worker 单 file_id 单次入库。

---

## 9. 第四遍结论

Services 层多数问题在集成层**被放大或暴露**。修复时应 **services + 路由 + worker 同 PR**，避免只改 `chat_file_index` 而 `my_files` 仍不清理索引。

详见：[设计方案差距对照](../design_vs_implementation_gap.md)
