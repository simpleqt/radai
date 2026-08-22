# Skills 上传、授权与智能体绑定设计方案

> **状态**：核心链路已实现；本文同时保留设计约束与剩余运维增强项
> **设计日期**：2026-06-12
> **最后同步**：2026-07-09
> **适用系统**：当前 FastAPI + MySQL + MinIO + Redis + Agent Runtime V2 架构

## 1. 设计目标

在现有本地目录型 Skill 机制上增加一套可管理的控制面，支持：

1. 用户或管理员上传 Skill ZIP 包，并完成格式、安全和运行时兼容性校验；
2. Skill 的版本、发布、禁用、归档与审计管理；
3. 按用户或角色授予 `查看 / 使用 / 编辑 / 管理` 权限；
4. 将 Skill 绑定到一个或多个智能体，并配置必选、默认选中和可选状态；
5. 在 Agent Runtime 执行前进行服务端二次鉴权，禁止前端通过伪造 `selected_skills` 越权；
6. 兼容当前 `backend/skills/public|custom`、`ScopedSkillsProvider`、`SkillManifestLoader` 和 `ToolAssemblyPipeline`。

本方案只允许上传“声明型 Skill”。上传包可以引用平台已注册工具，但不能通过上传 Python、Shell 或动态 provider 代码注册新工具。

---

## 2. 现有系统分析

### 2.1 当前链路

```text
backend/skills/public|custom/<skill>/SKILL.md
        │
        ├─ core/skills/loader.py
        ├─ core/skills/scoped_skills_provider.py
        └─ application/skills/manifest_loader.py
                    │
                    ▼
POST /api/v2/agent-runtime/query
selected_skills: [skill-name]
                    │
                    ▼
AgentService -> AgentRuntimeContext -> ToolAssemblyPipeline
```

### 2.2 可直接复用的能力

| 现有能力 | 复用方式 |
| :--- | :--- |
| `verify_api_key` / `require_admin` | 继续作为登录用户和管理员认证入口 |
| `users.role` | 第一阶段支持 `admin`、`user` 两类角色授权 |
| `agents` / `agent_kb_bindings` | 沿用现有智能体管理风格新增 Skill 绑定 |
| `SkillManifestLoader` | 复用 frontmatter、工具、workflow、KB、guardrail 校验 |
| `ScopedSkillsProvider` | 改为消费数据库计算出的有效 Skill，不再信任文件中的 `owner_id` |
| `ToolRegistry` / `ToolPolicy` | 上传 Skill 只能声明已注册且策略允许的工具 |
| MinIO | 保存不可变 ZIP 原包 |
| Redis | 设计用于发布后广播缓存失效事件；当前权限正确性以 MySQL 查询兜底 |

### 2.3 当前实现与剩余缺口

1. 托管 Skill 已具备数据库元数据、版本、绑定和授权记录；没有绑定记录时运行时回退到 profile/default Skill 与 public/custom 文件 Skill。
2. `/agent-runtime/skill-manifests` 已要求 `verify_api_key`，并按用户、智能体、workflow 解析托管 Skill；同时补充匹配 profile/domain 的 public manifest。
3. `/agent-runtime/query` 已通过 `SkillRuntimeResolver` 校验已请求 Skill 是否存在、已发布、已绑定、workflow/domain 兼容且当前用户有 `use` 权限；解析失败时仅在显式兼容开关或匿名兼容场景下回退。
4. `custom` 文件 Skill 仍可作为兼容来源，但托管 Skill 的授权不再信任 frontmatter `owner_id`。
5. 智能体默认 Skill 仍保留在 `backend/config/agents/*.yaml` 作为非托管/降级事实源；托管绑定可覆盖可选、必选和默认选择。
6. 多 API 实例下的缓存失效、包物化和跨进程同步仍需要结合 Redis/对象存储策略继续完善；即使缓存未命中，授权判断也必须回查 MySQL。

---

## 3. 总体架构

