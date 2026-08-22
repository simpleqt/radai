# 智能体-知识库动态绑定模块

> **最后同步**：2026-07-09
> 📊 KB 表与入库 → [infrastructure_module.md](./infrastructure_module.md) · 自研入库上手 → [self_ingestion_quickstart.md](../self_ingestion_quickstart.md)

## 1. 功能概述

```text
init_agent_bindings.py / PUT /api/v1/agents/{code}/kbs
        │
        ▼
agents + agent_kb_bindings (MySQL)
        │
        ├─► GET /api/v2/agent-runtime/profiles/{agent_code}/info → 前端 KB 名称/排序
        └─► AgentInfoService / KnowledgeBaseAccessPolicy / ProfiledAgentExecutor 读取默认 kb_ids
                └─► multi_kb_search → retrieve_context 每库并行
```

本模块将**智能体与知识库之间的关联关系从硬编码迁移到 MySQL 数据库**，实现：

- 每个智能体绑定的知识库列表**可在运行时通过管理接口动态变更**，无需重启服务；
- 前端知识库选择器的**名称标签**由后端从 `knowledge_bases` 表实时查询，不再在前端代码中维护映射表；
- 绑定关系与 KB 的 `ingestion_backend` / `retrieval_backend` **独立**：Agent 通过 `multi_kb_search` 检索时，各 KB 按自身后端路由 legacy DirectRetriever 或 self SelfDirectRetrieverV2；
- 提供完整的 **RESTful 管理 API**，支持对智能体及其知识库绑定进行增删改查。

---

## 2. 数据库设计

### 2.1 `agents` 表 — 智能体配置

| 列名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `agent_code` | `VARCHAR(64)` PK | 智能体唯一编码（如 `ne`、`csh`） |
| `agent_name` | `VARCHAR(100)` NOT NULL | 前端展示名称 |
| `description` | `TEXT` | 智能体描述 |
| `is_active` | `TINYINT(1)` | 是否启用 |
| `sort_order` | `SMALLINT` | 显示排序权重（升序） |
| `created_at` | `DATETIME` | 创建时间 |
| `updated_at` | `DATETIME` | 最后更新时间 |

### 2.2 `agent_kb_bindings` 表 — 知识库绑定

| 列名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `id` | `INT` PK AUTO_INCREMENT | 主键 |
| `agent_code` | `VARCHAR(64)` | 关联的智能体编码 |
| `kb_id` | `VARCHAR(36)` | 关联的知识库 UUID |
| `sort_order` | `SMALLINT` | 该 KB 在本智能体下的显示排序 |
| `created_at` | `DATETIME` | 绑定时间 |

- **唯一约束** `uk_agent_kb`：`(agent_code, kb_id)` 防止重复绑定；
- `agent_code` 建有普通索引；`kb_id` 建有普通索引。

> **说明**：由于 `agents` 表与 `knowledge_bases` 表创建时使用的 utf8mb4 排序规则（collation）不同（`agents` 使用 `utf8mb4_unicode_ci`，`knowledge_bases` 使用服务器默认值），无法在 `agent_kb_bindings → knowledge_bases` 之间建立 DB 级外键约束；引用完整性由**应用层**负责保证。`agent_kb_bindings → agents` 的外键关系在 ORM 级别通过 `relationship` 维持。

### 2.3 初始数据

| agent_code | agent_name | 绑定的 KB ID |
| :--- | :--- | :--- |
| `ne` | 核电子学智能体 | `c056bf59-…`（核电子学知识库）、`de4ccde7-…`（核电子学期刊） |
| `csh` / `csh_full_feature` | CSH-AI 水文地质智能体 | `b4c3f145-de5f-42fb-88de-2d4856d57460`（CSH 污染场地修复库，`domain` Profile） |

> 当前运行时 profile code 为 `ne` / `csh`。历史数据库中可能仍存在 `ne_full_feature` / `csh_full_feature`，`AgentCodeResolver` 与 `get_runtime_agent_kb_ids()` 会做兼容映射并优先读取当前 profile key。

---

## 3. 代码结构

