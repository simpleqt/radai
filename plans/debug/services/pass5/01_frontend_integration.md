# 第五遍：前端与 services 集成审查

> 范围：`frontend/src/views/ne-full/` 与 `backend/services` 协作路径。  
> 不改代码，仅记录风险。

---

## 1. API 调用映射

| 前端 | 后端 | services |
|------|------|----------|
| `useFileUpload` → POST | `/api/v2/chat-files/upload` | Parser + Index |
| `index.vue` attach | `/api/v1/sessions/{id}/files/attach` | Index on attach |
| `MyFilesView` upload | `/api/v1/files/upload` | Parser only |
| `MyFilesPickerDialog` | GET `/api/v1/my-files` | — |
| 下载预览 | `/api/v2/files/{id}/download` | user_file_service 鉴权 |
| `agent-v2.ts` | chat-files CRUD | 同 upload |

---

## 2. 问题清单

### FE-P0-01 attach 失败静默

`index.vue` `handleSend`：

```typescript
await fetch(`/api/v1/sessions/.../attach`, { ... });
// 未检查 res.ok
```

attach 失败仍 `sendMessage`，`fileIds` 含 `uf_*` 但会话无索引 / 无 SessionFile → Agent 检索失败且无提示。

**级别**：P1（体验接近 P0）

---

### FE-P1-01 上传结果按文件名匹配

`useFileUpload.ts`：

```typescript
const serverMap: Record<string, any[]> = {};
// 按 file_name 匹配，同名文件顺序消费
```

多文件同名时 `file_id` 可能错配到本地 attachment。

**级别**：P1

---

### FE-P1-02 来源筛选标签与后端语义颠倒

`MyFilesPickerDialog.vue`：

```typescript
{ label: '主动上传', value: 'minio' },
{ label: '系统生成', value: 'local' },
```

后端：`my_files` 上传为 `source_type=minio`；`local` 多为**工件路径登记**，非「系统生成」。用户筛选结果误导。

**级别**：P1（产品/文档）

---

### FE-P1-03 个人库 long_doc 展示 strategy=indexed 但未 attach

`addReadyAttachment` 直接 `status: 'ready'`, `strategy: file.strategy`。

用户从库中选 long_doc 后 UI 显示「已索引」，实际索引在**发送时 attach** 才建立；首条消息前若 Agent 预读 fileIds 可能竞态（当前 send 顺序：先 attach 再 sendMessage → **OK**）。

**级别**：P2（UI 文案可改为「发送后建立索引」）

---

### FE-P2-01 多轮保留 ready 附件

发送后 `attachments = filter(status === 'ready')`，同 file_id 每轮发送会**再次 attach**（服务端 dup 返回成功）——无害但多余请求。

---

### FE-P2-02 图片预览 URL

库文件图片用 `/api/v2/files/{id}/download` 作 `previewUrl`，需带鉴权头；`<img src>` 可能 401（取决于全局 cookie/鉴权方式）。

---

### FE-P2-03 `ACCEPTED_TYPES` 与后端 ALLOWED 不完全一致

前端 MIME 列表与后端扩展名白名单两套逻辑，可能出现前端可选、后端 400。

---

## 3. 与设计方案场景对照

| 场景 | 前端 | 差距 |
|------|------|------|
| A 上传并入库+会话 | chat-files upload | ✓ |
| B 从我的文件添加 | Picker + attach on send | attach 无错误处理 |
| C 系统生成保存 | MyFiles save / artifact | 来源筛选文案错 |
| D 删除后再引用失效 | MyFiles DELETE | 后端索引未清（见 P0-06） |

---

## 4. 建议前端改动（任务级）

| ID | 任务 |
|----|------|
| FE-1 | attach 检查 `res.ok`，失败 toast + 不发送或剔除该 file_id |
| FE-2 | 上传匹配改用服务端返回顺序或 client 临时 id |
| FE-3 | 修正 source 筛选：upload/minio/generated 与后端枚举对齐 |
| FE-4 | long_doc 芯片文案：「发送后索引」 |
| FE-5 | 图片预览走带 token 的 blob URL 或专用 preview API |

---

## 5. E2E 建议（Playwright）

1. 库中选 PDF long_doc → 发送 → 问文档内容 → 有检索结果。  
2. 库中删除文件 → 同会话再问 → 无结果或明确错误。  
3. 同时上传两个 `test.txt` → file_id 与文件内容对应。

---

*第五遍 — 前端*