```text
管理端 / 用户端
   │
   ├─ POST /api/v1/skills/uploads
   ├─ Skill 版本、发布、授权 API
   └─ /api/v1/agents/{agent_code}/skills 绑定 API
   │
   ▼
SkillsRouter
   │
   ▼
SkillManagementService
   ├─ SkillPackageValidator
   ├─ SkillCatalogRepository ─────────────► MySQL
   ├─ SkillPackageStorage ────────────────► MinIO 私有对象
   ├─ SkillMaterializer ──────────────────► 本地只读缓存
   ├─ SkillAccessService
   └─ SkillAuditService
                 │
                 └─ Redis `skill:catalog:changed`

Agent Runtime
   │
   ▼
SkillRuntimeResolver
   ├─ 查询智能体绑定
   ├─ 查询用户/角色授权
   ├─ 合并必选与默认 Skill
   └─ 校验状态、版本、domain、workflow、ToolPolicy
   │
   ▼
AgentRuntimeContext.selected_skills
   │
   ▼
ScopedSkillsProvider / SkillManifestLoader / ToolAssemblyPipeline
```

### 核心原则

- **MySQL 是控制面事实源**：所有权、状态、当前版本、授权和绑定均以数据库为准；
- **MinIO 保存不可变原包**：每个版本对应一个 SHA-256 固定的 ZIP；
- **本地目录只是运行缓存**：丢失后可从 MinIO 重建，不作为授权依据；
- **绑定不等于授权**：Skill 必须同时满足“已绑定智能体”和“用户可使用”；
- **服务端强制校验**：前端返回的 Skill 列表仅用于交互，不能作为可信权限来源；
- **名称兼容**：运行时继续使用稳定的 `skill_key`，兼容现有 `selected_skills: string[]`。

---

## 4. Skill 包规范

### 4.1 上传格式

第一阶段仅支持 `.zip`，包内允许以下两种布局：

```text
SKILL.md
references/
templates/
assets/
```

或：

```text
my-skill/
├── SKILL.md
├── references/
├── templates/
└── assets/
```

解压后的 Skill 根目录必须且只能包含一个入口 `SKILL.md`。

### 4.2 推荐 frontmatter

```yaml
---
name: groundwater-report
version: 1.2.0
description: 地下水项目报告分析与质量检查方法
domain: groundwater-project-analysis
license: internal
tools:
  - groundwater_project_skill_read
workflows:
  allowed:
    - full
  preferred: full
kb_policy:
  required: false
  binding_mode: agent_or_user_selected
guardrails:
  - groundwater_project_scope
---
```

约束：

| 字段 | 规则 |
| :--- | :--- |
| `name` | 必填，`^[a-z][a-z0-9-]{2,63}$`，作为稳定 `skill_key` |
| `version` | 必填，SemVer，如 `1.2.0` |
| `description` | 必填，1-500 字符 |
| `domain` | 必填，必须与目标智能体 domain 兼容 |
| `tools` | 只能引用 `ToolRegistry` 已注册工具 |
| `workflows.allowed` | 只能引用当前 `SUPPORTED_WORKFLOWS` |
| `owner_id` | 上传时忽略，不作为授权依据 |

### 4.3 安全限制

建议默认限制：

| 项目 | 限制 |
| :--- | :--- |
| ZIP 原包大小 | 20 MB |
| 解压后总大小 | 100 MB |
| 文件数量 | 200 |
| 单文件大小 | 10 MB |
| 路径深度 | 8 层 |
| `SKILL.md` 大小 | 1 MB |

必须拒绝：

- `../`、绝对路径、Windows 盘符、空字节和 Unicode 归一化后重复路径；
- 符号链接、硬链接、设备文件、加密 ZIP 和 ZIP bomb；
- `.py`、`.pyc`、`.pyo`、`.sh`、`.bash`、`.so`、`.dll`、`.exe`、`.jar`、`.class`、`.bin` 等可执行内容；
- 多个 `SKILL.md` 入口或入口缺失；
- 未注册工具、未知 workflow、非法 YAML、名称或版本不匹配；
- 文件摘要与记录不一致的包。

允许的资源后缀采用白名单，例如 `.md`、`.txt`、`.json`、`.yaml`、`.yml`、`.csv`、`.html`、`.css`、`.js`、`.png`、`.jpg`、`.jpeg`、`.webp`、`.gif`、`.svg`。HTML/JS 只允许作为 Skill 模板或工件内容，不在 API 服务进程中执行，也不直接以内联管理页面方式渲染。