```text
backend/
├── db/
│   ├── models/
│   │   └── agent.py              # ORM 模型：Agent、AgentKnowledgeBase
│   ├── crud/
│   │   └── agent_crud.py         # 异步 CRUD：get / list / upsert / bind
│   └── init_agent_bindings.py    # 一次性初始化脚本（预置智能体及其默认知识库）
├── api/
│   └── routers/
│       ├── agents.py             # 管理 API：GET/POST/PUT/DELETE /api/v1/agents
│       ├── agent_skills.py       # Agent-Skill 绑定 API：/api/v1/agents/{code}/skills
│       └── agent_runtime_v2.py   # /api/v2 聚合 agent-runtime / guest / explore
├── application/
│   └── agents/
│       └── info_service.py       # 标准 info 响应组装（含动态 KB 查询）
└── application/
    └── policies/
        └── knowledge_base_access.py # bound_only / bound_or_selected / owner/default 访问策略
```

---

## 4. 关键实现说明

### 4.1 ORM 模型（`backend/db/models/agent.py`）

```python
class Agent(Base):
    __tablename__ = "agents"
    agent_code = Column(String(64), primary_key=True)
    agent_name = Column(String(100), nullable=False)
    ...
    kb_bindings = relationship("AgentKnowledgeBase", back_populates="agent",
                               cascade="all, delete-orphan",
                               order_by="AgentKnowledgeBase.sort_order")

class AgentKnowledgeBase(Base):
    __tablename__ = "agent_kb_bindings"
    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_code = Column(String(64), nullable=False, index=True)
    kb_id = Column(String(36), nullable=False, index=True)
    sort_order = Column(SmallInteger, nullable=False, default=0)
```

### 4.2 CRUD 层（`backend/db/crud/agent_crud.py`）

| 函数 | 说明 |
| :--- | :--- |
| `get_agent_kb_ids(session, agent_code)` | 返回该智能体已绑定的 KB ID 列表，按 `sort_order` 排序 |
| `get_runtime_agent_kb_ids(session, profile_code)` | 按 `ne`/`csh` 与旧 `*_full_feature` 编码顺序解析运行时绑定 |
| `get_agent(session, agent_code)` | 获取单个智能体（含绑定） |
| `list_agents(session, active_only)` | 列出所有智能体（含绑定） |
| `upsert_agent(session, agent_code, ...)` | 创建或更新智能体元数据 |
| `set_agent_kb_bindings(session, agent_code, kb_ids)` | 整体替换绑定列表（先删后插） |
| `add_kb_binding(session, agent_code, kb_id, sort_order)` | 追加单条绑定 |
| `remove_kb_binding(session, agent_code, kb_id)` | 删除单条绑定 |
| `list_agent_bindings(session, agent_code)` | 列出绑定行（含 `sort_order`） |

### 4.3 标准 Info / Runtime 中的动态 KB 加载（以 `ne` 为例）

当前实现由 `backend/application/agents/info_service.py`、`profile_loader.py` 与运行时工作流统一读取绑定，不再由领域专属 API 手工拼装：

```python
profile = profile_loader.load("ne")
default_kb_ids = await get_agent_kb_ids(session, profile.code)
available_kbs = await _fetch_kb_options(default_kb_ids)
info = info_service.build_profile_info(profile, available_kbs=available_kbs)
```

`_fetch_kb_options` 查询 `knowledge_bases` 表，将 `kb_id → kb_name` 映射为前端可用的 `{ id, label }` 结构。

### 4.4 前端消费

后端 `/info` 接口响应新增字段 `available_kbs: KbOption[]`：

```typescript
export interface KbOption {
  id: string;
  label: string;   // 来自 knowledge_bases.kb_name
}

export interface AgentInfo {
  kb_ids: string[];
  available_kbs?: KbOption[];
  ...
}
```

`ChatBox.vue` 中的计算属性：

```typescript
const availableKbs = computed(() => {
  const kbs = props.agentInfo?.available_kbs;
  if (Array.isArray(kbs) && kbs.length > 0) {
    return kbs.map(kb => ({ id: kb.id, name: kb.label }));
  }
  // 降级：没有 available_kbs 时仅展示 id
  return (props.agentInfo?.kb_ids ?? []).map(id => ({ id, name: id }));
});
```

---

## 5. 管理 API（`/api/v1/agents`）

所有接口需要 **`admin`** 角色认证。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `GET` | `/api/v1/agents` | 列出所有智能体及其知识库绑定 |
| `GET` | `/api/v1/agents/{agent_code}` | 查询指定智能体详情 |
| `POST` | `/api/v1/agents/{agent_code}` | 创建或更新智能体元数据 |
| `PUT` | `/api/v1/agents/{agent_code}/kbs` | 整体替换知识库绑定列表 |
| `POST` | `/api/v1/agents/{agent_code}/kbs` | 追加一条知识库绑定 |
| `DELETE` | `/api/v1/agents/{agent_code}/kbs/{kb_id}` | 删除单条知识库绑定 |

