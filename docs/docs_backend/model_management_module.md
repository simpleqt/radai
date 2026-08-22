# Model Management Module (模型管理与热更新) 详解

> **最后同步**：2026-07-09（对齐 `models.py`、`llm_model.py`、`factory.py`、`asr_config.py`）

模型管理模块是整个系统的“引擎调速器”。当前主路径为 **MySQL 持久化 + 进程内缓存与 TTL + Redis 订阅/发布实时同步**；`backend/config.yaml` 与 `settings` 仍作为模型工厂 fallback，用于数据库不可用或未命中时保持兼容。这保证了在多 Worker、多进程的生产环境下，管理员能够零重启热更新对话模型配置。

---

## 🏗️ 整体架构与设计模式

系统采用三层架构来实现模型的高效访问与实时更新：

```text
热更新路径（零重启）
────────────────────
Admin PUT /api/v1/models/{id}
    → MySQL 事务提交
    → notify_model_change(name, usage_scope)
    → Redis PUBLISH model_config_changed
    → 各 API/Worker 节点 subscribe_model_changes 收到
    → invalidate_cache(name, scope)
    → 下次 create_chat_model 从 DB 重载

读路径（带 TTL）
────────────────
create_chat_model(name)
    → _DB_CACHE[usage_scope:name] 未过期 ? 命中 : 查 MySQL → 写缓存(30s)
    → MODEL_CONFIG_SOURCE=db 失败时降级 backend/config.yaml → settings

ASR 读取路径
────────────────
POST /api/v1/asr/transcribe
    → load_asr_config_from_db(name?)
    → llm_models where usage_scope='asr' and is_active=true
    → 未配置则返回 503
```

---

## 💾 数据库模型设计 (`backend/db/models/llm_model.py`)

数据库定义了两张表：`llm_models`（存储模型核心参数）与 `llm_model_access`（细粒度访问控制）。

### 1. `LlmModel` 核心字段与用途

| 字段名 | 类型 | 说明 | 示例值 |
| :--- | :--- | :--- | :--- |
| **id** | Integer | 自增主键 | `1` |
| **usage_scope** | String(32) | 用途域（与 `name` 联合唯一） | `"chat"`（Agent 对话）；**`"asr"`**（语音转写，见 `backend/core/models/asr_config.py`）；预留 `kb_chat`/`embedding`/`rerank` |
| **name** | String(100) | 模型唯一标识（API 与智能体调用名） | `"qwen-max"` |
| **display_name**| String(200) | 前端展示名 | `"通义千问 Qwen-Max"` |
| **provider** | String(50) | 模型提供商 | `"qwen"` / `"deepseek"` / `"custom"` |
| **use** | String(200) | LangChain 对应的 ChatModel 适配器路径 | `"langchain_openai:ChatOpenAI"` |
| **model** | String(100) | 调用接口实际的模型名称 | `"qwen-max-2025-01-25"` |
| **api_key** | String(500) | 环境变量引用标识或加密明文 | `"$DASHSCOPE_API_KEY"` |
| **base_url** | String(500) | API 请求基地址 | `"https://dashscope.aliyuncs.com/compatible-mode/v1"` |
| **api_key_env_var** / **base_url_env_var** | String | 当列值为空或 `$ENV` 未取到时的环境变量名 fallback | `"DASHSCOPE_API_KEY"` |
| **request_timeout** / **max_retries** | Float / SmallInteger | 请求超时与重试次数 | `600.0` / `2` |
| **max_tokens** | Integer | 最大输出 token | `16384` |
| **max_input_tokens** | Integer | 最大输入 token / 上下文窗口，运行时会注入 `profile.max_input_tokens` | `131072` |
| **temperature** / **top_p** | Float | 采样参数 | `0.1` / `0.8` |
| **extra_body** | JSON | OpenAI-compatible 额外请求体参数 | `{"enable_thinking": false}` |
| **when_thinking_enabled** | JSON | 开启思考模式时追加/覆盖的请求参数 | `{"extra_body": {"enable_thinking": true}}` |
| **thinking_config** | JSON | 结构化思考配置，转换为 factory 内部 `thinking` | `{"enabled": true}` |
| **supports_thinking** | Boolean | 是否支持深度思考（Reasoning）模式 | `true` |
| **supports_vision** | Boolean | 是否支持多模态图片输入 | `true` |
| **supports_reasoning_effort** | Boolean | 是否支持 OpenAI 风格 `reasoning_effort` | `false` |
| **supports_streaming** | Boolean | 是否支持流式输出（模型工厂会按适配器处理） | `true` |
| **use_responses_api** / **output_version** | Boolean/String | OpenAI Responses API 与结构化输出兼容字段 | `false` / `"responses/v1"` |
| **is_default** | Boolean | 是否为系统当前默认模型 | `true` |
| **is_active** | Boolean | 是否启用 | `true` |
| **sort_order** | SmallInteger | 默认模型候选排序 | `0` |

### 2. 智能环境变量解析机制

为了确保密钥安全并适配不同的部署环境，模型 ORM 提供了智能解析方法：

*   **`resolve_api_key()`** / **`resolve_base_url()`**:
    *   检查列值，若以 `$` 开头（如 `"$DASHSCOPE_API_KEY"`），则自动从系统当前环境变量中读取对应的键值。
    *   若不以 `$` 开头，则作为加密或明文直接返回。
    *   若上述两步均为空，则自动回退读取 `api_key_env_var` 字段所配置的环境变量。

---

## ⚡ 缓存与分布式同步机制 (`backend/core/models/model_cache.py`)

由于频繁的 SQL 查询会带来极高的 I/O 开销，系统引入了**带 TTL 进程内缓存与 Redis Pub/Sub 同步的“双通道”缓存机制**。

