# 用户隔离与文件安全模块详解

> **最后同步**：2026-07-14
> 本模块描述当前代码中的用户隔离基座：认证身份、内部 thread key、用户文件/对话附件、MinIO 作用域、知识库目录权限、运行时 Context Artifact 与 Skills 动态加载。历史文档中关于 `ScopedStorage` 旧路径回退的描述已不适用，当前实现拒绝无用户前缀的 legacy 读取。

## 隔离总览

```text
HTTP 请求
  ├─ Authorization: Bearer <token> / X-API-Key
  │    → backend/api/dependencies/auth.py
  │    → auth_service.resolve_*()
  │
  ├─ agent-runtime 登录用户
  │    → build_internal_thread_key(user_id, thread_id)
  │    → u_{uid}__t_{tid}
  │
  ├─ guest endpoint
  │    ├─ 有 token: u_{uid}__t_{tid}
  │    └─ 无 token: g_{HMAC(device)}__t_{tid}
  │
  ├─ 用户文件 / 对话附件
  │    ├─ UserFile.owner_user_id
  │    ├─ SessionFile.user_id + session_id
  │    └─ MinIO users/{uid}/...
  │
  └─ 知识库目录与上传
       ├─ require_admin() 管目录
       └─ require_kb_access() 管 KB 上传/入库

  └─ 大工具 Context Artifact
       ├─ context_artifacts 元数据表
       └─ ContextArtifactStore 按 run/thread/unit 记录引用
```

---

## 1. 认证与 token 边界

统一 API 鉴权位于 `backend/api/dependencies/auth.py`：

- `verify_api_key()`：强制认证，优先 `X-API-Key`，其次 Bearer token，最后兼容 `Authorization: Bearer <api_key>`。
- `optional_verify_api_key()`：有凭据则校验，无凭据返回 `ApiKeyAuth(api_key="", user_id="guest")`，主要用于 `/api/v2/guest/query`。
- `require_admin()`：管理员路由依赖。
- `require_kb_access()`：知识库级读写授权，读允许管理员、owner、默认/平台 KB、active agent 绑定 KB；写仅管理员或 owner。

登录 token 由 `backend/api/routers/auth.py:_issue_auth_token()` 写入 Redis：

```text
auth:token:{token} -> {user_id, username, display_name, role}
TTL = 24 小时
```

访客短信登录 `backend/api/routers/guest_sms_auth.py` 与普通账号登录签发同类 token。

---

## 2. thread key 与运行时路径

登录用户智能体入口会使用：

```text
build_internal_thread_key(user_id, thread_id) -> u_{uid}__t_{tid}
```

guest endpoint 的无登录设备态使用：

```text
g_{hmac_device_id}__t_{external_thread_id}
```

这些内部 key 被 LangGraph checkpointer 和运行时路径使用。用户侧展示、会话 API 和前端仍使用外部 `thread_id`。

---

## 3. MinIO 作用域存储 (`ScopedStorage`)

文件：`backend/core/storage/scoped_storage.py`

当前实现：

- 初始化前缀：`users/{user_id}/threads/{thread_id}/artifacts/`
- bucket：`get_user_minio_bucket_name()`
- `read_file(filename)`：只读取当前作用域下对象；不再回退 `artifacts/{thread_id}/{filename}`，因为旧命名空间无 `user_id`，不能安全授权。
- `save_file(filename, content, content_type)`：写入当前 thread artifact 前缀。
- `save_local_file(object_name, local_file_path)`：要求 `object_name.startswith(f"users/{user_id}/")`，否则抛出 `PermissionError`。
- `list_objects(prefix, recursive)`：只列当前 thread artifact 前缀。
- `delete_all_files()`：只删除当前 `users/{uid}/threads/{tid}/artifacts/` 前缀下对象；删除失败会抛出异常。

这层用于非沙箱/原生工具访问 MinIO。沙箱文件系统隔离见 [用户级隔离实施方案.md](../用户级隔离实施方案.md)。

---

## 4. 用户文件与对话附件隔离

### 4.1 个人文件夹

路由：`backend/api/routers/my_files.py`

当前端点实际路径：

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `POST` | `/api/v1/files/upload` | 上传到个人文件夹 |
| `GET` | `/api/v1/my-files` | 当前用户文件列表 |
| `PUT` | `/api/v1/my-files/{file_id}` | 重命名当前用户文件 |
| `DELETE` | `/api/v1/my-files/{file_id}` | 软删除当前用户文件并解绑会话 |
| `POST` | `/api/v1/sessions/{session_id}/files/attach` | 将个人文件绑定到会话 |
| `POST` | `/api/v1/generated-files/save` | 保存生成文件到个人文件夹 |

个人文件上传调用 `prepare_uploaded_user_file(storage_scope="personal")`，对象路径为：

```text
users/{owner_user_id}/personal-files/{file_id}/{filename}
users/{owner_user_id}/personal-files/{file_id}/images/{image_name}
```

### 4.2 对话附件

路由：`backend/api/routers/chat_files.py`

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `POST` | `/api/v2/chat-files/upload` | 上传并解析当前会话附件 |
| `GET` | `/api/v2/chat-files/{thread_id}` | 列出当前用户绑定到该会话的附件 |
| `DELETE` | `/api/v2/chat-files/{file_id}` | 删除当前用户的会话绑定，长文档会同步移除内存索引 |
| `GET` | `/api/v2/chat-files/{file_id}/images/{image_name}` | 签名 URL 读取文档图片 |

