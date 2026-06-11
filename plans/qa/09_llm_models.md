# 09 · LLM 模型与工厂

> 📊 **图表：** 完整 ASCII 图见 [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 17、19

> 本模块答案采用**分层讲解**：先给结论，再解释「为什么」，最后用项目里的真实代码和场景串起来。

---

## L1 初级 🟢

### Q1：`create_chat_model` 是什么？项目里哪些地方会调用它？

**一句话结论：** `backend/core/models/factory.py` 的 **`create_chat_model()`** 是全项目**唯一推荐**的聊天模型工厂入口，统一从 DB/YAML/settings 解析配置并处理 thinking、vision、timeout 等兼容差异。

#### 背景

历史上存在 `deerflow.models.create_chat_model` 与 `backend.deerflow.models.create_chat_model` 双入口，配置散落导致「改了一个 Agent 忘了另一个」。合并为单一 factory 是 **DRY + 运维一致**。

#### 详细解释：调用方地图

**模型工厂解析（ASCII 架构图）：**

```text
┌─────────────────┐
                    │ create_chat_model│
                    └────────┬────────┘
                             │ MODEL_CONFIG_SOURCE=db
                             ▼
    ┌─────────────┐   miss   ┌─────────────┐   miss   ┌──────────┐
    │ MySQL       │ ───────► │ config.yaml │ ───────► │ settings │
    │ llm_models  │          │   models    │          │  /.env   │
    └──────┬──────┘          └─────────────┘          └──────────┘
           │
           ▼
     BaseChatModel (+ thinking patch / callbacks)
           │
    ┌──────┴──────┬────────────┬──────────────┐
    ▼             ▼            ▼              ▼
 FullAgent   Lightweight   Explore      Follow-up 服务
```



| 调用方 | 用途 |
|--------|------|
| `ProfiledAgentExecutor` | 建 LangGraph 时绑定 LLM |
| `LightweightQAAgent` | 轻量问答 |
| `FollowUpSuggestionService` | 生成追问建议 |
| `explore_learning` 服务 | 探索式学习各能力 |
| 深度研究 Draft / CoordinatedAgentWorkflow | 长流程写作与规划 |
| 其他服务层 | 任何需 `BaseChatModel` 的地方 |

```text
create_chat_model(name=...)
        │
        ▼
 MODEL_CONFIG_SOURCE=db ?
        │
   ┌────┴────┐
  db        yaml（跳过 DB）
   │          │
   ▼          ▼
 MySQL      config.yaml models
 llm_models      │
   │ miss        │ miss
   └──────┬──────┘
          ▼
    settings.py / .env
          │
          ▼
   BaseChatModel + callbacks
          ▲
          │ 热更新
    Redis 订阅（http_server lifespan）
```



**yaml 模式：**

```
MODEL_CONFIG_SOURCE=yaml  # 或非 db
  → 跳过 ①，直接 ② → ③
适合 CI、无 MySQL 的本地开发
```

#### 项目实现

`factory.py` → `_check_config_available()` + `_create_from_config()` + DB CRUD 路径（读 `db/models/llm_model.py`）。

`api/routers/models.py` 提供管理 API。

#### 具体例子

**运维场景：** DB 里 `deepseek-v4-flash` 指向旧 gateway，YAML 已更新为新 URL

```
MODEL_CONFIG_SOURCE=db
  → 实际仍走 DB 旧 URL → 502
排查：查 llm_models.base_url，而非只看 YAML
修复：更新 DB 或临时 MODEL_CONFIG_SOURCE=yaml
```

#### 常见误区

- ❌ 「改 config.yaml 一定生效」→ DB 优先时 **DB 覆盖**。
- ❌ 「.env 里 MODEL 名决定一切」→ 名在 DB/YAML，.env 主要是 **密钥与网关默认值**。
- ❌ 「热更新不需要重启」→ 取决于 Redis 推送与进程是否订阅；部分缓存可能需 restart。

#### 延伸追问（L2/L3）

- L3：Q11 DB/YAML 冲突排查步骤？

---

### Q3：`config.yaml` 里一个模型条目包含哪些关键字段？

**一句话结论：** 核心字段包括 **`name`/`display_name`、`use`（LangChain 类路径）、`model`（网关 ID）、`base_url`/`api_key`、token/温度限制、`extra_body.thinking`/`enable_thinking`、`supports_vision`/`supports_thinking`** 等能力标志。

#### 背景

YAML 条目是 DeerFlow 风格配置的延续，`use` 字段允许指向 ** patched 子类**，是厂商兼容的关键扩展点。

#### 详细解释：字段表

**config.yaml 模型条目 → factory（ASCII 架构图）：**

```text
层 1 请求（factory）：
  thinking_enabled=false（默认）
  → merge thinking.type: disabled 等

层 2 流式（StreamHandler / agent.py）：
  状态机剥离 thinking 标签 / reasoning_content
  SSE 只推「干净」answer token
```



**已知缺口：** 主站 API think 过滤与 guest **未完全对齐**（架构文档 P0-3）——面试可主动提及。

#### 项目实现

- `create_chat_model(..., thinking_enabled=...)`
- `_apply_thinking_and_profile_settings`
- `StreamHandler` / `runner.py` 内 `_THINK_*_RE` 正则
- 深度研究 runner 另有 think segment 拆分逻辑

前端可提供「深度思考」开关 → `thinking_enabled=true`。

#### 具体例子

用户关闭 thinking（默认）：

```
factory → thinking disabled
SSE → 首 token 快，无  块
账单 → 无 reasoning token
```

用户开启 thinking：

```
factory → when_thinking_enabled 块生效
SSE → StreamHandler 剥离后用户仍见干净答案（若过滤完整）
账单 → token 增加
```

#### 常见误区

- ❌ 「yaml disabled 就够了」→ 部分厂商仍漏 tag，需 **流式层**。
- ❌ 「thinking 等于 chain-of-thought prompt」→ 是**模型内置能力**，不是 Prompt 技巧。
- ❌ 「guest 与 main 一致」→ 当前是 **P0 对齐项**，可能仍有差异。

#### 延伸追问（L2/L3）

- L3：Q13 关 thinking 对质量与延迟 A/B？

---

## L2 中级 🟡

### Q5：`PatchedDeepSeekOpenAI` 为什么需要单独 patch？

**一句话结论：** DeepSeek 网关的 **reasoning 字段、响应格式、tool calling 行为**与标准 OpenAI SDK 假设不一致，patch 类在 LangChain 封装层**统一适配**，避免每个 Agent 重复 hack。

#### 背景

「OpenAI 兼容」不是字节级兼容。DeepSeek v4 等返回结构、流式 chunk、tool_calls 边界情况，直接用 `ChatOpenAI` 可能出现：reasoning 进 content、tool call 解析失败、finish_reason 异常。

#### 详细解释

**典型差异点：**

- `reasoning_content` 与 `content` 双通道
- 流式 delta 中 reasoning 与 answer 交错
- `tool_calls` JSON 不完全符合 OpenAI schema
- timeout / retry 与校内网关交互

**同类 patch：**

- `core/models/patched_deepseek_openai.py` — Patched 类处理厂商差异
- 未来新厂商 → 新 patched 类，**不改 Agent 业务代码**

#### 项目实现

```yaml
# config.yaml
use: backend.core.models.patched_deepseek_openai:PatchedDeepSeekOpenAI
```

`resolve_class(model_config.use, BaseChatModel)` 动态加载。

Patch 类应只处理**传输层/消息解析**，业务 Prompt 仍在 Agent。

#### 具体例子

DeepSeek 开 tool calling 的 full Agent：

```
无 patch：tool_call 解析 intermittent fail → Agent 循环报错
有 patch：统一转为 LangChain AIMessage.tool_calls
Allowlist 正常执行 multi_kb_search
```

#### 常见误区

- ❌ 「patch = fork 整个 LangChain」→ 通常只 override `_generate` / `_stream` 等少数方法。
- ❌ 「所有模型都要 patch」→ 标准 OpenAI/DashScope 兼容良好时用原生 `ChatOpenAI`。
- ❌ 「patch 可绕过 ToolGuardrail」→ 无关，Guardrail 在 Agent 层。

#### 延伸追问（L3）

- 如何单测 patch 类而不打真实网关？mock LLMResult 结构。

---

### Q6：`LLMPerformanceCallback` 记录了什么？

**一句话结论：** 它是挂在每个 `create_chat_model` 实例上的 **BaseCallbackHandler**，记录 **调用耗时、消息规模、TTFT、token usage、finish_reason**，用于性能排障与成本监控。

#### 背景

「慢在哪」——是 prompt 太长、网关 TTFT 高、还是 output token 多？没有 callback 只能猜。

#### 详细解释：钩子与指标

| 钩子 | 记录内容 |
|------|----------|
| `on_chat_model_start` | model 名、messages 条数、total_chars、run_id |
| `on_llm_new_token` | **首 token 时间 TTFT** |
| `on_chat_model_end` | duration ms、total_tokens、finish_reason |
| `on_chat_model_error` | 异常与 duration |

Token 提取：优先 `llm_output.token_usage`，fallback `message.usage_metadata`。

#### 项目实现

```python
# factory.py _create_from_config
perf_callback = LLMPerformanceCallback()
model_instance.callbacks = [perf_callback, ...]
```

全局 token 统计还可由 `config.yaml` → `token_usage.enabled` 控制（若项目启用聚合）。

日志前缀：`[LLMPerformance]`，便于 grep 与 request_id 关联（若 metadata 传递）。

#### 具体例子

排查「首字慢」：

```
grep "首包延迟 (TTFT)" api.log
  → TTFT 8000ms → 网关或 thinking 未关
  → total_chars 120000 → prompt 过大，查 ContextPruning
  → total_tokens 500 → 正常，问题在 input 侧
```

#### 常见误区

- ❌ 「callback 会改模型行为」→ 只读观测，无副作用。
- ❌ 「所有 LLM 调用都有 token」→ 部分网关不返回 usage，日志显示 `-`。
- ❌ 「TTFT = 整轮 SSE 时间」→ TTFT 仅**首个 answer token**，不含 tool loop。

#### 延伸追问（L3）

- Q13 如何用 callback 数据做 thinking on/off A/B？

---

### Q7：`thinking_enabled` 参数在 Agent 层如何传递？

**一句话结论：** API/Router 接收用户 **`thinking_enabled`（默认 true/false 视产品）** → 传给 Agent 构造 → **`create_chat_model(..., thinking_enabled=...)`** → factory 合并或剥离 thinking 相关 model_settings。

#### 背景

thinking 开关是**产品层**决策（质量 vs 速度），不应散落在每个 YAML 或硬编码在 Agent 里。

#### 详细解释：传递链

```
前端 Toggle「深度思考」
  → QueryRequest.thinking_enabled
  → ProfiledAgentExecutor(thinking_enabled=...)
  → ProfiledGraphBuilder.build() 内 create_chat_model(name=..., thinking_enabled=...)
  → _apply_thinking_and_profile_settings:
       thinking_enabled=True  → merge when_thinking_enabled + thinking 块
       thinking_enabled=False → 强制 disabled / 不 merge enabled 块
```

Explore / FollowUp 若传 `model_name` 也可单独 create，默认可能继承会话 thinking 策略或固定 false（视 API 设计）。

```text
Step 1: echo $MODEL_CONFIG_SOURCE
        db（默认）→ 继续查 MySQL；yaml → 跳过 DB
        │
        ▼
Step 2: SELECT * FROM llm_models WHERE name='xxx' AND is_active=1
        │
        ▼
Step 3: diff DB 行 vs config.yaml 同 name 条目
        │
        ▼
Step 4: Redis 配置变更订阅是否收到 / 多 worker 是否一致
        │
        ▼
Step 5: grep api.log create_chat_model / LLMPerformance model=
        │
        ▼
Step 6: curl 网关验证 DB 里的 model id 是否存在
```



```
Step 1: echo $MODEL_CONFIG_SOURCE
        db（默认）→ 继续查 MySQL；yaml → 跳过 DB

Step 2: SELECT * FROM llm_models WHERE name='xxx' AND is_active=1

Step 3: diff DB 行 vs config.yaml 同 name 条目
        base_url, model, max_tokens, api_key 引用

Step 4: Redis 是否缓存旧 config 版本
        http_server lifespan 订阅是否收到更新

Step 5: grep api.log create_chat_model / LLMPerformance model=

Step 6: 直接 curl 网关 with DB 里的 model id 验证网关侧是否存在
```

**常见坑清单：**

| 现象 | 原因 |
|------|------|
| YAML 改了不生效 | DB 优先 |
| 调用不存在 model id | DB 旧行 |
| 偶发新旧配置 | 多 worker 部分未收到 Redis |
| guest 与 main 不同模型 | 不同 default 或 access |

#### 项目实现

factory 全局 `_CONFIG_AVAILABLE` 缓存；异常时 fallback settings。

#### 具体例子

```
开发：删 YAML 中 foo-model
生产 DB 仍有 foo-model is_active=1
  → create_chat_model("foo-model") 仍成功
  → 网关 404 model not found
修复：DB is_active=0 或 sync YAML→DB migration
```

#### 常见误区

- ❌ 「restart 一定解决」→ Redis stale 或多环境 env 变量错误时无效。
- ❌ 「只查一个 worker 日志」→ 多 worker 配置不一致要 **全实例** grep。

#### 延伸追问

- 如何实现 model config 的 GitOps 单源真理？

---

### Q12：多模型网关（校内 aigateway + DashScope + 自建）如何统一管理？

**一句话结论：** 项目实践是 **per-model base_url + api_key 的 OpenAI 兼容客户端** + 环境变量占位；改进方向是 **统一 gateway 代理、circuit breaker、cost/latency 路由 fallback**。

#### 背景

高校常有校内 AI 网关（统一计费审计），又有 DashScope 直连、自建 vLLM。后端若硬编码单一 base_url，换供应商要改代码。

#### 详细解释

**现状模式：**

```yaml
models:
  - name: qwen-campus
    base_url: https://aigateway.university.edu/v1
    api_key: $CAMPUS_AI_KEY
    model: qwen-max
  - name: qwen-dashscope
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: $DASHSCOPE_API_KEY
    model: qwen-max-latest
```

每个 `create_chat_model(name=...)` 独立 endpoint — **简单、显式**。

**改进架构（面试加分）：**

```
                    ┌─────────────────┐
  Agents ──────────►│ Unified Gateway │──► vendor A/B/C
                    │ 路由/熔断/审计    │
                    └─────────────────┘
后端只配一个 base_url，model 名由网关路由
```

| 能力 | 价值 |
|------|------|
| Circuit breaker | 单网关挂不影响全部 Agent |
| Fallback | flash 失败 → 备用 model |
| 统一 audit log | 合规 |

#### 项目实现

patched 类 per 厂商仍在 gateway 背后或直连 DashScope 时使用。

`LLMPerformanceCallback` 可对比不同 model name 的 TTFT 做 routing 数据。

#### 具体例子

DashScope 区域故障：

```
现状：所有 qwen 条目 502 → 全站 Agent 挂
改进：gateway 自动 failover 到 campus mirror
或：factory 层 try campus name fallback（需产品定义）
```

#### 常见误区

- ❌ 「OpenAI 兼容 = 零配置切换」→ 仍要 patch thinking/tool 差异。
- ❌ 「一个 api_key 走天下」→ 审计与配额需 per-tenant key。
- ❌ 「网关解决所有 timeout」→ Agent 仍有 stream_timeouts 整轮限制。

#### 延伸追问

- Q15 600s 超时与网关超时分层关系？

---

### Q13：如何评估「关 thinking」对回答质量与延迟的影响？

**一句话结论：** 对 **TTFT、总 token、复杂题准确率、用户满意度** 做 A/B；项目默认关 thinking 偏 **UX + 成本**；垂直难题可让用户显式 `thinking_enabled=true`。

#### 背景

thinking 不是免费午餐。产品默认策略需要数据支撑，而非拍脑袋。

#### 详细解释：评估维度

| 指标 | 开 thinking | 关 thinking |
|------|-------------|-------------|
| **TTFT** | 高（先输出思考） | 低 |
| **总延迟** | 高 | 低 |
| **Token 成本** | 高（reasoning tokens） | 低 |
| **复杂推理准确率** | 可能 +5~15%（视题库） | 依赖 base 模型 |
| **流式 UX** | 需剥离，否则泄露 | 干净 |
| **工具调用正确率** | 可能略好 | 视 Prompt |

**方法论：**

1. 选 golden set（NE/CSH 各 50 题，含多步推理）
2. 固定 model，只变 thinking_enabled
3. `LLMPerformanceCallback` 采 TTFT/token
4. LLM-as-judge + 人工 spot check 准确率
5. 线上 shadow 5% 流量（若可行）

#### 项目实现

factory + API 已支持开关；缺系统化 eval pipeline（可答后续建 `datasets` 回归）。

#### 具体例子

核电子学计算题 subset：

```
thinking on  → 准确率 82%, TTFT 4.2s, cost +40%
thinking off → 准确率 76%, TTFT 0.8s
产品：默认 off，提供「深度思考」toggle 做难题
```

#### 常见误区

- ❌ 「thinking 总是提高质量」→ 简单 FAQ 无收益只有成本。
- ❌ 「只测 TTFT 不测准确率」→ 业务可能愿付延迟换正确。
- ❌ 「eval 用单次 run」→ LLM 有方差，需多次或 temperature=0。

#### 延伸追问

- thinking on 时 StreamHandler 剥离失败率如何监控？

---

### Q14：子 Agent 与主 Agent 能否用不同模型？

**一句话结论：** **架构支持**——每次 `create_chat_model(name=...)` 独立；SubagentConfig / task 派发可指定不同 `model_name`；注意 **勿跨 event loop 共享连接**，成本追踪需分 tag。

#### 背景

主 Agent 用强模型做规划，子 Agent 用 flash 做检索摘要，是常见的 **cost optimization**。但实现细节错误会导致 obscure 的 asyncio bug。

#### 详细解释

**主子 Agent 模型隔离（ASCII 架构图）：**

```text
┌────────────────┐  ┌────────────────┐
│ 主 Agent loop A │  │ task 工具        │
└────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
┌────────────────────────────┐  ┌────────────────┐
│ create_chat_model qwen-max │  │ SUB            │
└────────────────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
┌──────────────────────────────────────┐
│ create_chat_model qwen-flash         │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ LLM 网关                               │
└──────────────────────────────────────┘

```

```
主 Agent (asyncio loop A)
  create_chat_model(name="qwen-max")
        │
        │ task 工具派发
        ▼
子 Agent (subagent-exec-* 线程 + loop B)
  create_chat_model(name="qwen-flash")  ← 须在子 loop 内新建
        │
        ▼
  勿跨 loop 共享 httpx/SQLAlchemy 连接（NullPool）

成本：LLMPerformanceCallback 加 subagent tag 分账
```

**支持方式：**

```python
# 主 Agent
main_llm = create_chat_model(name="qwen-max", thinking_enabled=False)

# 子 Agent factory 内
sub_llm = create_chat_model(name="qwen-flash", thinking_enabled=False)
```

`core/tools/task_tool.py` 派发子 Agent 时可传 model 参数（视 SubagentConfig 定义）。

**注意事项：**

| 点 | 说明 |
|----|------|
| event loop | 子 Agent 常在独立线程新 loop，model 实例要在**该 loop** 创建 |
| 连接池 | httpx client 不要跨 loop 共享 |
| 成本 | callback metadata 加 subagent=name tag |
| 质量 | 子 Agent 任务简单时可 flash，写作子任务可能仍需强模型 |

**现状：** NE/CSH 主路径多数 **共用同一 model_name** — 差异化是扩展点，非默认。

#### 项目实现

深度研究 Orchestrator 可能各阶段用不同 create 调用（视实现）。

`get_kb_manager()` loop-local 与 model loop-local 是同一类问题。

#### 具体例子

```
主 Agent qwen-max 规划
  → dispatch research subagent with model=qwen-flash
  → 子 Agent 多轮 retrieve + 短摘要
  → 主 Agent 综合长文
账单：flash 占 70% 调用次数但 30% 费用
```

#### 常见误区

- ❌ 「全局单例 ChatOpenAI 给所有子 Agent」→ **跨 loop 风险**。
- ❌ 「子 Agent model 从主 Agent 自动继承」→ 需显式 Config，除非统一 factory wrapper。
- ❌ 「不同模型 context window 相同」→ max_input_tokens profile 要 per-model。

#### 延伸追问

- Registry 化后 SubagentConfig 如何声明 default_model tier？

---

### Q15：若网关 600s 超时仍不够（深度研究），怎么办？

**一句话结论：** **`request_timeout: 600` 是单次 LLM HTTP 上限**；深度研究跨多轮 LLM+工具，总时长靠 **stream 整轮超时、runner Task 生命周期、checkpoint 续跑** 组合——**不应**简单调大到无限。

#### 背景

一篇深度研究报告可能 20+ 章节，每章 2–3 次 LLM，累计远超 600s。单次 HTTP 超时解决的是**一次调用 hung**，不是**整任务时长**。

#### 详细解释：时间预算分层

| 层级 | 控制什么 | 配置/代码 |
|------|----------|-----------|
| **单次 LLM HTTP** | 一次 ainvoke/stream | `config.yaml request_timeout: 600` |
| **SSE 整轮** | 用户可见一问一答 | `stream_timeouts.py` |
| **深度研究 Task** | 后台多阶段 | `runner.py` + TaskManager |
| **前端** | 断连重连 | SSE reconnect + thread_id |

**正确做法：**

```
深度研究 = 多步 checkpoint 状态机
  每步 LLM < 600s
  步骤间 persist ResearchWorkflowState
  失败/断连 → 从 checkpoint resume
  而非 single 3600s HTTP call
```

#### 项目实现

`custom/tasks/runner.py` + `ResearchWorkflowState` + `check_cancel_flag_from_db`

Orchestrator 按章节/阶段拆分 LLM 调用。

若某一步需 >600s（极长生成），应 **拆 prompt 多段** 或 **流式累计**，而非调 HTTP timeout 到 7200。

#### 具体例子

```
章节 1 写作：LLM 480s 完成 → OK
章节 2 网关 hung：600s timeout → 标记失败，runner 重试该章
用户 SSE 断连：后台继续 vs cancel flag — 产品策略
整报告 30min：10 次独立 LLM，每次 <600s
```

#### 常见误区

- ❌ 「600s 不够就改 3600s」→ 掩盖 hung、占用连接、LB 超时。
- ❌ 「深度研究一次 LLM 写完」→ 应架构上 **多步**。
- ❌ 「timeout 与 cancel 相同」→ timeout 是时间；cancel 是用户意图。

#### 延伸追问

- checkpoint resume 如何保证章节间引用一致？
