# MySQL 模型管理实施方案

> **状态**: ✅ 已实现 — 详见 [docs_backend/model_management_module.md](./docs_backend/model_management_module.md)  
> 目标：将**前端对话模型**配置从 YAML 文件迁移到 MySQL 数据库，实现动态增删改查，无需重启服务。

### 范围界定（必读）

| 本阶段**做** | 本阶段**不做**（知识库 / RAG 模型端） |
|--------------|----------------------------------------|
| 聊天 UI 选用的对话大模型（`create_chat_model`、前端下拉框数据源） | 嵌入模型、重排序模型、知识库专用推理链、切片/向量化侧模型配置 |
| 与对话相关的启用/排序/默认模型/管理后台 CRUD | 为知识库流水线单独建表、独立管理 API、与向量库绑定的模型生命周期 |

**说明**：现有 `GET /api/v1/rag/models` 若仍被前端用作「对话模型列表」，在本方案中仅视为**对话模型列表的数据源对接**，不展开知识库业务域内的模型治理；知识库端模型管理留待后续迭代。

### 后续扩展预留（知识库模型端）

为二期「知识库模型端」管理预留空间，建议在本期落地时采用**可演进、低耦合**约定（实现可分期，但命名与字段上先留口）：

1. **数据域区分**：`llm_models` 增加可选字段 `usage_scope`（或等价枚举），默认 `'chat'`；未来知识库侧可 `'kb_chat'`、`'embedding'`、`'rerank'` 等，查询与缓存键均带 scope，避免与对话模型混读。
2. **表/API 演进**：二期可新增 `kb_llm_models`（或按 provider 拆表）及 `/api/v1/kb/models` 等独立路由；本期管理端与工厂只认 `usage_scope='chat'`，扩展时新增模块而非改写字段语义。
3. **缓存与事件**：Redis Pub/Sub / 缓存 key 使用前缀（如 `chat_model:` / 预留 `kb_model:`），避免对话与知识库配置互相冲掉。

---

## 一、现状分析

> 本节保留改造前的数据源问题作为历史背景。当前实现见 §4、§6 和 [模型管理模块文档](./docs_backend/model_management_module.md)：模型工厂与 `/api/v1/rag/models` 都优先读取 MySQL，`config.yaml` / settings 仅作降级兜底。

### 1.1 改造前基线（历史）

| 链路 | 代码入口 | 实际数据源 | 用途 |
|------|-----------|------------|------|
| **对话实例** | `create_chat_model()` → `_create_from_config` / `_create_from_settings` | ① `get_app_config()` 加载的 `config.yaml` 中 `models`（`ModelConfig`）；不可用时 ② `BaseLLMProvider.get_model`（`settings`） | Agent / 对话后端构造 `ChatModel` |
| **前端下拉** | `GET /api/v1/rag/models`（`backend/api/routers/rag.py`） | `settings.get_available_models()`：`AVAILABLE_MODELS` 环境变量（JSON 字符串）或内置默认列表（如 `qwen3-max`）；**不读取 `config.yaml` 的 `models`** | `frontend/src/views/chat/` 等 `loadModels()` |

其它相关配置：

| 配置源 | 位置 | 说明 |
|--------|------|------|
| `config.yaml` | `backend/config.yaml`，经 `backend/core/config` 解析 | 对话工厂主路径，字段与 `ModelConfig` 一致 |
| `Settings` | `backend/config/settings.py` | DashScope 等 Key/Base、`available_models` JSON、工厂兜底 |
| 环境变量 | `.env` | 敏感信息与 `AVAILABLE_MODELS` 等 |

**核心问题**（结合上表）：
- 下拉列表与 `create_chat_model` 使用的模型集合**可能不一致**（YAML 一套、Settings 一套），运维易混淆。
- 修改 `config.yaml` 通常需**重载应用配置**后工厂才稳定生效；无集中式启用/排序/默认模型管理。
- API Key 仍可能出现在 YAML；无 DB 侧权限与审计闭环。
- 无法按用户/角色区分可用模型（可选 `llm_model_access` 解决）。

### 1.2 数据流（现状 vs 目标）

**现状（两条独立链路）**：

```text
config.yaml ──► get_app_config() / ModelConfig ──► factory.create_chat_model() ──► ChatModel
settings.get_available_models() ──► GET /api/v1/rag/models ──► 前端 <select> 模型下拉
```

**当前结果**：`llm_models`（`usage_scope='chat'`）是默认主数据源；`GET /api/v1/rag/models` 与 `create_chat_model`（`MODEL_CONFIG_SOURCE=db`，默认）均优先读 DB，`config.yaml` / `settings` 作为明确的降级兜底，因此并非严格意义上的“唯一事实源”。

### 1.3 与仓库基础设施对齐（实施时注意）

