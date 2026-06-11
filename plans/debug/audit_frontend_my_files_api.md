# 前端审计：个人文件夹 vs `my_files` API

> 组件：`MyFilesView.vue`、`MyFilesPickerDialog.vue`、`index.vue`（attach）、`useFileUpload.ts`  
> 后端：`api/routers/my_files.py`  
> 只读审查。

---

## 1. API 使用矩阵

| 前端位置 | 方法 | 路径 | 后端校验 |
|----------|------|------|----------|
| MyFilesView 列表 | GET | `/api/v1/my-files?...` | owner ✅ |
| MyFilesView 上传 | POST | `/api/v1/files/upload` | owner ✅ |
| MyFilesView 重命名 | PUT | `/api/v1/my-files/{id}` | owner ✅ |
| MyFilesView 删除 | DELETE | `/api/v1/my-files/{id}` | owner ✅ |
| Picker 列表 | GET | 同上 | owner ✅ |
| 发送前 attach | POST | `/api/v1/sessions/{threadId}/files/attach` | file owner ✅，**会话未校验** |
| 对话直传 | POST | `/api/v2/chat-files/upload` | `useFileUpload` |
| 下载 | GET | `download_url` + `?token=` | ⚠️ 见 §3 |

---

## 2. 与设计方案对齐

| 方案能力 | 前端 | 缺口 |
|----------|------|------|
| 文件管理页 | `MyFilesView` + 路由 `/my-files` | ✅ |
| 从库中选文件进对话 | `MyFilesPickerDialog` → `addReadyAttachment` | ✅ |
| 发送时 attach | `index.vue` L171–187 | **FE-MF-1** 未检查 `res.ok` |
| 上传默认进个人库 | `/api/v1/files/upload` | ✅ |
| 生成文件归档 | 无 UI 调 `generated-files/save` | ❌ 仅后端骨架 |
| 标签/高级搜索 | 仅 keyword + file_type + source | ⚠️ 与后端 count 过滤 bug 叠加 |

---

## 3. 下载方式（FE-MF-2）

```typescript
const url = `${file.download_url}${token ? `?token=${token}` : ''}`;
window.open(url, '_blank');
```

- `download_url` 为 `/api/v2/files/{id}/download`  
- 依赖 query `token` — 与 Bearer 头并存，需确认 `files.py` 是否接受 query token（auth 模块 `get_current_user` 支持 `?token=`）  
- **风险**：token 进浏览器历史/Referer；建议统一 Authorization 或短期 signed URL

---

## 4. `useFileUpload` 与双路径

| 路径 | 行为 |
|------|------|
| 本地选文件 | `chat-files/upload`，写入 `thread_id` + SessionFile |
| 个人库 `addReadyAttachment` | 仅前端 `ready` 状态，`uf_*` id |
| 发送时 | attach API + `fileIds` 传给 agent |

**FE-MF-3（medium）**：attach 失败仅 `console.error`，用户仍可发送，Agent 可能缺文件上下文。

**FE-MF-4（low）**：库文件预览 URL 用 download 链接，大 PDF 可能误触发下载而非预览。

---

## 5. 过滤参数一致性

前端 `sourceOptions`：

- `minio` → 主动上传  
- `local` → 系统生成  

后端 `my_files`：`source` 映射 `UserFile.source_type`。  
chat 上传为 `source_type="minio"`；`register_*_artifact` 为 `local` — **语义基本对齐**。

`file_type` 后端 count 未过滤 — 分页 total 可能偏大（见 [deep_dive_my_files_user_file_service.md](./deep_dive_my_files_user_file_service.md) MF-4）。

---

## 6. CSH / Guest

- `frontend_csh_ai` 未在本次逐文件 diff；主站 `ne-full` 路径已覆盖  
- Guest 模式通常无个人文件夹路由 — 符合产品

---

## 7. 建议

1. attach 失败时 toast 并阻止发送或重试。  
2. 下载改用 `fetch` + blob + Authorization 头。  
3. Picker 分页与 MyFilesView 共用 composable `useMyFiles.ts` 减少重复。  
4. E2E：选库文件 → attach → 多轮对话仍带 file_ids。

---

## 8. 与后端 P0 联动

Agent 仍通过 `chat_files.get_file_meta` 注入 — 即使前端 attach 正确，**越权 file_id** 问题见 CF-1（与前端无关，后端必修）。