上传流程必须先写临时目录，完成全部校验后再原子发布到缓存目录。原 ZIP 始终保存在私有 MinIO 路径，不返回公开 URL。

---

## 5. 生命周期

### 5.1 Skill 状态

```text
draft -> published -> disabled -> published
   └──────────────► archived
```

### 5.2 版本状态

```text
uploaded -> validated -> pending_review -> published
                  └───────────────► rejected
```

- 当前实现中上传成功会创建 `draft` Skill 与 `validated` 版本；普通用户需要再调用 `submit` 进入 `pending_review`；
- 管理员可直接发布；
- 同一 Skill 可以有多个历史版本，但只能有一个 `active_version_id`；
- 发布新版本时旧版本保留，支持回滚；
- `disabled` 后新请求立即不可用，正在执行的请求不强制中断；
- `archived` 只允许查询历史，不允许重新绑定，恢复需管理员操作。

---

## 6. 数据库设计

### 6.1 `skills`：Skill 主表

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `skill_id` | `VARCHAR(36)` PK | UUID |
| `skill_key` | `VARCHAR(64)` UNIQUE | 运行时稳定名称，对应 frontmatter `name` |
| `display_name` | `VARCHAR(100)` | 管理端显示名 |
| `description` | `VARCHAR(500)` | 描述 |
| `domain` | `VARCHAR(64)` | Skill 领域 |
| `source_type` | `VARCHAR(20)` | `builtin` / `uploaded` |
| `visibility` | `VARCHAR(20)` | `public` / `private` / `restricted` |
| `owner_user_id` | `VARCHAR(36)` NULL | 上传者；内置 Skill 可为空 |
| `status` | `VARCHAR(20)` | `draft/published/disabled/archived` |
| `active_version_id` | `VARCHAR(36)` NULL | 当前发布版本 |
| `created_by` | `VARCHAR(36)` | 创建人 |
| `created_at` | `DATETIME` | 创建时间 |
| `updated_at` | `DATETIME` | 更新时间 |

索引：`uk_skills_key(skill_key)`、`idx_skills_owner(owner_user_id)`、`idx_skills_status(status)`。

### 6.2 `skill_versions`：不可变版本表

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `version_id` | `VARCHAR(36)` PK | UUID |
| `skill_id` | `VARCHAR(36)` | 关联 Skill |
| `version` | `VARCHAR(32)` | SemVer |
| `status` | `VARCHAR(20)` | 版本状态 |
| `package_bucket` | `VARCHAR(128)` | MinIO bucket |
| `package_object` | `VARCHAR(512)` | ZIP 对象路径 |
| `package_sha256` | `VARCHAR(64)` | 原包摘要 |
| `package_size` | `BIGINT` | 原包字节数 |
| `manifest_json` | `JSON` | 校验后的标准化 Manifest |
| `file_index_json` | `JSON` | 文件路径、大小、摘要索引 |
| `review_comment` | `TEXT` NULL | 审核意见 |
| `created_by` | `VARCHAR(36)` | 上传人 |
| `reviewed_by` | `VARCHAR(36)` NULL | 审核人 |
| `created_at` | `DATETIME` | 上传时间 |
| `published_at` | `DATETIME` NULL | 发布时间 |

唯一约束：`uk_skill_version(skill_id, version)`。

### 6.3 `skill_acl`：用户与角色授权

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `id` | `BIGINT` PK | 自增 ID |
| `skill_id` | `VARCHAR(36)` | Skill ID |
| `subject_type` | `VARCHAR(16)` | `user` / `role` |
| `subject_id` | `VARCHAR(64)` | 用户 ID 或角色名，如 `user` |
| `permission` | `VARCHAR(16)` | `view/use/edit/manage` |
| `effect` | `VARCHAR(8)` | `allow/deny` |
| `expires_at` | `DATETIME` NULL | 可选过期时间 |
| `created_by` | `VARCHAR(36)` | 授权人 |
| `created_at` | `DATETIME` | 创建时间 |

唯一约束：`uk_skill_acl(skill_id, subject_type, subject_id, permission)`。