### 请求/响应示例

**整体替换绑定（`PUT /api/v1/agents/ne/kbs`）：**

```json
{
  "kb_ids": [
    "c056bf59-6091-45ed-98cb-e2e860580a69",
    "de4ccde7-b286-4e49-bd8c-9dd37bd61878"
  ]
}
```

`set_agent_kb_bindings()` 会按数组顺序写入 `sort_order`；若要设置单条绑定排序，使用 `POST /api/v1/agents/{agent_code}/kbs` 的 `{ "kb_id": "...", "sort_order": 1 }`。

**智能体详情响应（`GET /api/v1/agents/ne`）：**

```json
{
  "agent_code": "ne",
  "agent_name": "核电子学智能体",
  "description": "专注核电子学领域，支持双知识库并行检索、深度研究及多轮对话。",
  "is_active": true,
  "sort_order": 0,
  "kb_bindings": [
    { "kb_id": "c056bf59-6091-45ed-98cb-e2e860580a69", "sort_order": 0 },
    { "kb_id": "de4ccde7-b286-4e49-bd8c-9dd37bd61878", "sort_order": 1 }
  ]
}
```

---

## 6. 初始化与迁移

### 6.1 创建表

```bash
# 在项目根目录，激活虚拟环境后执行：
python -m backend.db.init_db --create-tables
```

`agent_kb_bindings` 表如因排序规则冲突无法通过 ORM 自动建表，请改用以下 SQL 手动执行：

```sql
CREATE TABLE IF NOT EXISTS `agent_kb_bindings` (
  `id`          INT         NOT NULL AUTO_INCREMENT COMMENT '主键',
  `agent_code`  VARCHAR(64) NOT NULL COMMENT '关联智能体编码',
  `kb_id`       VARCHAR(36) NOT NULL COMMENT '关联知识库 ID',
  `sort_order`  SMALLINT    NOT NULL DEFAULT 0 COMMENT '展示排序（升序）',
  `created_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_agent_kb` (`agent_code`, `kb_id`),
  KEY `idx_agent_kb_agent` (`agent_code`),
  KEY `idx_agent_kb_kb` (`kb_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能体-知识库绑定表';
```

### 6.2 初始化默认绑定数据

```bash
# 首次初始化
python -m backend.db.init_agent_bindings

# 强制覆盖已有数据（重置）
python -m backend.db.init_agent_bindings --force
```

---

## 7. 数据流全链路

```
用户打开对话界面
       │
       ▼
GET /api/v2/agent-runtime/profiles/{agent_code}/info
       │
       ├─ 查 agent_kb_bindings 获取 kb_ids
       │
       ├─ 查 knowledge_bases 获取各 KB 的 kb_name
       │
       └─ 返回 { kb_ids, available_kbs: [{id, label}, …], … }
                    │
                    ▼
           前端 ChatBox.vue 渲染知识库选择器
           （label 来自 available_kbs，不再硬编码）
                    │
                    ▼
       用户提问，选中的 kb_ids 随请求提交
                    │
                    ▼
POST /api/v2/agent-runtime/query
       │
       ├─ 若请求未携带 kb_ids，由 KnowledgeBaseAccessPolicy 解析绑定/默认库
       ├─ 若请求携带 kb_ids，校验其为当前 agent 绑定库、默认库或当前用户 owner 库
       └─ 将 kb_ids 传入 ProfiledAgentExecutor / ToolAssemblyPipeline 执行
```

---

## 8. 注意事项

1. **唯一事实源**：当前默认知识库来源于 DB 绑定与 profile 的 `kb_policy` 组合，不再从旧 FullFeatureAgent 常量兜底。
2. **排序**：`agent_kb_bindings.sort_order` 控制前端知识库选择器中各 KB 的展示顺序，`agents.sort_order` 控制多智能体切换列表的顺序。
3. **删除知识库时**：由于没有 DB 级外键约束，删除 `knowledge_bases` 中的某个 KB 后，需手动或通过管理接口同步删除 `agent_kb_bindings` 中对应的绑定行。
4. **新增智能体**：需在 `agents` 表与 `agent_kb_bindings` 中补齐数据，并新增对应 profile YAML；运行入口仍复用统一的 `agent-runtime` API。