对话附件对象路径为：

```text
users/{owner_user_id}/threads/{thread_id}/chat-files/{file_id}/{filename}
users/{owner_user_id}/threads/{thread_id}/chat-files/{file_id}/images/{image_name}
```

所有列表、删除和绑定都按 `UserFile.owner_user_id` 与 `SessionFile.user_id` 过滤。

### 4.3 文件下载中心

路由：`backend/api/routers/files.py`，注册前缀 `/api/v2`：

- `GET /api/v2/files?thread_id=...`：列当前用户某会话下 active 文件。
- `GET /api/v2/files/{file_id}/meta`：校验权限后返回元数据。
- `GET /api/v2/files/{file_id}/download`：校验 owner 或 `user_file_permissions` 授权后下载，并写 `file_download_audit`。
- `GET /api/v2/files/minio-artifact/download`：仅允许当前用户 `users/{uid}/threads/...` 工件对象。
- `GET /api/v2/files/kb-image`：公共 KB 图片代理，使用签名校验。

下载时再次校验：

- MinIO 文件必须在用户桶且对象名以 `users/{owner_user_id}/` 开头。
- 本地文件必须位于 `base_dir/users/{owner_user_id}/threads/.../files/artifacts/...`，拒绝系统目录、符号链接和路径穿越。

---

## 5. 知识库目录与上传隔离

知识库目录路由：`backend/api/routers/directories.py`，注册前缀 `/api/v1`。

- 所有目录管理端点依赖 `require_admin()`。
- 路径通过 `_normalize_path()` 去空段；物理目录的重命名、移动、删除和重排由 `DirectoryService` 更新目录表与文档 `category_path`。
- `DirectoryService.get_directory_tree()` 会组合物理目录、文档隐含目录和基于 `document_date` 的虚拟日期目录。虚拟日期目录只能改为有效的 `YYYY-MM-DD`（同步更新文档日期）或移入物理目录，不能删除或排序。

知识库上传路由：`backend/api/routers/uploads.py`，注册为 `/api/v1/uploads/*`。

- 强制 `verify_api_key()`。
- 写操作调用 `require_kb_access(..., access="write")`。
- 待入库文件写公共 KB bucket：`settings.minio_bucket_name`，对象路径 `pending/{kb_id}/{file_id}/{filename}`。
- 这是设计例外，不走用户文件桶；隔离由 KB owner/admin 授权、`PendingFile.created_by` 过滤和 KB 入库权限保证。

---

## 6. Skills 动态加载与隔离

核心文件：

- `backend/core/skills/scoped_skills_provider.py`：读取 public/custom Skill 文件、生成 prompt section、限制 reference 路径。
- `backend/db/models/skill.py`：`skills`、`skill_versions`、`skill_acl`、`agent_skill_bindings`、`skill_audit_logs`。
- `backend/application/skills/runtime_resolver.py`：按用户、角色、agent、workflow 和绑定关系解析本次可用 Skill。
- `backend/api/routers/skills.py`、`agent_skills.py`：Skill ZIP 上传、版本发布/激活、授权和 Agent-Skill 绑定管理。

当前机制：

- public/custom 文件 Skill 仍由 `ScopedSkillsProvider` 扫描；数据库托管 Skill 由 `SkillRuntimeResolver` 和 `SkillCRUD` 读取。
- 根据用户/角色、Skill ACL、Agent 绑定、workflow 的 `allowed_workflows`、domain 兼容性过滤可见与可用 Skill。
- `build_skills_prompt_section()` 生成动态 prompt section。
- `read_skill_reference()` 使用 `Path.resolve()` 和 `relative_to()` 防路径穿越。
- `read_skill_reference`、`read_local_skill_file` 等原生 Skill 工具只在 ToolPolicy/registry 允许时装配，避免任意文件读取。

访客 endpoint 会强制 `selected_skills=[]`，不会向轻量访客请求注入 Skills。

---

## 7. Context Artifact 隔离

大工具结果不再全部塞进消息历史。`ContextPruningMiddleware` 会将超长工具输出写入 `ContextArtifactStore`，并在 MySQL `context_artifacts` 表记录元数据；运行时再通过 `read_context_artifact` 按需读取片段。

安全边界：

- artifact ref 由运行时生成，包含 `artifact_id` / `path` / `sha256` / `run_id` / `thread_id` 等元数据，不接受用户拼接任意本地路径。
- 本地实现默认根目录为 `CONTEXT_ARTIFACT_ROOT`（缺省 `/tmp/langchain_context_artifacts`），写入时防路径穿越；MinIO 实现使用 sidecar metadata。
- `read_context_artifact` 工具在 `backend/config/tools/registry.yaml` 注册，默认无副作用、15 秒超时，并受 ToolPolicy 控制。
- 多智能体 handoff / 大 RAG 结果通过 context ledger 和 artifact 引用传递，减少跨用户/跨线程上下文混入风险。

---

## 8. 关联文档

| 文档 | 重点 |
| :--- | :--- |
| [用户级隔离实施方案.md](../用户级隔离实施方案.md) | 智能体 runtime、沙箱路径、会话删除与本地目录隔离 |
| [原生环境_非沙箱_用户隔离实施方案.md](../原生环境_非沙箱_用户隔离实施方案.md) | MinIO `ScopedStorage` 和原生工具安全 |
| [chat_file_upload_design.md](../chat_file_upload_design.md) | 对话附件上传、解析、图片与检索 |

---

© 2026 核电子学智能助手项目组.