### 1. 进程内缓存 (`_DB_CACHE`)
*   **结构**: `Dict[str, dict]` 存储已解析的配置字典，缓存键为 `f"{usage_scope}:{name or '__default__'}"`。
*   **TTL 机制**: 默认生存周期为 **30 秒** (`_DB_CACHE_TTL = 30.0`)。每次读取时检查时间差，超时自动失效并回退 MySQL 查询，取得新值后再回填。

### 2. 分布式多端同步 (`subscribe_model_changes`)
当管理员在后台修改、禁用或删除了某个模型时，系统如何确保所有的 API 服务与 Worker 节点同步刷新？

1.  **事件广播**: API 在提交修改事务后，调用 `notify_model_change(name, usage_scope)` 方法。
2.  **通道发布**: 借助 `redis.asyncio`，通过 Redis Channel `"model_config_changed"` 发布一条 JSON 变更消息：
    ```json
    {
      "name": "qwen-max",
      "usage_scope": "chat",
      "action": "update"
    }
    ```
3.  **监听订阅**: 所有在线的 API 节点在服务启动的 Lifespan 阶段均启动了一个名为 `subscribe_model_changes()` 的后台侦听任务，订阅该 Redis 频道。
4.  **按需清除**: 一旦收到广播消息，节点立刻调用 `invalidate_cache(name, usage_scope)` 将当前进程内的特定模型缓存清除。**下一次用户调用该模型时，服务便会自动从 MySQL 中读取最新配置，实现了零重启的毫秒级热更新！**

---

## 🚀 统一模型工厂 (`backend/core/models/factory.py`)

`create_chat_model` 接收模型名与配置参数，并在底层统一生成对应的 `BaseChatModel` 实例。

### 1. 模型解析源优先级 (`MODEL_CONFIG_SOURCE`)

解析顺序完全由环境变量 `MODEL_CONFIG_SOURCE` 控制（默认值为 `"db"`）：

```text
MODEL_CONFIG_SOURCE = "db"
  ├── 1. 尝试从 MySQL (带 cache + Redis 广播) 加载
  │   └── 成功: 实例化并返回 (如 LlmModel Or ModelConfig)
  ├── 2. 失败/未找到: 降级尝试从 backend/config.yaml 加载
  │   └── 成功: 实例化并返回
  └── 3. 仍失败: 最终回退至 BaseLLMProvider / settings 全局配置
```

### 2. 深度思考自适应 (`_apply_thinking_and_profile_settings`)

为了兼容不同厂商的 Reasoning 机制，模型工厂会在运行时自动调整参数：

*   **阿里通义 (DashScope/Qwen)**:
    *   若启用思考且支持，注入特定的 API 请求参数。
    *   若关闭思考，强行将 `extra_body` 中的 `enable_thinking` 设为 `false`，并从参数中剔除 `reasoning_effort`。
*   **VLLM / 兼容端点**:
    *   自动将 `extra_body` 中的 `chat_template_kwargs` 内的 `thinking` 键设为 `false`，避免不支持思考模式的模型引发请求崩溃。
*   **上下文窗口**:
    *   `max_input_tokens` 优先来自 MySQL；缺失时通过 `context_limits` 按配置/模型名推断，并写入 LangChain 模型实例的 `profile.max_input_tokens`。

---

## 🛠️ RESTful 管理接口 (`backend/api/routers/models.py`)

路由前缀为 `/api/v1/models`，所有接口均挂载 **`require_admin`** 依赖注入，强制进行管理员角色校验。

> 当前边界：`GET /api/v1/models?usage_scope=...` 可按 scope 查询；`POST`、`PUT`、`DELETE`、`toggle` 的写入和广播逻辑固定面向 `usage_scope="chat"`。`usage_scope="asr"` 记录由 ASR 运行时读取，但不是当前管理 API 的写入主路径。
> 管理审计：创建、更新、删除、启停都会通过 `write_admin_audit()` 写入管理员审计日志；响应中的 `api_key` 会做首尾掩码。

1.  **`GET /`** — 列出指定 `usage_scope` 下的全部模型配置（响应中的 `api_key` 会做掩码）。
2.  **`POST /`** — 创建新的 chat 模型配置。若请求中 `is_default=True`，会自动清空 chat scope 下其他模型的默认状态。随后通过 Redis 广播变更。
3.  **`PUT /{model_id}`** — 修改指定模型的所有参数。若 `api_key` 为空或仍是掩码值，则保留原密钥；更新完成后广播指定模型和默认模型缓存失效。
4.  **`DELETE /{model_id}`** — 删除模型配置，并向频道发布失效广播。
5.  **`POST /{model_id}/toggle`** — 快速开启/禁用模型 (`is_active` 状态切换)，用于应急上下架。

---

## 🎙️ ASR 配置读取 (`backend/core/models/asr_config.py`)

语音转写接口 `POST /api/v1/asr/transcribe` 要求登录用户，并从 `llm_models` 读取 `usage_scope="asr"`、`is_active=true` 的模型配置；未指定模型名时按 `is_default desc, sort_order` 取第一条。ASR 读取 `extra_body.default_language` 作为默认语言，缺省为 `zh`。未配置可用 ASR 模型时，接口返回 503。

---

## 📋 后续演化建议

1.  **API Key AES 加密**: 当前模型的 `api_key` 以明文存放在 MySQL 中（建议通过 `$ENV` 的形式引用系统环境变量）。后续可实现底层存储字段 of AES 加密，在 ORM 解析层进行动态解密。
2.  **细粒度权限管控**: 当前 `LlmModelAccess` 结构已预留，后续可无缝结合 `require_admin` 和用户组，为特定的科研团队提供专属的昂贵模型服务。
