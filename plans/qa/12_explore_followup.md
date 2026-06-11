# 12 · 探索学习与追问建议

> 📊 **图表：** 完整 ASCII 图见 [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 20、**29**；正式文档 → [explore_module.md](../../docs/docs_backend/explore_module.md) · [follow_up_suggestions_module.md](../../docs/docs_backend/follow_up_suggestions_module.md)

> 本模块答案采用**分层讲解**：先给结论，再解释「为什么」，最后用项目里的真实代码和场景串起来。

---

## L1 初级 🟢

### Q1：探索式学习 API 入口在哪？提供哪些能力？

**一句话结论：** 入口在 **`api/routers/explore.py`**，前缀 **`/api/v2/explore`**，提供**主题发现、思维导图、知识点详解（SSE）、关联资源、习题生成与评分**；支持 **`domain=ne|csh`** 切换学科。

#### 背景

RadAI / CSH-AI 需要「结构化学习路径」，而非自由聊天。Explore 是**垂直场景 API**，把学习流程产品化为可复用后端能力。

#### 详细解释：能力表

**Explore 学习路径（ASCII 架构图）：**

```text
┌────────────┐
│ 前端学习 UI    │
└────────────┘

  前端学习 UI ──────────────► EX

  EX ──────────────► topics/map JSON

  topics/map JSON ──────────────► detail SSE

  detail SSE ──────────────► resources JSON

  resources JSON ──────────────► quiz/grade JSON

  quiz/grade JSON ──────────────► SVC

  SVC ──────────────► create_chat_model

  create_chat_model ──────────────► retrieve_context

```

```
┌─────────────┐   /api/v2/explore/*   ┌─────────────────────┐
│  前端学习UI  │ ────────────────────► │ explore.py 路由      │
└─────────────┘   domain=ne|csh       └──────────┬──────────┘
                                                 │
              ┌──────────┬───────────┬───────────┼──────────┬─────────┐
              ▼          ▼           ▼           ▼          ▼         ▼
          topics/map   detail    resources    quiz      grade
          (JSON)      (SSE)       (JSON)      (JSON)    (JSON)
              │          │           │           │         │
              └──────────┴───────────┴───────────┴─────────┘
                                         │
                                         ▼
                           explore_learning.py
                           create_chat_model + retrieve_context
                                         │
                                   (非 LangGraph Agent)
```

| 端点能力 | 说明 | 响应形式 |
|----------|------|----------|
| **发现态主题** | 预制 + LLM 生成主题列表 | JSON |
| **思维导图** | 生成 / 展开节点 | JSON（树结构） |
| **知识点详解** | 流式讲解某节点 | **SSE** |
| **关联资源** | KB 检索相关 chunk/文献 | JSON + 溯源元数据 |
| **习题练习** | 选择/判断/简答生成 | JSON |
| **习题评分** | 对比标准答案评分 | JSON |

学科：`domain=ne` 核电子学；`domain=csh` 场地污染水文地质。

#### 项目实现

```python
# explore.py
router = APIRouter(prefix="/explore", tags=["explore"])
# 委托 custom/services/explore_learning.py
```

服务层统一：`create_chat_model` + `retrieve_context` grounding。

#### 具体例子

CSH 前端学习「渗透系数」：

```
POST /api/v2/explore/topics     domain=csh
POST /api/v2/explore/map        topic=渗透系数
POST /api/v2/explore/detail     SSE 流式详解
GET  /api/v2/explore/resources  返回 KB chunk 列表
POST /api/v2/explore/quiz       生成 3 选择题
POST /api/v2/explore/grade      用户作答评分
```

#### 常见误区

- ❌ 「Explore 走 Agent Router」→ **独立服务层**，非 LangGraph Agent。
- ❌ 「所有接口都是 SSE」→ 仅 **detail** 等长内容流式；主题/评分为 JSON。
- ❌ 「必须登录」→ 登录可选，访客可用（见 Q13）。

#### 延伸追问（L2/L3）

- L2：Q2 与主 Agent 区别？Q5 预制主题？
- L3：Q11 不走 LangGraph 利弊？

---

### Q2：探索学习与主 Agent 问答有什么区别？

**一句话结论：** **主 Agent** 是 LangGraph 自由多轮 + Router 选 lightweight/full/deep_research；**Explore** 是**固定学习路径**（主题→导图→详解→习题）的服务层直调 LLM+RAG，**不走 ToolAssembly、不走 Checkpointer 多轮（本期）**。

#### 背景

同一套 RAG 和模型工厂，两种产品形态：Explore 要**可控输出 schema**（导图 JSON、习题题型）；主 Agent 要**灵活工具 loop**。

#### 详细解释：对比表

| 维度 | 主 Agent `/api/v2/agent/query` | Explore `/api/v2/explore` |
|------|--------------------------------|---------------------------|
| **交互** | 自由多轮对话 | 结构化学习步骤 |
| **编排** | LangGraph + Router | `explore_learning.py` 函数 |
| **工具** | multi_kb、Skill、联网、子 Agent | 内部直接 `retrieve_context` |
| **状态** | AsyncSqliteSaver checkpointer | 前端 **localStorage**（本期） |
| **RAG** | `multi_kb_search` 工具 | 服务内 **retrieve_context** |
| **输出** | 自然语言 + artifact | JSON schema / SSE 讲解 |
| **延迟** | 可能有 tool loop | 路径确定，通常更低 |

```text
┌───────────────────────────────────────────────────────────────┐
│                     主 Agent /api/v2/agent                     │
│ Router  │  MA2  │  MA3  │  SSE chunk/done                     │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                     探索学习 /api/v2/explore                      │
│ explore.py  │  EX2  │  EX3  │  EX4  │  导图/习题/详解 JSON/SSE      │
└───────────────────────────────────────────────────────────────┘

连接:
  Router ──────► MA2
  MA2 ──────► MA3
  MA3 ──────► SSE chunk/done
  explore.py ──────► EX2
  EX2 ──────► EX3
  EX3 ──────► EX4
  EX4 ──────► 导图/习题/详解 JSON/SSE
  (RAG Service) ──────► MA3
  (RAG Service) ──────► EX3

```

```
┌─────────────────────────────┐       ┌─────────────────────────────┐
│ 主 Agent /api/v2/agent/query │       │ Explore /api/v2/explore     │
├─────────────────────────────┤       ├─────────────────────────────┤
│ Router → lightweight/full   │       │ explore.py 固定学习步骤      │
│ LangGraph 多轮 tool loop    │       │ explore_learning.py 直调    │
│ multi_kb_search 工具        │       │ retrieve_context 内联       │
│ AsyncSqliteSaver 多轮状态   │       │ localStorage 状态（本期）    │
│ SSE chunk/event/artifact    │       │ JSON 导图 / SSE 详解 / 习题  │
└──────────────┬──────────────┘       └──────────────┬──────────────┘
               │                                      │
               └──────────────┬───────────────────────┘
                              ▼
                    共用 create_chat_model + RAG Service
```

#### 项目实现

Explore 不实例化 `ProfiledAgentExecutor`；用 `create_chat_model(...).bind(...).ainvoke/astream`。

Prompt 在 `explore_prompts.py` / `explore_learning.py` 内模板化。

#### 具体例子

同一知识点「前置放大器」：

```
主 Agent：用户随意问，可能调 multi_kb + web_search，多轮追问
Explore：  主题列表选中 → 导图 6 子节点 → 点节点 SSE 详解 → 配套习题
           每步 Prompt 固定，输出 JSON 可解析
```

#### 常见误区

- ❌ 「Explore 不能多轮」→ 前端可多次调 API，但**无服务端 thread 状态**。
- ❌ 「Explore 不用 RAG」→ **强制 retrieve_context grounding**（见 Q4）。
- ❌ 「Explore 用 lightweight Agent」→ **不用 Agent 类**，直接 service。

#### 延伸追问（L2/L3）

- L3：Q11 能否沉淀为 Skill + subgraph？

---

### Q3：什么是追问建议（Follow-up Suggestions）？

**一句话结论：** 用户收到 Assistant 回复后，系统额外生成 **2–4 条推荐追问**（配置 `follow_up_suggestion_count`），降低「不知道问什么」的摩擦；由 **`FollowUpSuggestionService`** 在 SSE **`done` 前**生成并推送。

#### 背景

对话 UI 常见问题：用户读完长回答后**输入框空白**。追问建议是 **post-processing** 增强 engagement，类似搜索引擎「相关搜索」。

#### 详细解释

**追问建议时序（ASCII 架构图）：**

```text
┌─────────────────────────┐ ┌─────────────────────────┐ ┌─────────────────────────┐
│        Agent SSE        │ │FollowUpSuggestionService│ │           前端            │
└─────────────────────────┘ └─────────────────────────┘ └─────────────────────────┘


```

```
Agent SSE 流式输出 assistant 正文
        │
        ├── tool 事件 → note_process_event 累积摘要
        │
        ▼
主回答即将完成（yield done 之前）
        │
        ▼
┌─────────────────────────────┐
│ FollowUpSuggestionService   │
│ .generate(FollowUpContext)  │
└──────────────┬──────────────┘
               │
               ▼
   SSE: {"type":"follow_up_suggestions", "suggestions":[...]}
               │
               ▼
   可选 persist_follow_up_to_checkpoint
               │
               ▼
   SSE: {"type":"done", ...}
```

**触发时机：**

```
Agent SSE 流式输出 assistant 内容
  → tool 事件经 note_process_event 累积摘要
  → 主回答完成，yield done 之前
  → follow_up_service.generate(FollowUpContext)
  → SSE: type=follow_up_suggestions
  → 可选 persist_follow_up_to_checkpoint
```

```text
Explore 内容生成（detail / resources / quiz 等）
        │
        ▼
 resolve kb_ids from request
        │
        ▼
┌─────────────────────┐
│ retrieve_context    │  topic / node_title 作 query
└──────────┬──────────┘
           │
           ▼
 Top-K chunks 拼入 Prompt context 块
           │
           ▼
 create_chat_model.ainvoke / astream
           │
           ▼
 JSON 容错解析 / SSE 流式输出

map / expand 不检索 KB（纯 LLM 结构）；detail/resources 必须 grounding
```



**关联资源 API** 几乎纯 RAG 展示，LLM 只做轻量排序/摘要（视实现）。

#### 项目实现

文件头 docstring 明确：

```python
# explore_learning.py
# 所有专业内容均先经 retrieve_context 检索知识库上下文后再交由 LLM 生成
```

与 `follow_up_suggestions.py` 范式一致：factory + bind + invoke/stream。

#### 具体例子

生成「脉冲成形电路」习题：

```
retrieve_context("脉冲成形电路", kb_ids=[NE_KB])
  → 返回 8 chunks 含教材段落
Prompt: 仅基于以下上下文出题，不得编造未出现公式
  → 选择题选项可追溯到 chunk source
前端 resources 接口展示 PDF 文件名供点击
```

#### 常见误区

- ❌ 「预制主题不需要 RAG」→ 预制只用于**主题列表**；详解/习题仍 RAG。
- ❌ 「retrieve 空就硬生成」→ 应有 **fallback 模板或缩短输出**（视代码分支）。
- ❌ 「grounding = 100% 无幻觉」→ 降低概率，非数学保证；需 UI 溯源提示。

#### 延伸追问（L3）

- Q14 习题 AI 评分与 grounding 关系？

---

## L2 中级 🟡

### Q5：预制主题（PRESET_TOPICS）为什么存在？

**一句话结论：** 按学科内置 **PRESET_TOPICS** 清单，实现**零 LLM 延迟**的主题发现、**保证核心课节点覆盖**、**避免冷启动超时**；与 LLM 动态生成（带 `seed`）**并存**。

#### 背景

打开 Explore 首页若等 LLM 生成 20 个主题，TTFT + JSON 解析可能 5–15s，移动端体验差。核电子/CSH 课程大纲相对固定，预制是合理优化。

#### 详细解释

```python
# explore_learning.py
PRESET_TOPICS: dict[str, list[dict[str, str]]] = {
    "核电子学": [
        {"title": "前置放大器", "hint": "微弱信号放大与阻抗匹配"},
        ...
    ],
    "场地污染水文地质": [
        {"title": "渗透系数", "hint": "测定方法与影响因素"},
        ...
    ],
}
```

**价值：**

| 价值 | 说明 |
|------|------|
| 即时返回 | 无 LLM 调用 |
| 覆盖保障 | 核心考点不依赖模型随机性 |
| 超时规避 | TIMEOUT_MAP 等 LLM 超时不影响预制列表 |
| 学科切换 | `resolve_subject(domain)` 选对应清单 |

用户传 `seed` 时仍可 LLM 生成**补充**主题，与预制 merge 或 replace（视 API 逻辑）。

#### 项目实现

`resolve_subject(domain)` → `SUBJECT_BY_DOMAIN` 映射学科名 → 取 PRESET_TOPICS 键。

NE 约 28 条、CSH 约 28 条预制（代码内列表）。

#### 具体例子

```
GET topics?domain=ne&count=20
  → 立即返回预制前 20 条（或 shuffle）
GET topics?domain=ne&seed=半导体探测器
  → LLM 生成聚焦主题 + 可能合并预制
```

#### 常见误区

- ❌ 「预制 = 静态无 LLM」→ 仅**主题发现**层；导图/详解仍 LLM。
- ❌ 「预制主题无 hint」→ 每条含 title+hint 助 UI 展示。
- ❌ 「CSH 用 NE 预制」→ domain 必须传 csh。

#### 延伸追问（L3）

- 预制与 KB 内容不一致时如何更新 PRESET_TOPICS 版本？

---

### Q6：`FollowUpContext` 包含哪些字段？如何影响追问质量？

**一句话结论：** **`FollowUpContext`** 携带用户问题、助手回答、**tool_summary**（工具执行摘要）、agent_mode、skills、model、locale 等；其中 **tool_summary 使追问绑定刚检索的论文/证据**，而非仅 paraphrase 最终 answer。

#### 背景

若追问只基于 answer 文本，会漏掉「Agent 调了 multi_kb 找到但压缩进回答的文献」。tool_summary 是 **process 事件的蒸馏**。

#### 详细解释：字段表

```python
@dataclass
class FollowUpContext:
    user_question: str          # 用户原问（truncate 500）
    assistant_answer: str       # 助手最终回答（truncate 2000）
    tool_summary: str = ""      # 工具上下文摘要（truncate 500）
    agent_mode: str = "normal"  # normal / deep_research 等
    selected_skills: list[str] | None = None
    model_name: str | None = None
    locale: str = "zh-CN"
    use_llm: bool = True
```

**tool_summary 来源：** `note_process_event` 监听 SSE payload：

- `kind in {tool_end, tool_result}`
- 提取 `title`、`summary`、`detail`、`metadata.papers/results/...`
- 最多保留 12 条，格式 `[multi_kb_search] 标题 | 摘要`

#### 项目实现

```python
# agent.py 流式循环内
follow_up_service.note_process_event(event_payload)
...
suggestions, sug_meta = await follow_up_service.generate(
    FollowUpContext(
        user_question=...,
        assistant_answer=...,
        tool_summary=follow_up_service.build_tool_summary(),
        ...
    )
)
```

Prompt 构建见 `build_follow_up_prompt(ctx)`。

#### 具体例子

| 有 tool_summary | 无 tool_summary |
|-----------------|-----------------|
| 「能否深入解读《XXX》一文中的实验方法？」 | 「能否提供更多细节？」 |
| 追问含具体论文名 | 追问 generic |

#### 常见误区

- ❌ 「tool_summary = 完整 ToolMessage」→ 是**短摘要**，非原文。
- ❌ 「deep_research 无 tool 事件」→ 可能有 process 事件，仍 note。
- ❌ 「locale 未使用」→ Prompt 可据此生成中文追问（视 prompt 实现）。

#### 延伸追问（L3）

- Q9 persist checkpoint 后 tool_summary 是否还需存？

---

### Q7：追问建议的 JSON 解析失败怎么办？

**一句话结论：** 使用 **正则提取 JSON 数组** / `{"suggestions": [...]}` 结构；解析失败 → **模板 fallback**（generic 追问）；**绝不**因 LLM 输出非 JSON 导致前端崩溃。

#### 背景

小模型生成「追问建议」常包裹 markdown fence 或前后废话。严格 `json.loads` 全篇会 flaky。

#### 详细解释

```python
# follow_up_suggestions.py
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*?\]")
_SUGGESTIONS_OBJ_RE = re.compile(r'\{\s*"suggestions"\s*:\s*\[[\s\S]*?\]\s*\}')
```

**流程：**

```
LLM 返回文本
  → 尝试 extract JSON array / suggestions 对象
  → json.loads + 校验 list[str]
  → 失败：metadata.source = "fallback"
  → 返回模板句：「能否展开说明…」「有无相关应用案例？」等
  → latency 仍记录 sug_meta
```

与 `explore_learning.py` 的 `_JSON_OBJ_RE`、`_FENCE_RE` **同范式**。

#### 项目实现

`generate()` 返回 `(suggestions: list[str], metadata: dict)`，metadata 含 `source: llm|fallback|skipped`。

空 answer → `skipped`，不发追问。

#### 具体例子

LLM 返回：

```
以下是追问建议：
```json
["问题1", "问题2"]
```
```

正则抽 `[...]` → 解析成功。

若返回纯 prose → fallback 3 条 generic。

#### 常见误区

- ❌ 「fallback 不上 SSE」→ 仍推送，前端无感降级。
- ❌ 「retry LLM 三次」→ 当前偏 **fast fallback**，非 retry 链。
- ❌ 「explore 与 follow_up 解析代码重复」→ 有意一致，未来可抽 util。

#### 延伸追问

- 是否用 structured output / tool calling 替代 regex？trade-off？

---

### Q8：探索详解为什么用 SSE 而不用普通 JSON？

**一句话结论：** 知识点详解**篇幅长**，SSE **流式**降低首字延迟、改善「打字机」UX，与主 Agent **体验一致**；短接口（主题、评分）仍用 JSON。

#### 背景

一节点详解可能 1500–3000 字。等 LLM 全生成再 JSON 返回，用户白屏 10–20s。流式首 token 通常 <2s。

#### 详细解释

**SSE 路径：**

```
POST /explore/detail
  → StreamingResponse
  → explore_learning.astream_detail(...)
  → create_chat_model.astream
  → yield data: {"type":"chunk","content":"..."}\n\n
  → 结束 data: {"type":"done"}\n\n
```

**JSON 路径：** topics、map、quiz、grade — 结构小、需一次性 parse。

#### 项目实现

`explore.py` 中 detail 路由返回 `StreamingResponse`，media type `text/event-stream`。

超时：`TIMEOUT_MAP = 25.0` 等 per-capability。

#### 具体例子

用户点击导图节点「CR-RC 成形」：

```
0.5s  首 chunk 到达前端开始渲染
3s    已显示前两段
8s    流结束 done
若用 JSON：8s 内 UI 无任何内容
```

#### 常见误区

- ❌ 「Explore 全部 SSE」→ **仅长内容**流式。
- ❌ 「SSE = Agent SSE 协议完全相同」→ 事件 type 字段可能简化，但格式类似。
- ❌ 「流式不能 grounding」→ retrieve 在 stream **之前**完成。

#### 延伸追问

- detail SSE 中断连如何 resume？

---

### Q9：追问建议是否写入 Checkpointer？

**一句话结论：** **`persist_follow_up_to_checkpoint()`** 将 suggestions 写入 thread state **metadata**，刷新页面后仍可展示；转换逻辑在 **`agent.py` 的 `suggestions_to_metadata_items`**。

#### 背景

用户刷新浏览器，若追问仅存 SSE 一次推送则丢失。Checkpointer 持久化使 **conversation UI 与历史一致**。

#### 详细解释

```
SSE follow_up_suggestions 事件
  → persist_follow_up_to_checkpoint(
        checkpointer, thread_id, suggestions, metadata
    )
  → LangGraph state metadata 或 messages 附加项
前端 reload thread
  → conversations API 读 checkpoint
  → 渲染上次追问 chips
```

与 assistant message 绑定时机：通常在 **done 之前或之后**立即 persist（见 agent.py 实现）。

#### 项目实现

`api/routers/agent_runtime.py` import `persist_follow_up_to_checkpoint`。

Explore **本期不** persist 学习进度到 checkpointer（见 Q13），追问仅 Agent 路径。

#### 具体例子

```
用户手机切后台，SSE 断
重开对话页
  → checkpoint 含 follow_up metadata
  → 仍显示 3 条推荐追问可点击
```

#### 常见误区

- ❌ 「追问进 messages 列表」→ 多在 **metadata** 或专用字段，非 HumanMessage。
- ❌ 「Explore 也 persist follow_up」→ 否。
- ❌ 「guest checkpoint 不 persist」→ guest 用 `guest_checkpoints.db`，若走同 API 则 persist。

#### 延伸追问

- metadata 过大是否影响 checkpoint 体积？

---

### Q10：`SUBJECT_BY_DOMAIN` 映射的作用？

**一句话结论：** 前端传 **`domain=ne|csh`** → **`SUBJECT_BY_DOMAIN`** 映射学科中文名 → 驱动 **Prompt、PRESET_TOPICS、Explore RAG 绑定**，实现**一套 explore 服务支撑 RadAI + CSH-AI**。

#### 背景

Explore 代码不应复制两份。domain 是**产品标识**，subject 是**Prompt 里的人可读学科名**。

#### 详细解释

```python
SUBJECT_BY_DOMAIN: dict[str, str] = {
    "ne": "核电子学",
    "csh": "场地污染水文地质",
}

def resolve_subject(domain, subject=None):
    if subject: return subject.strip()
    return SUBJECT_BY_DOMAIN.get(domain.lower(), DEFAULT_SUBJECT)
```

**影响范围：**

| 模块 | 如何使用 subject |
|------|------------------|
| PRESET_TOPICS | dict key = 学科名 |
| Prompt 模板 | 「你是一位{subject}教授…」 |
| 默认 KB | 可结合 settings 或 kb_ids |
| 日志 | 区分 NE/CSH 流量 |

#### 项目实现

`explore.py` 各 Request 模型含 `domain: str = "ne"`。

`DEFAULT_SUBJECT` 可 settings 覆盖 `explore_subject`。

#### 具体例子

```
RadAI 前端：domain=ne  → subject=核电子学  → 预制「前置放大器」
CSH-AI：   domain=csh → subject=场地污染水文地质 → 预制「渗透系数」
同一 deploy 后端，不同 domain 参数
```

#### 常见误区

- ❌ 「domain 自动选 kb_ids」→ 仍需传 kb_ids 或 settings 默认。
- ❌ 「subject 可任意字符串绕过」→ API 可显式传 subject 覆盖（需鉴权防滥用）。
- ❌ 「ne/csh 是 Router mode」→ domain **仅 Explore**，与 Agent lightweight/full 无关。

#### 延伸追问

- 第三学科接入：加 PRESET_TOPICS 键 + SUBJECT_BY_DOMAIN 条目即可？

---

## L3 高级 🔴

### Q11：探索服务不走 LangGraph，利弊是什么？

**一句话结论：** **优点**：延迟低、路径确定、Prompt 输出 schema 可控、易单测；**缺点**：无法复用 ToolAssembly/Subagent/ContextPruning、复杂中途换意图需新状态机、与主 Agent **RAG 调用两套入口**。

#### 背景

架构取舍题：Explore 用 LangGraph 会更「统一」，但会引入 tool loop 不确定性。当前选择 **service-oriented** 快速交付教学场景。

#### 详细解释

**优点详述：**

| 优点 | 说明 |
|------|------|
| 低延迟 | 无 Agent 自主 multi_kb 循环 |
| 确定性 | map→detail→quiz 固定调用序 |
| 可测 | mock retrieve_context + mock LLM 单测函数 |
| 运维简 | 无 checkpointer 并发写问题 |

**缺点详述：**

| 缺点 | 说明 |
|------|------|
| 能力重复 | retrieve_context 与 multi_kb 逻辑平行 |
| 无 Skill | 不能用户选 html-ppt Skill 做 explore 课件 |
| 无 Subagent | 长讲解不能派 research 子 Agent |
| 无 ContextPruning | 长 retrieve 全进单次 Prompt |
| 交互 rigid | 用户中途「换话题」需前端状态机 |

**演进：** 高频 explore 能力 → **Skill + 专用 LangGraph subgraph**（见 06-Q11 SkillWorkflowExecutor）。

#### 项目实现

`explore_learning.py` 直接 `from backend.core.models.factory import create_chat_model`。

不经过 `ToolAssemblyPipeline` / `ToolGuardrailMiddleware`（内部 retrieve 无 tool_call 面）。

#### 具体例子

**若 Explore 用 LangGraph：**

```
用户点节点 A 讲解中途问「顺便对比一下 B」
  → ReAct Agent 可能 web_search 跑偏
  → 输出不再符合习题 JSON schema
Service 版：  需新 API「对比模式」，或拒绝自由对话
```

#### 常见误区

- ❌ 「Explore 完全无 Agent 技术」→ 仍用 **LLM + RAG + SSE**，只是无 LangGraph。
- ❌ 「应立刻迁 LangGraph」→ 视产品是否需要自由对话。
- ❌ 「两套 RAG 必然不一致」→ 应共享 `retrieve_context` 底层（已共享 Manager）。

#### 延伸追问

- Explore subgraph  handoff 回主 Agent 的产品设计？

---

### Q12：追问建议使用同一 model 还是轻量 model？

**一句话结论：** **`FollowUpContext.model_name` 可传 API 所选模型**；未传则用 factory default；**优化方向**是用 flash 等小模型（任务简单），**当前倾向与主对话同模型**保一致性。

#### 背景

追问生成是短 JSON 列表，用大模型「杀鸡用牛刀」。但模型不一致可能导致追问风格与回答脱节。

#### 详细解释

**Trade-off 表：**

| 策略 | 优点 | 缺点 |
|------|------|------|
| **同主模型** | 风格一致、理解 answer 更深 | 延迟+cost 略高 |
| **flash 轻量** | 快、便宜 | 可能忽略 tool_summary 细节 |
| **规则/template** | 零 cost | 质量差，仅 fallback |

**实现钩子：**

```python
create_chat_model(name=ctx.model_name or default)
# settings 可加 FOLLOW_UP_MODEL_NAME 覆盖
```

#### 项目实现

`follow_up_suggestions.generate` 内 create_chat_model；truncate 输入控 token。

评估应用 offline：同 100 answer 对比 LLM vs flash 的点击率（若前端埋点）。

#### 具体例子

```
主对话 qwen-max 长回答 2000 字
追问用 qwen-flash：200ms 完成，建议仍含论文名 → 可接受
追问用 template fallback：0ms，generic → 兜底
```

#### 常见误区

- ❌ 「追问一定用 default model」→ API 可传 model_name。
- ❌ 「flash 必错」→ 短任务常足够，需 A/B。
- ❌ 「追问不计 token 成本」→ 每次 generate 都是独立 LLM call。

#### 延伸追问

- 能否用主模型 answer 的结构化 summary 代替二次 LLM？

---

### Q13：探索模式「不做服务端持久化」的含义与风险？

**一句话结论：** `explore.py` 注释：**登录可选，学习进度由前端 localStorage 保存**；后端**不建 explore_sessions 表**——换设备/清缓存丢进度，但降低存储与 GDPR 复杂度。

#### 背景

Explore 面向访客学习 + 快速 MVP。全量服务端持久化要设计 session、进度同步、隐私同意。

#### 详细解释

**本期架构：**

```
前端 state：当前主题、导图展开节点、习题得分
  → localStorage.setItem('explore_progress', ...)
后端：无 explore session API（注释明确）
登录用户：auth 可记录 user，但不 persist 进度到 DB
```

**风险：**

| 风险 | 影响 |
|------|------|
| 换设备 | 进度丢失 |
| 清缓存 | 进度丢失 |
| 无学习分析 | 无法 dashboard 完成率 |
| 合规 | 访客数据少，GDPR 轻 |

**好处：**

- 后端简单
- 访客即用
- 无 session 存储成本

**若产品要学习记录：**

```
新增 explore_sessions / explore_progress 表
同步 API：POST /explore/progress
与 User 关联，支持多端
```

#### 项目实现

`explore.py` 文件头 docstring；`get_current_user` 可选 Depends。

Checkpointer **不**用于 explore 路径（与 Agent Q9 对比）。

#### 具体例子

学生在机房浏览器学完半章 → 回家用自己电脑 → **进度不在** → 需重做或前端 export/import（若实现）。

#### 常见误区

- ❌ 「登录就有云端进度」→ **本期无**，除非前端另接别的 API。
- ❌ 「Explore 不写任何 DB」→ 关联 resources 仍读 KB MySQL。
- ❌ 「与 guest Agent checkpoint 相同」→ guest **Agent** 有 checkpoint；**Explore 无**。

#### 延伸追问

- localStorage 存 KB 检索结果是否涉版权/cache 合规？

---

### Q14：习题自动评分的可信度如何保障？

**一句话结论：** **客观题优先规则校验**；**简答题**用 LLM 对比参考要点（非单一标准答案）；**题目范围 RAG grounding**；前端标注 **「AI 评分仅供参考」**——主观题无法数学保证公正。

#### 背景

Explore 习题是 LLM 生成 + LLM 评分，双重不确定性。产品需管理用户预期与教学合规。

#### 详细解释

**生成阶段（quiz）：**

```
retrieve_context(node_title)
  → Prompt：仅基于上下文出选择/判断/简答
  → 标准答案附参考要点列表
```

**评分阶段（grade）：**

```
客观题（choice/judge）：字符串/选项 index 规则匹配 → 高可信
简答题：LLM 对比 user_answer vs reference_points
  → 给分 + 短评
  → TIMEOUT_GRADE = 25s
```

**缓解措施：**

| 措施 | 说明 |
|------|------|
| 题型优先 | choice/judge 占默认 types 前列 |
| 参考要点 | 简答非 single gold answer |
| Grounding | 超 KB 范围的题减少 |
| UI 免责声明 | AI 评分仅供参考 |
| 人工复核 | 高利害考试不应纯 AI 评 |

#### 项目实现

`explore_learning.py` quiz/grade 函数；`QuizRequest.types` 默认 `["choice", "judge", "short"]`。

`explore.py` grade 端点返回 score + feedback JSON。

#### 具体例子

```
判断题「CR-RC 成形提高信噪比」用户选 T
  → 规则对比 answer_key → 100% 可信

简答题「描述 P&T 修复原理」
  → LLM 看是否覆盖「抽取、处理、回灌」要点
  → 给 8/10 + 「未提及监测井」
  → 教师可不同意，UI 已 disclaimer
```

#### 常见误区

- ❌ 「AI 评分 = 标准答案」→ 简答是 **rubric 匹配**。
- ❌ 「grounding 保证题正确」→ 生成仍可能误解 chunk。
- ❌ 「grade 走 SSE」→ 通常 **JSON 一次返回**。

#### 延伸追问

- 能否用 embedding 相似度代替 LLM grade 降成本？

---

### Q15：主 Agent 的「意图识别」与 Explore 主题生成有何异同？

**一句话结论：** **主 Agent** 用 `_normalize_intent_payload` 分 **`deep_research` / `normal_qa` / `clarification_needed`** 驱动 **Router 与状态机**；**Explore 主题**是 **预制列表或 LLM 内容策展**，**不做任务型意图分类**，二者同用 `create_chat_model` 但**产品目标不同**。

#### 背景

都涉及「理解用户想干什么」，但 Agent 意图是 **routing**，Explore 主题是 **content discovery**。

#### 详细解释

**主 Agent 意图（agent.py）：**

```
用户消息 + 上下文
  → 意图模型/规则 → intent payload
  → _normalize_intent_payload
       deep_research → Orchestrator + 大纲状态机
       normal_qa     → lightweight/full Router
       clarification_needed → 反问用户
  → 影响 kb、skills、SSE 形态
```

**Explore 主题（explore_learning.py）：**

```
domain + optional seed
  → PRESET_TOPICS 和/或 LLM 生成 title+hint 列表
  → 无意图三分法
  → 不触发 deep_research workflow
  → 用户选中主题后才进入 map/detail/quiz 流水线
```

**同：** 都用 factory、都可 RAG、都服务 NE/CSH 域。

**异：**

| 维度 | Agent 意图 | Explore 主题 |
|------|------------|--------------|
| 输出 | 路由决策 | 学习主题列表 |
| 失败 | clarification | fallback 预制 |
| 状态 | checkpointer | localStorage |
| 深度 | 可 deep_research | 固定浅路径 |

#### 项目实现

Agent：`api/routers/agent_runtime.py` 意图相关 helper。

Explore：`generate_topics()` 等，seed 聚焦方向。

#### 具体例子

```
用户 Agent 问：「帮我做一份核电子学深度调研报告」
  → intent: deep_research → 大纲 → runner

用户 Explore 选 seed=「闪烁探测器」
  → 生成 10 个相关学习主题
  → 无意图分类，不会启动 30min 研究报告任务
```

#### 常见误区

- ❌ 「Explore seed = Agent task_type」→ 无 deep_research 等价物。
- ❌ 「意图识别用同一 Prompt 模板」→ 完全分离。
- ❌ 「预制主题替代意图」→ 仅 Explore 发现页；Agent 仍要意图。

#### 延伸追问

- 是否用 Agent lightweight 统一做 Explore 主题生成（延迟 trade-off）？
