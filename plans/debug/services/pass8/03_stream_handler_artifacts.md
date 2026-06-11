# 第八遍：`NEAgentStreamHandler` 与产物/文件下载链

**文件**：`backend/custom/agents/streaming/stream_handler.py`（~987 行）  
**依赖**：`artifact_utils` → `user_file_service.register_minio_*`

---

## 1. 与会话上传文件的区别

| 类型 | 来源 | 登记方式 | 用户下载 |
|------|------|----------|----------|
| 会话上传 | chat_files / my_files | UserFile + SessionFile | `/api/v2/files/{id}/download` |
| Agent 工件 | 报告/PPT/图表工具输出 | `build_registered_artifact_metadata` | 同上（登记后） |
| 虚拟 live markdown | 流式写作过程 | `virtual_artifact: true` | 可能无独立 file_id |

审查 `backend/services` 时，**工件登记**走 `user_file_service`，不走 `ChatFileParser`。

---

## 2. 关键流程

```text
┌───────────────────────────────┐ ┌───────────────────────────────┐ ┌───────────────────────────────┐ ┌───────────────────────────────┐ ┌───────────────────────────────┐
│generate_html_ppt / report_tool│ │     NEAgentStreamHandler      │ │        artifact_utils         │ │       user_file_service       │ │       files.py download       │
└───────────────────────────────┘ └───────────────────────────────┘ └───────────────────────────────┘ └───────────────────────────────┘ └───────────────────────────────┘


```

---

## 3. 问题清单

| ID | 级别 | 描述 |
|----|------|------|
| SH-01 | P2 | `sent_artifacts` 内存去重，进程内有效，多 worker 不共享 |
| SH-02 | P2 | MinIO 登记失败时用户仍可能看到 artifact 事件但 404（pass7） |
| SH-03 | P2 | `build_live_markdown_artifact` 虚拟工件与真实 `user_files` 行不一致 |
| SH-04 | P3 | handler 与 `stream_utils` 中 artifact_path 提取逻辑重复 |
| SH-05 | P3 | 深研模式专用分支多，单测成本高 |

---

## 4. 与 services 审查的交叉引用

| services 问题 | 在 handler 链上的体现 |
|---------------|----------------------|
| minio 缺 size_bytes | 登记后 download 无 Content-Length 优化 |
| local_abs_path 泄露 | 列表 API 问题，非 handler |
| export-docx 无鉴权 | 独立路由，不经 handler |

---

## 5. SSE 与文件内容

Handler **不**解析上传文档正文；文档检索在：

- Lightweight：内联 search（pass7）  
- Full：chat_file_search 工具  

与 `sse_events_module.md` 事件类型 `artifact` / `process` 正交。

---

*第八遍 — stream_handler*