| 项 | 仓库现状 | 方案落地建议 |
|----|-----------|----------------|
| ORM 位置 | 多数模型在 `backend/db/models/__init__.py`，知识库在 `knowledge_base.py` | 新增 `llm_model.py` 并在 `__init__.py` 导出、加入 `__all__`，与现有风格一致 |
| 表结构迁移 | 仓库内**未检出 Alembic**；`init_db()` 使用 `Base.metadata.create_all` | DDL 可随代码发布执行，或与 `create_all` 同步；若团队规范强制 Alembic，需**单独引入**后再改文档流程 |
| Redis | `backend/db/redis.py`：`get_redis()` 用 `redis_host` / `redis_port` / `redis_password` / `redis_db` **拼接 URL**，无 `settings.redis_url` | Pub/Sub 示例应复用该拼接逻辑或 `get_redis()`，勿假设 `redis_url` 字段 |
| RAG 路由前缀 | `http_server.py`：`rag_router` 挂载在 **`/api/v1/rag`** | 文档中路径保持 `/api/v1/rag/models` |
| 管理端前端 | `frontend/src/views/admin/platform.vue` 已提供模型列表、新增、编辑、启停和删除 | 继续沿用 `AdminLayout` 下的 `/admin/platform` 页面 |

### 1.4 现有 config.yaml 模型字段

```yaml
- name: deepseek-v4-flash          # 唯一标识
  display_name: deepseek-v4-flash  # 前端显示名
  use: langchain_openai:ChatOpenAI # LangChain 类路径
  model: deepseek-v4-flash         # 实际模型名
  api_key: sk-xxx                  # API Key
  base_url: https://xxx/v1         # API Base URL
  request_timeout: 600.0
  max_retries: 2
  max_tokens: 16384
  temperature: 0.1
  extra_body:                      # 额外参数（JSON）
    thinking:
      type: disabled
  reasoning_effort: none
  supports_vision: false
  supports_thinking: false
  supports_reasoning_effort: false
```

---

## 二、MySQL 表设计

### 2.1 核心表：`llm_models`（对话模型配置表）