### 6.4 `agent_skill_bindings`：智能体绑定

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `id` | `BIGINT` PK | 自增 ID |
| `agent_code` | `VARCHAR(64)` | 统一使用 Agent Runtime profile code，如 `ne`、`csh` |
| `skill_id` | `VARCHAR(36)` | Skill ID |
| `is_enabled` | `TINYINT(1)` | 是否启用 |
| `is_required` | `TINYINT(1)` | 每次请求强制加载，用户不能取消 |
| `default_selected` | `TINYINT(1)` | 默认选中，但用户可取消 |
| `is_selectable` | `TINYINT(1)` | 是否在前端选择器展示 |
| `sort_order` | `SMALLINT` | 展示顺序 |
| `created_by` | `VARCHAR(36)` | 绑定人 |
| `created_at` | `DATETIME` | 创建时间 |
| `updated_at` | `DATETIME` | 更新时间 |

唯一约束：`uk_agent_skill(agent_code, skill_id)`。

规则：

- `is_required=true` 时必须同时满足 `is_enabled=true`；
- 必选 Skill 即使 `is_selectable=false` 也会进入运行时；
- Skill domain 与 Agent profile domain 不兼容时禁止绑定；
- 已禁用、未发布或没有活动版本的 Skill 禁止新绑定。

当前系统存在历史编码差异：Runtime profile 使用 `ne/csh`，部分 `agents` 表初始化数据使用 `ne_full_feature/csh_full_feature`，`AgentInfoService` 通过私有映射兼容。Skill 绑定应以 Runtime profile code 为唯一标准；迁移期新增统一的 `AgentCodeResolver` 处理旧编码，不能在 Skill 模块中再复制一份硬编码映射。后续建议将 `agents` 表主数据也迁移为 `ne/csh/groundwater`。

### 6.5 `skill_audit_logs`：审计日志

记录上传、校验、提交审核、发布、回滚、禁用、授权、撤权、绑定和解绑操作。建议字段：`audit_id`、`skill_id`、`version_id`、`actor_user_id`、`action`、`target_type`、`target_id`、`detail_json`、`request_id`、`created_at`。

---

## 7. 权限模型

### 7.1 权限含义

| 权限 | 能力 |
| :--- | :--- |
| `view` | 查看元数据、版本和文件清单 |
| `use` | 在已绑定的智能体中选择并执行 |
| `edit` | 上传新版本、修改未发布元数据 |
| `manage` | 发布、禁用、归档、授权和智能体绑定 |

### 7.2 默认权限

- `admin`：拥有所有权限；
- Skill owner：默认拥有 `view/use/edit`，不默认拥有发布与全局授权权限；
- `visibility=public` 且已发布：所有已认证用户默认拥有 `view/use`；
- `visibility=private`：仅 owner、管理员和显式授权主体可见；
- `visibility=restricted`：仅显式 ACL、owner 和管理员可使用；
- 访客端默认不开放上传 Skill 和自定义 Skill，只允许管理员配置的公开、已绑定 Skill。

### 7.3 判定优先级

```text
admin 全权限
  > 用户级 deny
  > 用户级 allow
  > 角色级 deny
  > 角色级 allow
  > owner 默认权限
  > public 默认 view/use
  > 默认拒绝
```

`manage` 隐含 `edit/view/use`，`edit` 隐含 `view/use`，但数据库中仍建议按单个权限保存，便于审计和过期处理。

### 7.4 最终可执行条件

用户在某智能体中执行 Skill，必须同时满足：

```text
Skill.status == published
AND active_version 已发布
AND agent_skill_bindings.is_enabled == true
AND 用户拥有 use 权限
AND Skill domain 与 Agent domain 兼容
AND 当前 workflow 在 allowed_workflows 中
AND Skill 声明工具通过 ToolRegistry + ToolPolicy
AND KB/guardrail 条件满足
```

---

## 8. API 设计

统一使用现有认证头：

```http
Authorization: Bearer <session_token>
# 或
X-API-Key: <api_key>
```

### 8.1 Skill 查询与上传

| 方法 | 路径 | 权限 | 说明 |
| :--- | :--- | :--- | :--- |
| `GET` | `/api/v1/skills` | 登录 | 返回当前用户可见 Skill，可按 `agent_code/status/owner` 过滤 |
| `GET` | `/api/v1/skills/{skill_id}` | `view` | Skill 详情、当前版本和绑定摘要 |
| `POST` | `/api/v1/skills/uploads` | 登录 | 上传新 Skill 或新版本 ZIP |
| `GET` | `/api/v1/skills/{skill_id}/versions` | `view` | 版本列表 |
| `POST` | `/api/v1/skills/{skill_id}/versions/{version_id}/submit` | `edit` | 提交审核 |
| `GET` | `/api/v1/skills/{skill_id}/versions/{version_id}/download` | `view` | 返回短时效私有下载 URL |

上传请求：

```http
POST /api/v1/skills/uploads
Content-Type: multipart/form-data

file=<skill.zip>
visibility=private
```

上传响应当前为 Skill 主体字段，并内嵌 `version` 与 `validation` 对象：

```json
{
  "skill_id": "8a1e4d4d-32f3-4f55-b5be-845f76aef180",
  "skill_key": "groundwater-report",
  "status": "validated",
  "version": {
    "version_id": "f65e2de4-831c-4285-a44c-d724b07a3939",
    "version": "1.2.0",
    "status": "validated",
    "package_sha256": "..."
  },
  "validation": {
    "valid": true,
    "warnings": [],
    "tools": ["groundwater_project_skill_read"],
    "allowed_workflows": ["full"]
  }
}
```

同一 `skill_key` 已存在时：

- owner 或管理员上传不同版本，创建 `skill_versions`；
- 无 `edit` 权限的用户返回 `409 SKILL_KEY_ALREADY_EXISTS`，不能覆盖；
- 相同版本或相同 SHA-256 重复上传返回 `409 SKILL_VERSION_EXISTS`。

### 8.2 审核与生命周期 API