```sql
CREATE TABLE `llm_models` (
  -- 主键
  `id`              INT UNSIGNED    NOT NULL AUTO_INCREMENT  COMMENT '自增主键',

  -- 用途域（预留：二期知识库模型端可扩展 embedding/rerank 等，本期仅使用 chat）
  `usage_scope`     VARCHAR(32)     NOT NULL DEFAULT 'chat' COMMENT '用途域：chat=前端对话；预留 kb_chat/embedding/rerank 等',

  -- 基础信息
  `name`            VARCHAR(100)    NOT NULL                 COMMENT '模型唯一标识（如 deepseek-v4-flash）',
  `display_name`    VARCHAR(200)    NOT NULL                 COMMENT '前端显示名称（如 DeepSeek V4 Flash）',
  `description`     VARCHAR(500)    DEFAULT NULL             COMMENT '模型描述',
  `provider`        VARCHAR(50)     NOT NULL DEFAULT 'custom' COMMENT '服务商标识（deepseek/qwen/minimax/zhipu/openai/custom）',
  `icon_url`        VARCHAR(500)    DEFAULT NULL             COMMENT '模型图标 URL（前端展示用）',

  -- LangChain 类映射
  `use`             VARCHAR(200)    NOT NULL DEFAULT 'langchain_openai:ChatOpenAI'
                                                              COMMENT 'LangChain 类路径（如 langchain_openai:ChatOpenAI）',
  `model`           VARCHAR(100)    NOT NULL                 COMMENT '实际模型名称（传给 API 的 model 参数）',

  -- API 连接配置
  `api_key`         VARCHAR(500)    NOT NULL                 COMMENT 'API Key（环境变量引用或列内值，见 2.3 节）',
  `base_url`        VARCHAR(500)    NOT NULL                 COMMENT 'API Base URL',
  `api_key_env_var` VARCHAR(100)    DEFAULT NULL             COMMENT 'API Key 环境变量名（如 DASHSCOPE_API_KEY，优先从环境变量读取）',
  `base_url_env_var`VARCHAR(100)    DEFAULT NULL             COMMENT 'Base URL 环境变量名（如 DASHSCOPE_API_BASE，优先从环境变量读取）',

  -- 模型参数
  `request_timeout` FLOAT           NOT NULL DEFAULT 600.0   COMMENT '请求超时（秒）',
  `max_retries`     SMALLINT UNSIGNED NOT NULL DEFAULT 2     COMMENT '最大重试次数',
  `max_tokens`      INT UNSIGNED    NOT NULL DEFAULT 16384   COMMENT '最大输出 token 数',
  `temperature`     FLOAT           NOT NULL DEFAULT 0.1     COMMENT '生成温度',
  `top_p`           FLOAT           DEFAULT NULL             COMMENT 'Top-P 采样参数',
  `frequency_penalty` FLOAT         DEFAULT NULL             COMMENT '频率惩罚',
  `presence_penalty`  FLOAT         DEFAULT NULL             COMMENT '存在惩罚',

  -- 扩展参数（JSON，对应 config.yaml 中的 extra_body / when_thinking_enabled 等）
  `extra_body`          JSON        DEFAULT NULL             COMMENT '额外请求体参数（如 {"thinking":{"type":"disabled"}}）',
  `when_thinking_enabled` JSON      DEFAULT NULL             COMMENT '思考模式启用时的额外参数',
  `thinking_config`     JSON        DEFAULT NULL             COMMENT '思考模式配置（合并到 when_thinking_enabled）',

  -- 模型能力标记
  `supports_vision`           BOOLEAN  NOT NULL DEFAULT FALSE  COMMENT '是否支持图片输入',
  `supports_thinking`         BOOLEAN  NOT NULL DEFAULT FALSE  COMMENT '是否支持思考/推理模式',
  `supports_reasoning_effort` BOOLEAN  NOT NULL DEFAULT FALSE  COMMENT '是否支持推理努力调节',
  `supports_streaming`        BOOLEAN  NOT NULL DEFAULT TRUE   COMMENT '是否支持流式输出',

  -- OpenAI Responses API 特殊配置
  `use_responses_api`  BOOLEAN      DEFAULT NULL             COMMENT '是否走 /v1/responses API',
  `output_version`     VARCHAR(50)  DEFAULT NULL             COMMENT '结构化输出版本（如 responses/v1）',

  -- 状态与排序
  `is_active`        BOOLEAN        NOT NULL DEFAULT TRUE     COMMENT '是否启用（前端只展示启用的模型）',
  `is_default`       BOOLEAN        NOT NULL DEFAULT FALSE    COMMENT '是否为默认模型（全局仅一个）',
  `sort_order`       SMALLINT       NOT NULL DEFAULT 0        COMMENT '排序权重（越小越靠前）',

  -- 审计字段
  `created_at`       DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP  COMMENT '创建时间',
  `updated_at`       DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `created_by`       VARCHAR(36)    DEFAULT NULL             COMMENT '创建人 user_id',
  `updated_by`       VARCHAR(36)    DEFAULT NULL             COMMENT '最后修改人 user_id',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_name_scope` (`name`, `usage_scope`),
  KEY `idx_provider` (`provider`),
  KEY `idx_scope_active_sort` (`usage_scope`, `is_active`, `sort_order`),
  KEY `idx_is_default` (`is_default`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='LLM 模型配置表（本期仅承载前端对话；usage_scope 预留知识库等扩展）';
```

**`is_default` 说明**：本期仅写入 `usage_scope='chat'`，全局至多一条 `is_default=TRUE`。未来多用途域并存时，建议在应用层按 `usage_scope` 分别维护默认模型（或再增加 `(usage_scope, is_default)` 相关约束）。

### 2.2 辅助表：`llm_model_access`（模型访问权限表，可选）

> 如果需要按用户/角色控制可用模型范围，使用此表。初期可跳过，默认所有用户可见所有 `is_active=1` 的模型。

```sql
CREATE TABLE `llm_model_access` (
  `id`          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `model_id`    INT UNSIGNED NOT NULL             COMMENT '关联 llm_models.id',
  `user_id`     VARCHAR(36)  DEFAULT NULL         COMMENT '用户 ID（NULL 表示按角色）',
  `role`        VARCHAR(50)  DEFAULT NULL         COMMENT '角色名（NULL 表示按用户）',
  `is_allowed`  BOOLEAN      NOT NULL DEFAULT TRUE COMMENT '是否允许访问（支持黑名单模式）',
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_model_user` (`model_id`, `user_id`),
  UNIQUE KEY `uk_model_role` (`model_id`, `role`),
  KEY `idx_user_id` (`user_id`),
  CONSTRAINT `fk_model_access_model` FOREIGN KEY (`model_id`) REFERENCES `llm_models`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='模型访问权限表';
```

### 2.3 API Key 存储策略

| 方案 | 说明 | 推荐 |
|------|------|------|
| **A. 环境变量引用** | `api_key` 字段存 `$DASHSCOPE_API_KEY`，或填写 `api_key_env_var`，运行时从 env 解析 | ✅ 当前推荐，与现有 config.yaml 兼容 |
| **B. 原样存储** | 非 `$` 值当前按列内值直接使用，管理 API 返回时掩码 | 已实现，适合内网/开发环境 |
| **C. AES 加密存储** | `api_key` 字段存加密密文，解密密钥从 env 读取 | 尚未实现，适合后续多租户增强 |

**当前实现**（`backend/db/models/llm_model.py`）：
- 如果 `api_key` 以 `$` 开头 → 视为环境变量引用，运行时解析
- 否则 → 直接使用列内值
- 如果 `api_key` 为空，再回退 `api_key_env_var` 指向的环境变量
- `base_url` 同样支持 `$ENV` 与 `base_url_env_var` 兜底
- 管理接口返回 `api_key` 时只返回掩码；更新时传空值或包含 `***` 会保留原密钥

---

## 三、SQLAlchemy ORM 模型

在 `backend/db/models/` 下新增 `llm_model.py`（与 `knowledge_base.py` 并列；勿与 `backend/core/config/model_config.py` 的 Pydantic `ModelConfig` 混淆）：

```python
"""LLM 模型配置 ORM 定义"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer,
    JSON, SmallInteger, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.db.models import Base

class ModelProvider(enum.Enum):
    """模型服务商"""
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    MINIMAX = "minimax"
    ZHIPU = "zhipu"
    OPENAI = "openai"
    CUSTOM = "custom"

class LlmModel(Base):
    """LLM 模型配置表（本期仅读写 usage_scope=chat；其他 scope 预留给知识库等）"""
    __tablename__ = "llm_models"
    __table_args__ = (
        UniqueConstraint("name", "usage_scope", name="uk_name_scope"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    usage_scope = Column(
        String(32),
        nullable=False,
        default="chat",
        comment="用途域：chat=对话；预留 kb_chat/embedding/rerank",
    )

    # 基础信息
    name = Column(String(100), nullable=False, comment="模型唯一标识（与 usage_scope 联合唯一）")
    display_name = Column(String(200), nullable=False, comment="前端显示名称")
    description = Column(String(500), nullable=True, comment="模型描述")
    provider = Column(String(50), nullable=False, default="custom", comment="服务商标识")
    icon_url = Column(String(500), nullable=True, comment="模型图标 URL")

    # LangChain 类映射
    use = Column(String(200), nullable=False, default="langchain_openai:ChatOpenAI",
                 comment="LangChain 类路径")
    model = Column(String(100), nullable=False, comment="实际模型名称")

    # API 连接配置
    api_key = Column(String(500), nullable=False, comment="API Key（环境变量引用或列内值）")
    base_url = Column(String(500), nullable=False, comment="API Base URL")
    api_key_env_var = Column(String(100), nullable=True, comment="API Key 环境变量名")
    base_url_env_var = Column(String(100), nullable=True, comment="Base URL 环境变量名")

    # 模型参数
    request_timeout = Column(Float, nullable=False, default=600.0, comment="请求超时（秒）")
    max_retries = Column(SmallInteger, nullable=False, default=2, comment="最大重试次数")
    max_tokens = Column(Integer, nullable=False, default=16384, comment="最大输出 token")
    temperature = Column(Float, nullable=False, default=0.1, comment="生成温度")
    top_p = Column(Float, nullable=True, comment="Top-P 采样")
    frequency_penalty = Column(Float, nullable=True, comment="频率惩罚")
    presence_penalty = Column(Float, nullable=True, comment="存在惩罚")

    # 扩展参数
    extra_body = Column(JSON, nullable=True, comment="额外请求体参数")
    when_thinking_enabled = Column(JSON, nullable=True, comment="思考模式启用时额外参数")
    thinking_config = Column(JSON, nullable=True, comment="思考模式配置")

    # 模型能力
    supports_vision = Column(Boolean, nullable=False, default=False, comment="支持图片输入")
    supports_thinking = Column(Boolean, nullable=False, default=False, comment="支持思考模式")
    supports_reasoning_effort = Column(Boolean, nullable=False, default=False,
                                       comment="支持推理努力调节")
    supports_streaming = Column(Boolean, nullable=False, default=True, comment="支持流式输出")

    # OpenAI Responses API
    use_responses_api = Column(Boolean, nullable=True, comment="走 /v1/responses API")
    output_version = Column(String(50), nullable=True, comment="结构化输出版本")

    # 状态与排序
    is_active = Column(Boolean, nullable=False, default=True, comment="是否启用")
    is_default = Column(Boolean, nullable=False, default=False, comment="是否默认模型")
    sort_order = Column(SmallInteger, nullable=False, default=0, comment="排序权重")

    # 审计
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    created_by = Column(String(36), nullable=True, comment="创建人")
    updated_by = Column(String(36), nullable=True, comment="最后修改人")

    # 关系
    access_rules = relationship("LlmModelAccess", back_populates="model",
                                cascade="all, delete-orphan")

    def resolve_api_key(self) -> str:
        """解析 API Key：环境变量引用 > 解密 > 原值"""
        import os
        raw = self.api_key or ""
        if raw.startswith("$"):
            env_var = raw[1:]
            return os.environ.get(env_var, raw)
        # 当前实现：非 $ENV 的值按列内值直接使用；AES 可作为后续增强
        return raw

    def resolve_base_url(self) -> str:
        """解析 Base URL：环境变量引用 > 原值"""
        import os
        raw = self.base_url or ""
        if raw.startswith("$"):
            env_var = raw[1:]
            return os.environ.get(env_var, raw)
        return raw

    def to_model_config_dict(self) -> dict:
        """转换为 factory.create_chat_model 所需的 ModelConfig 字典格式"""
        config = {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "use": self.use,
            "model": self.model,
            "api_key": self.resolve_api_key(),
            "base_url": self.resolve_base_url(),
            "request_timeout": self.request_timeout,
            "max_retries": self.max_retries,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "supports_vision": self.supports_vision,
            "supports_thinking": self.supports_thinking,
            "supports_reasoning_effort": self.supports_reasoning_effort,
        }
        # 可选字段
        if self.top_p is not None:
            config["top_p"] = self.top_p
        if self.frequency_penalty is not None:
            config["frequency_penalty"] = self.frequency_penalty
        if self.presence_penalty is not None:
            config["presence_penalty"] = self.presence_penalty
        if self.extra_body:
            config["extra_body"] = self.extra_body
        if self.when_thinking_enabled:
            config["when_thinking_enabled"] = self.when_thinking_enabled
        if self.thinking_config:
            config["thinking"] = self.thinking_config
        if self.use_responses_api is not None:
            config["use_responses_api"] = self.use_responses_api
        if self.output_version:
            config["output_version"] = self.output_version
        return config

    def __repr__(self):
        return f"<LlmModel(id={self.id}, name={self.name}, provider={self.provider})>"

class LlmModelAccess(Base):
    """模型访问权限表"""
    __tablename__ = "llm_model_access"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(Integer, nullable=False, comment="关联 llm_models.id")
    user_id = Column(String(36), nullable=True, comment="用户 ID")
    role = Column(String(50), nullable=True, comment="角色名")
    is_allowed = Column(Boolean, nullable=False, default=True, comment="是否允许")
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    model = relationship("LlmModel", back_populates="access_rules")

    def __repr__(self):
        return f"<LlmModelAccess(model_id={self.model_id}, user_id={self.user_id})>"
```

---

## 四、后端改造方案

### 4.1 当前 API 端点

在 `backend/api/routers/` 下新增 `models.py`：

```
GET    /api/v1/models              # 管理端按 usage_scope 获取模型配置列表
POST   /api/v1/models              # 新增模型（管理员）
PUT    /api/v1/models/{id}         # 更新模型（管理员）
DELETE /api/v1/models/{id}         # 删除模型（管理员）
POST   /api/v1/models/{id}/toggle  # 启用/禁用模型（管理员）
```

以上接口均要求管理员权限；`GET /api/v1/models` 是管理端完整列表，不是普通用户的前端下拉接口。对话页的可用模型列表由 `GET /api/v1/rag/models` 提供。默认模型和排序通过 `PUT /api/v1/models/{id}` 更新 `is_default`、`sort_order`，当前没有独立的 `/{id}/default` 或 `/sort` 路由。

**关键接口设计**：

```python
# GET /api/v1/models — 管理端调用，可按 usage_scope 查询，密钥字段返回掩码
@router.get("")
async def list_admin_models(
    usage_scope: str = "chat",
    admin: ApiKeyAuth = Depends(require_admin),
):
    """获取指定 scope 的管理端模型列表。"""
```

```python
# POST /api/v1/models — 管理员新增模型
@router.post("/models")
async def create_model(request: ModelCreateRequest, admin: AdminAuth):
    """新增模型配置"""
    # 1. 校验 (name, usage_scope) 唯一性（本期 usage_scope 固定 chat）
    # 2. api_key 可填写 $ENV；非 $ 值当前按列内值保存，响应时掩码
    # 3. 如果 is_default=True，先清除其他模型的 is_default
    # 4. 插入记录
    # 5. 刷新内存缓存
```

### 4.2 改造模型工厂（factory.py）

**核心改动**：`create_chat_model` 优先从数据库读取，YAML 作为兜底。

```python
# backend/core/models/factory.py 改造逻辑

def create_chat_model(name: str | None = None, thinking_enabled: bool = False, **kwargs):
    """
    优先级（DB 模式开启时；与 MODEL_CONFIG_SOURCE 等开关配合）：
    1. 数据库 llm_models（usage_scope='chat'，动态，热更新）
    2. config.yaml / get_app_config()（与当前仓库 factory 行为一致，作兜底）
    3. Settings / BaseLLMProvider（与当前仓库一致，最终兜底）
    """
    # 尝试从数据库加载
    db_config = _load_from_db(name)
    if db_config:
        return _create_from_db_config(db_config, thinking_enabled, **kwargs)

    # 兜底：从 config.yaml 加载（保持与现有 _create_from_config 一致）
    if _check_config_available():
        return _create_from_config(name, thinking_enabled, **kwargs)

    # 最终兜底：Settings（与现网 factory 一致，签名不含 thinking_enabled）
    return _create_from_settings(name, **kwargs)

def _load_from_db(name: str | None) -> dict | None:
    """从数据库加载模型配置（带进程内缓存 + TTL）"""
    global _db_cache, _db_cache_ts

    now = time.time()
    if _db_cache and (now - _db_cache_ts) < _DB_CACHE_TTL:
        cached = _db_cache.get(name or "__default__")
        if cached:
            return cached

    # 同步查询数据库（仓库提供 get_session()，勿混用未导出的 get_async_session_factory 做同步 IO）
    from backend.db.connection import get_session
    from backend.db.models.llm_model import LlmModel
    from sqlalchemy import select

    session = get_session()
    try:
        if name is None:
            stmt = select(LlmModel).where(
                LlmModel.usage_scope == "chat",
                LlmModel.is_active == True,
            ).order_by(LlmModel.sort_order).limit(1)
        else:
            stmt = select(LlmModel).where(
                LlmModel.name == name,
                LlmModel.usage_scope == "chat",
                LlmModel.is_active == True,
            ).limit(1)

        result = session.execute(stmt)
        model = result.scalar_one_or_none()
        if model:
            config = model.to_model_config_dict()
            _db_cache = _db_cache or {}
            _db_cache[name or "__default__"] = config
            _db_cache_ts = now
            return config
    finally:
        session.close()

    return None
```

**实现提示**：`_create_from_db_config` 建议将 `LlmModel.to_model_config_dict()` 的结果转为与 `get_app_config().get_model_config(name)` 等价的结构后，**复用**现有 `_create_from_config` 内关于 `thinking_enabled`、`extra_body`、`reasoning_effort` 的处理（或抽取公共函数），避免 DB 路径与 YAML 路径行为不一致。

### 4.3 改造 `/api/v1/rag/models` 接口（仅对话列表数据源）

> **注意**：路径名含 `rag` 为历史原因。本方案中该接口**只服务前端对话模型下拉**，不实现知识库嵌入/重排等模型管理；知识库模型端后续可独立路由（如 `/api/v1/kb/models`）。

```python
# backend/api/routers/rag.py — get_available_models 改造

@router.get("/models", response_model=ModelsListResponse)
async def get_available_models():
    """获取可用对话模型列表 — 优先从数据库读取（usage_scope='chat'）"""
    from backend.db.connection import get_async_session_factory
    from backend.db.models.llm_model import LlmModel
    from sqlalchemy import select

    # 与 backend/db/connection.py 一致：async_sessionmaker 实例可 async with
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        # 尝试从数据库读取
        stmt = select(LlmModel).where(
            LlmModel.usage_scope == "chat",
            LlmModel.is_active == True,
        ).order_by(LlmModel.sort_order)
        result = await session.execute(stmt)
        db_models = result.scalars().all()

        if db_models:
            model_list = [
                ModelInfo(
                    id=m.name,
                    name=m.display_name or m.name,
                    provider=m.provider,
                    description=m.description,
                )
                for m in db_models
            ]
            default_model = next(
                (m.name for m in db_models if m.is_default),
                db_models[0].name if db_models else "deepseek-v4-flash"
            )
            return ModelsListResponse(models=model_list, default_model=default_model)

    # 兜底：与现网 rag.py 一致 — settings.get_available_models()（含 AVAILABLE_MODELS JSON）
    from backend.config.settings import settings
    models = settings.get_available_models()
    model_list = [
        ModelInfo(id=m.get("id", ""), name=m.get("name", m.get("id", "")),
                  provider=m.get("provider", "custom"), description=m.get("description"))
        for m in models
    ]
    default_model = settings.dashscope_model or "qwen3-max"
    return ModelsListResponse(models=model_list, default_model=default_model)
```

### 4.4 数据迁移脚本

```python
# backend/scripts/migrate_models_to_db.py
"""将 config.yaml 中的模型配置迁移到 MySQL llm_models 表"""

import yaml
from backend.db.connection import get_session
from backend.db.models.llm_model import LlmModel

def migrate():
    with open("backend/config.yaml", "r") as f:
        config = yaml.safe_load(f)

    models = config.get("models", [])
    session = get_session()

    try:
        for i, m in enumerate(models):
            existing = (
                session.query(LlmModel)
                .filter_by(name=m["name"], usage_scope="chat")
                .first()
            )
            if existing:
                print(f"  跳过已存在: {m['name']}")
                continue

            record = LlmModel(
                usage_scope="chat",
                name=m["name"],
                display_name=m.get("display_name", m["name"]),
                description=m.get("description"),
                provider=_infer_provider(m),
                use=m.get("use", "langchain_openai:ChatOpenAI"),
                model=m.get("model", m["name"]),
                api_key=m.get("api_key", ""),
                base_url=m.get("base_url", ""),
                api_key_env_var=_extract_env_var(m.get("api_key", "")),
                base_url_env_var=_extract_env_var(m.get("base_url", "")),
                request_timeout=m.get("request_timeout", 600.0),
                max_retries=m.get("max_retries", 2),
                max_tokens=m.get("max_tokens", 16384),
                temperature=m.get("temperature", 0.1),
                extra_body=m.get("extra_body"),
                when_thinking_enabled=m.get("when_thinking_enabled"),
                thinking_config=m.get("thinking"),
                supports_vision=m.get("supports_vision", False),
                supports_thinking=m.get("supports_thinking", False),
                supports_reasoning_effort=m.get("supports_reasoning_effort", False),
                use_responses_api=m.get("use_responses_api"),
                output_version=m.get("output_version"),
                is_active=True,
                is_default=(i == 0),  # 第一个模型设为默认
                sort_order=i * 10,
            )
            session.add(record)
            print(f"  迁移: {m['name']}")

        session.commit()
        print(f"迁移完成，共处理 {len(models)} 个模型")
    except Exception as e:
        session.rollback()
        print(f"迁移失败: {e}")
    finally:
        session.close()

def _infer_provider(model_config: dict) -> str:
    """从模型名/URL 推断 provider"""
    name = model_config.get("model", "").lower()
    url = model_config.get("base_url", "").lower()
    if "deepseek" in name: return "deepseek"
    if "minimax" in name: return "minimax"
    if "glm" in name or "zhipu" in url: return "zhipu"
    if "qwen" in name or "dashscope" in url: return "qwen"
    return "custom"

def _extract_env_var(value: str) -> str | None:
    """提取环境变量名（如 $DASHSCOPE_API_KEY → DASHSCOPE_API_KEY）"""
    if value and value.startswith("$"):
        return value[1:]
    return None

if __name__ == "__main__":
    migrate()
```

---

## 五、前端改造方案

### 5.1 模型选择组件增强

当前前端模型选择是简单的 `<select>` 下拉框，改造后需支持：

| 功能 | 说明 |
|------|------|
| 模型图标 | 根据 `icon_url` 或 `provider` 显示对应图标 |
| 能力标签 | 显示 `supports_vision` / `supports_thinking` 标签 |
| 描述提示 | hover 显示 `description` |
| 默认标记 | 默认模型加 ⭐ 标记 |

### 5.2 管理页面（新增）

在 `frontend/src/views/admin/` 下新增**对话模型**管理页面（知识库模型管理 UI 不在本期）：

```
/admin/models
  ├── 模型列表（表格：名称/Provider/状态/排序/操作）
  ├── 新增模型（表单：所有字段）
  ├── 编辑模型（同新增，预填数据）
  └── 删除确认
```

**关键交互**：
- API Key 输入框：密码模式，支持"测试连接"按钮
- `extra_body` / `when_thinking_enabled`：JSON 编辑器（带语法校验）
- 排序：拖拽排序或上下箭头
- 启用/禁用：开关组件，即时生效

---

## 六、缓存与热更新策略

### 6.1 进程内缓存

```python
# backend/core/models/model_cache.py

# 键建议带用途域前缀，预留 kb / embedding 等不共用同一 dict
_DB_CACHE: dict[str, dict] = {}       # f"{usage_scope}:{name}" → config_dict
_DB_CACHE_TS: float = 0               # 上次刷新时间
_DB_CACHE_TTL: float = 30.0           # 缓存 TTL（秒）

def get_cached_model_config(name: str | None) -> dict | None:
    """带 TTL 的进程内缓存"""
    ...

def invalidate_cache(name: str | None = None):
    """主动失效（增删改时调用）"""
    global _DB_CACHE, _DB_CACHE_TS
    if name:
        _DB_CACHE.pop(name, None)
    else:
        _DB_CACHE.clear()
    _DB_CACHE_TS = 0
```

### 6.2 多 Worker 同步

当 `workers > 1` 时，一个 Worker 的修改需通知其他 Worker 刷新缓存：

| 方案 | 说明 | 推荐 |
|------|------|------|
| **A. Redis Pub/Sub** | 修改后发布 `model_config_changed` 事件，其他 Worker 订阅并刷新 | ✅ 推荐；项目已有 `backend/db/redis.py`（需配置 `redis_host` 等） |
| **B. 短 TTL** | 缓存 TTL 设为 10-30 秒，自然过期刷新 | 简单但有延迟 |
| **C. 数据库轮询** | 定时查询 `updated_at` 变化 | 不推荐 |

**推荐方案 A**（连接方式与 `backend/db/redis.py` 一致，**不要**使用不存在的 `settings.redis_url`）：

```python
# 写入后发布（channel / payload 带 usage_scope，避免与后续知识库模型事件混淆）
def _redis_url() -> str:
    from backend.config.settings import settings as s
    if s.redis_password:
        return f"redis://:{s.redis_password}@{s.redis_host}:{s.redis_port}/{s.redis_db}"
    return f"redis://{s.redis_host}:{s.redis_port}/{s.redis_db}"

async def _notify_model_change(name: str, usage_scope: str = "chat"):
    import json
    import redis.asyncio as aioredis
    r = aioredis.from_url(_redis_url(), decode_responses=True)
    try:
        await r.publish(
            "model_config_changed",
            json.dumps({"name": name, "usage_scope": usage_scope, "action": "update"}),
        )
    finally:
        await r.aclose()

# 启动时订阅（长连接需在应用 lifespan 中管理；多 Worker 时各进程各自订阅）
async def _subscribe_model_changes():
    import json
    import redis.asyncio as aioredis
    r = aioredis.from_url(_redis_url(), decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe("model_config_changed")
    async for message in pubsub.listen():
        if message["type"] == "message":
            data = json.loads(message["data"])
            invalidate_cache(data.get("name"))
```

---

## 七、实施步骤

### Phase 1：建表 + ORM + 迁移（1-2 天）

1. ✅ 执行 DDL 创建 `llm_models` 表（含 `usage_scope`，默认 `chat`）；或合并进现有 `init_db()` / `create_all` 流程（仓库当前无 Alembic 目录）
2. ✅ 新增 `backend/db/models/llm_model.py` ORM 定义
3. ✅ 在 `backend/db/models/__init__.py` 中 import 新模型并导出
4. ✅ 已提供 `backend/scripts/migrate_models_to_db.py`（从 `config.yaml` 的 `models` 导入，`usage_scope='chat'`）；是否已在目标数据库执行需由部署环境单独确认
5. ✅ 验证数据完整性

### Phase 2：后端 API + 工厂改造（2-3 天）

1. ✅ 新增 `backend/api/routers/models.py`（CRUD API，本期仅 `usage_scope=chat`）
2. ✅ 改造 `factory.py`：优先从 DB 读取（**仅对话**；不按本期方案改知识库流水线）
3. ✅ 改造 `GET /api/v1/rag/models`：优先从 DB 读取**对话模型**（`usage_scope='chat'`）
4. ✅ 实现缓存层（进程内 + Redis Pub/Sub）
5. ⚠️ API Key 当前支持环境变量引用、列内值与返回掩码；AES 加密/解密工具尚未落地
6. ✅ 单元测试

### Phase 3：前端管理页面（2-3 天）

1. ✅ 模型管理页面（列表/新增/编辑/删除）
2. ✅ 对话模型选择读取 Agent Info 的 `available_models`
3. 📋 API Key 测试连接功能尚无专用后端端点和前端操作
4. 📋 模型排序拖拽尚未实现；`sort_order` 字段可由 API 更新，但当前模型编辑表单未暴露该输入

### Phase 4：灰度切换 + 清理（1 天）

1. ✅ 环境变量 `MODEL_CONFIG_SOURCE=db|yaml` 控制切换
2. 📋 灰度验证属于部署动作，代码仓库无法证明已在目标环境完成
3. 📋 全量切换属于部署动作，需按实际环境确认
4. ⏸ `backend/config.yaml` 中仍保留 `models` 段，作为数据库未命中或不可用时的 fallback

---

## 九、ER 图

```text
┌─────────────────────────────────────────────────────────────────┐
│ llm_models (PK: id)                                              │
│  usage_scope · name · provider · model · api_key · is_default …  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ 1
                               │ has access rules
                               ▼ N
┌─────────────────────────────────────────────────────────────────┐
│ llm_model_access (PK: id, FK: model_id)                          │
│  user_id · role · is_allowed · created_at                        │
└──────────────────────────────┬──────────────────────────────────┘
                               │ N
                               │ has model access
                               ▼ 1
┌─────────────────────────────────────────────────────────────────┐
│ users (PK: user_id) · username                                   │
└─────────────────────────────────────────────────────────────────┘

约束: (name, usage_scope) 联合唯一 uk_name_scope
```

**约束说明**：`name` 与 `usage_scope` 联合唯一（`uk_name_scope`）；本期数据以 `usage_scope='chat'` 为主。

---

## 十、安全注意事项

1. **API Key 掩码返回**：`GET /api/v1/models` 仅供管理员使用；响应保留 `api_key` 字段，但只返回首尾字符掩码，不返回完整密钥
2. **管理接口鉴权**：CRUD 接口需管理员权限（`role=admin`）
3. **API Key 存储边界**：优先使用 `$ENV` 或 `api_key_env_var`；非环境变量引用的 Key 当前仍为列内值，AES-256-GCM 加密需后续实现
4. **操作审计**：创建、更新、删除和启停通过 `write_admin_audit()` 写入管理员审计日志；ORM 的 `created_by/updated_by` 字段当前未由该路由主动赋值
5. **SQL 注入防护**：使用 SQLAlchemy ORM，参数化查询