| 方法 | 路径 | 权限 | 说明 |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/skills/{skill_id}/versions/{version_id}/publish` | admin/`manage` | 发布并设置活动版本 |
| `POST` | `/api/v1/skills/{skill_id}/versions/{version_id}/reject` | admin/`manage` | 驳回版本 |
| `POST` | `/api/v1/skills/{skill_id}/versions/{version_id}/activate` | admin/`manage` | 回滚或切换活动版本 |
| `POST` | `/api/v1/skills/{skill_id}/disable` | admin/`manage` | 禁用 Skill |
| `POST` | `/api/v1/skills/{skill_id}/enable` | admin/`manage` | 重新启用 |
| `DELETE` | `/api/v1/skills/{skill_id}` | admin/`manage` | 逻辑归档，不物理删除历史包 |

发布操作必须在一个数据库事务内完成：锁定 Skill、校验版本状态、更新旧活动版本、设置 `active_version_id`、写审计日志。事务提交后再发布 Redis 缓存失效事件。

### 8.3 授权 API

| 方法 | 路径 | 权限 | 说明 |
| :--- | :--- | :--- | :--- |
| `GET` | `/api/v1/skills/{skill_id}/grants` | `manage` | 查询授权列表 |
| `POST` | `/api/v1/skills/{skill_id}/grants` | `manage` | 新增或更新授权 |
| `DELETE` | `/api/v1/skills/{skill_id}/grants/{grant_id}` | `manage` | 撤销授权 |

请求示例：

```json
{
  "subject_type": "user",
  "subject_id": "user-uuid",
  "permissions": ["view", "use"],
  "effect": "allow",
  "expires_at": "2026-12-31T23:59:59+08:00"
}
```

角色授权示例：

```json
{
  "subject_type": "role",
  "subject_id": "user",
  "permissions": ["view", "use"],
  "effect": "allow"
}
```

服务端必须校验 `subject_type=user` 对应用户存在；`subject_type=role` 第一阶段只允许系统已知角色。

### 8.4 智能体绑定 API

接口风格与现有 KB 绑定保持一致：

| 方法 | 路径 | 权限 | 说明 |
| :--- | :--- | :--- | :--- |
| `GET` | `/api/v1/agents/{agent_code}/skills` | admin | 查询绑定 |
| `PUT` | `/api/v1/agents/{agent_code}/skills` | admin | 全量替换绑定 |
| `POST` | `/api/v1/agents/{agent_code}/skills` | admin | 追加或更新单条绑定 |
| `DELETE` | `/api/v1/agents/{agent_code}/skills/{skill_id}` | admin | 解绑 |

全量设置示例：

```json
{
  "skills": [
    {
      "skill_id": "skill-uuid-1",
      "is_enabled": true,
      "is_required": true,
      "default_selected": true,
      "is_selectable": false,
      "sort_order": 0
    },
    {
      "skill_id": "skill-uuid-2",
      "is_enabled": true,
      "is_required": false,
      "default_selected": true,
      "is_selectable": true,
      "sort_order": 1
    }
  ]
}
```

全量替换必须先完整校验所有 Skill，再在同一事务内更新，避免部分绑定成功。

智能体属于平台级配置，权限严格沿用当前 `agents.py` 的 `require_admin`。Skill 的 `manage` 权限不自动赋予修改全局智能体配置的能力。

### 8.5 运行时发现 API 改造

现有接口调整：

```http
GET /api/v2/agent-runtime/skill-manifests?agent_code=ne
```

当前实现会先解析数据库托管 Skill；若托管目录可用，则返回当前用户在指定智能体下可使用且可选择的 Skill，并额外合并与 profile/domain/workflow 匹配的 public Skill manifest。若托管解析失败，则回退到 profile 默认 Skill，再同样合并匹配的 public Skill manifest。

```json
{
  "skills": [
    {
      "id": "skill-uuid",
      "name": "nuclear-electronics",
      "description": "...",
      "version": "1.0.0",
      "required": true,
      "default_selected": true,
      "selectable": false,
      "allowed_workflows": ["full", "lightweight"]
    }
  ]
}
```

`GET /api/v2/agent-runtime/profiles/{agent_code}/info` 增加：

```json
{
  "selected_skills": ["nuclear-electronics"],
  "required_skills": ["nuclear-electronics"],
  "available_skills": [
    {
      "id": "skill-uuid",
      "name": "nuclear-electronics",
      "label": "核电子学分析",
      "default_selected": true,
      "required": true,
      "selectable": false
    }
  ]
}
```

该 `/info` 接口当前通过 `verify_api_key` 获取 `user_id` 与 `role`，再调用运行时解析器生成 `available_skills` / `required_skills`。访客应用需要单独使用访客白名单结果，不能复用普通用户私有授权。`/skill-manifests` 的合并逻辑只补充 `category=public` 且 domain 与 profile code/domain 或通用 Skill 域匹配的 manifest，已存在同名托管 Skill 时不重复追加。

---

## 9. Agent Runtime 鉴权与合并规则

### 9.1 请求处理

```text
客户端 selected_skills
        │
        ▼
SkillRuntimeResolver.resolve(user_id, agent_code, workflow, requested)
        │
        ├─ 获取已发布、已启用的智能体绑定
        ├─ 获取用户 use 权限
        ├─ 加入 is_required Skill
        ├─ 请求未显式传 selected_skills 时加入 default_selected Skill
        ├─ 校验 domain/workflow/KB/guardrail
        └─ 返回 effective_skills + resolved manifests
        │
        ▼
AgentRuntimeContext.selected_skills
```

### 9.2 明确的空列表语义

当前请求字段已是：

```python
selected_skills: list[str] | None = None
```

- 托管 Skill 生效时，`null` 或字段缺失：使用数据库中的 `default_selected + required`；
- 托管 Skill 生效时，`[]`：只使用 `required`；
- 托管 Skill 生效时，非空数组：校验请求列表均可用，再加入 `required`；
- 请求包含无权使用、未发布、未绑定或 workflow/domain 不兼容的托管 Skill 时，不静默丢弃，返回明确错误；
- 没有托管绑定时，`null` 使用 profile `default_skills`，空数组保持为空；若请求的是已存在托管 Skill 但未绑定当前智能体，则返回 `SKILL_NOT_BOUND_TO_AGENT`。

### 9.3 错误码

| HTTP | 业务码 | 场景 |
| :--- | :--- | :--- |
| `400` | `INVALID_SKILL_PACKAGE` | ZIP 或 Manifest 非法 |
| `403` | `SKILL_ACCESS_DENIED` | 用户没有 `use` 权限 |
| `404` | `SKILL_NOT_FOUND` | Skill 不存在或私有且不可见 |
| `409` | `SKILL_NOT_PUBLISHED` | 未发布、禁用或无活动版本 |
| `409` | `SKILL_NOT_BOUND_TO_AGENT` | 未绑定当前智能体 |
| `409` | `SKILL_DOMAIN_MISMATCH` | domain 不兼容 |
| `422` | `SKILL_WORKFLOW_NOT_ALLOWED` | workflow 不允许 |
| `422` | `SKILL_TOOL_POLICY_DENIED` | 声明工具不满足策略 |

### 9.4 防止绕过

以下入口必须统一调用 `SkillRuntimeResolver`：

- `/api/v2/agent-runtime/query`；
- MCP Agent 执行入口；
- 子智能体 `task_tool.py`；
- 后台研究任务和恢复任务；
- 访客 Agent 入口。

不能只在前端选择器或 `/skill-manifests` 接口过滤。

---

## 10. 与现有代码的改造点

### 10.1 建议新增

```text
backend/
├── api/routers/skills.py
├── application/skills/
│   ├── management_service.py
│   ├── package_validator.py
│   ├── access_service.py
│   ├── runtime_resolver.py
│   └── materializer.py
├── db/models/skill.py
├── db/crud/skill_crud.py
└── services/skill_package_storage.py
```

### 10.2 建议修改

| 文件 | 改造内容 |
| :--- | :--- |
| `backend/db/models/__init__.py` | 导入 Skill ORM，确保 `create_all` 可发现 |
| `backend/api/http_server.py` | 注册 `/api/v1/skills` 路由 |
| `backend/api/routers/agents.py` | 增加 `/skills` 绑定接口，或拆为独立 router |
| `backend/api/routers/agent_runtime.py` | info/manifests 按用户过滤；query 调用 RuntimeResolver |
| `backend/application/agent_service.py` | 使用解析后的有效 Skill，保留 `null` 与 `[]` 的差异 |
| `backend/core/skills/scoped_skills_provider.py` | 接收已授权 Skill 描述，不再依据 frontmatter `owner_id` |
| `backend/core/skills/loader.py` | 支持内置目录与 materialized 目录组成的多个根路径 |
| `backend/application/skills/manifest_loader.py` | 支持按活动版本路径加载，缓存键包含 `version_id` |
| `backend/application/agents/info_service.py` | 返回 `available_skills/required_skills` |
| `backend/config/agents/*.yaml` | `default_skills` 迁移后仅作为数据库不可用时的兼容兜底 |

### 10.3 内置 Skill 迁移

首次迁移脚本扫描：

```text
backend/skills/public/*/SKILL.md
backend/skills/custom/*/SKILL.md
```

并写入：

- `skills.source_type=builtin`；
- `public` 目录映射为 `visibility=public`；
- `custom` 目录根据现有 `owner_id` 尽力映射 owner，无法映射时设为管理员托管；
- 当前文件内容生成 `1.0.0+imported` 版本；
- profile YAML 中 `default_skills` 转换为 `agent_skill_bindings`。

迁移后内置 Skill 可继续从仓库目录加载，但权限与绑定以数据库为准。上传 Skill 则从 MinIO materialize 到：

```text
backend/data/skill_cache/<skill_id>/<version_id>/
```

生产部署应将该目录配置为共享卷或允许各 API/Worker 实例独立按需重建。

---

## 11. 缓存与一致性

建议缓存：

- `skill:meta:{skill_id}`：Skill 与活动版本；
- `skill:access:{user_id}:{agent_code}`：用户在智能体下的有效 Skill；
- `skill:manifest:{version_id}`：标准化 Manifest；
- 本地物化目录按 `version_id` 不可变。

发布、禁用、授权和绑定变更后发送：

```json
{
  "event": "skill.catalog.changed",
  "skill_id": "...",
  "agent_codes": ["ne"],
  "user_ids": ["..."],
  "version": 18
}
```

API 和 Worker 订阅 Redis channel `skill:catalog:changed`，清理相关进程缓存。数据库查询仍是最终兜底，缓存失效不能影响权限正确性。

---

## 12. 前端管理建议

管理端新增三个页面：

1. **Skills 管理**：列表、状态、owner、当前版本、上传新版本、发布/禁用/回滚；
2. **授权管理**：按用户或角色配置权限及过期时间；
3. **智能体绑定**：在现有智能体管理页，与知识库绑定并列配置 Skill。

聊天页的 Skill 选择器不再硬编码 `availableSkills`，改为读取 Agent info：

- `required=true`：展示为锁定选中；
- `default_selected=true`：首次进入默认选中；
- `selectable=false`：不允许用户切换；
- 切换智能体或 workflow 后重新拉取可用列表；
- 服务端返回 `SKILL_*` 错误时刷新 Agent info，避免使用过期缓存。

---

## 13. 审计与安全要求

1. 日志不得记录 ZIP 文件正文、完整 Skill 内容、Token 或 API Key；
2. 上传、发布、授权、绑定、禁用和回滚必须写审计表；
3. 下载使用 5-10 分钟有效的 presigned URL，并再次校验 `view` 权限；
4. 上传限流建议按用户 `10 次/小时`，管理员可单独配置；
5. 同一 Skill 发布使用数据库行锁或乐观版本号，防止并发覆盖；
6. Skill 内容属于提示词供应链输入，应在审核页展示工具、workflow、外链和敏感指令摘要；
7. 上传 Skill 不能扩大 ToolPolicy 权限，不能覆盖平台内置工具 provider；
8. 物理删除 MinIO 历史包应由独立保留策略处理，不与 API 逻辑归档同步执行。

---

## 14. 测试方案

### 14.1 单元测试

- ZIP 路径穿越、符号链接、压缩炸弹、重复路径和文件数限制；
- frontmatter、SemVer、工具、workflow、domain 和 KB 策略校验；
- ACL 优先级、过期授权、public/private/restricted；
- required/default/empty/null 的 Skill 合并规则；
- active version 切换与缓存键变化。

### 14.2 API 测试

- 普通用户上传、提交审核、管理员发布；
- owner 上传新版本，其他用户不能覆盖同名 Skill；
- 用户和角色授权、撤权、deny 覆盖 allow；
- 智能体全量绑定事务回滚；
- 未授权用户无法下载原包或查看私有元数据；
- info、manifest 和 query 三个接口返回一致的可用 Skill 集合。

### 14.3 运行时测试

- 伪造 `selected_skills` 返回 `403` 或 `409`；
- Skill 已禁用后新请求立即失败；
- ToolPolicy 拒绝的工具不会因 Skill 声明进入工具集；
- 主智能体和 `task_tool.py` 子智能体权限一致；
- 多 API 实例收到 Redis 事件后失效缓存；
- MinIO 可用、本地缓存丢失时能够重新 materialize。

---

## 15. 分阶段实施

### Phase 1：控制面基础

- 建表和迁移内置 Skill；
- 实现 ZIP 上传、校验、版本、发布和审计；
- 管理员完成 Skill 生命周期管理。

### Phase 2：授权与绑定

- 实现 `skill_acl` 和 `agent_skill_bindings`；
- 改造 Agent info 与 Skill manifest 接口；
- 前端选择器从后端动态加载。

### Phase 3：运行时强制鉴权

- 引入 `SkillRuntimeResolver`；
- 覆盖 query、MCP、子智能体、协同深研/恢复入口和访客入口；
- 移除对 frontmatter `owner_id` 的信任。

### Phase 4：多实例与运维

- Redis 缓存失效；
- MinIO 按需物化和缓存清理；
- 发布回滚、审计查询、监控告警和保留策略。

上线顺序必须先完成运行时鉴权，再向普通用户开放上传入口；否则会出现“管理端看似隔离，但请求可伪造 Skill 名称绕过”的安全缺口。

---

## 16. 验收标准

1. 用户上传合法 ZIP 后能够生成不可变版本和校验报告；
2. 未发布版本不能被 Agent 使用；
3. 用户只能看到和使用自己有权访问的 Skill；
4. Skill 只有绑定目标智能体后才能在该智能体中执行；
5. required、default 和用户显式空选的行为符合本文规则；
6. 伪造请求不能绕过用户授权、智能体绑定、workflow 或 ToolPolicy；
7. 发布、授权和绑定变化在多实例环境中能够及时生效；
8. 现有内置 Skills、Agent profile 和工具动态装配测试保持通过。
