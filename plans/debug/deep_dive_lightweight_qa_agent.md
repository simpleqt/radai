# 深度审查：`lightweight_qa_agent.py` 函数级拆解

> 文件：`backend/custom/agents/lightweight_qa_agent.py`（**1349 行**）  
> 只读审查，未改代码。

---

## 1. 在架构中的位置

```text
┌────────────────┐  ┌──────────────────────────┐
│ guest/query    │  │ POST /api/v2/agent/query │
└────────────────┘  └──────────────────────────┘
           │
           ▼
           │
           ▼
┌────────────────┐  ┌────────────────┐
│ H              │  │ B              │
└────────────────┘  └────────────────┘
           │
           ▼
         (无 selected_skills)
           │
           ▼
         (有 skills)
           │
           ▼
┌────────────────┐  ┌────────────────────────────────────────────────┐
│ C              │  │ NEFullFeatureAgent full + NEAgentStreamHandler │
└────────────────┘  └────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ LightweightQAAgent.aquery_stream     │
└──────────────────────────────────────┘

```

| 入口 | 是否走 LightweightQAAgent |
|------|-------------------------|
| `NEAgentRouter` → `mode=lightweight` | ✅ 经 `NEFullFeatureAgent._lightweight_mode` 委托 |
| `guest.py` | ✅ 同上 |
| `NEFullFeatureAgent` 全功能 + skills | ❌ 走 LangGraph + StreamHandler |
| 直接 import 类 | 仅 `ne/csh_full_feature_agent` 内嵌调用 |

**结论**：该文件是「无 Skill 轻量通道」的核心实现，但 **1349 行仍属 God file**，与架构文档「轻量通道保留」一致，需继续拆分。

---

## 2. 模块级符号（L1–445）

| 符号 | 行约 | 职责 | 质量 |
|------|------|------|------|
| `_compress_cache` / `_compress_tasks` | 27–28 | 线程级历史压缩缓存（下轮生效） | global 进程状态，多 worker 各一份 |
| `_get_thread_id` | 31–33 | 从 RunnableConfig 取 thread_id | ok |
| `LIGHTWEIGHT_SYSTEM_PROMPT` | 39–46 | 核电子学领域 system 提示 | 长常量，宜外置 `.md` |
| `LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT` | 48–122 | 结构/图片/公式/引用模板 | **~75 行**，修改需改代码 |
| `_make_process_event` | 129–157 | 本地版过程事件（与 `stream_utils` 重复） | **duplication** |
| `_stringify_message_content` | 160–177 | 解析 AIMessage content | ok |
| `_extract_reasoning_content` | 179–192 | 提取 reasoning 字段 | ok |
| `_summarize_text` | 194–198 | 日志截断 | ok |
| `_strip_thinking_markup` | 200–206 | 单次剥离 think 标签 | 与 SSE 层 6 处实现并列 |
| `_clean_question` | 208–226 | 去噪用户问题 | ok |
| `_is_skip_kb_decision` | 228–238 | 解析 SKIP_KB 决策 | ok |
| `_sanitize_search_query` | 241–252 | 清洗检索词 | ok |
| `_display_search_detail` | 255–260 | UI 展示用检索词 | ok |
| `_trim_prompt_context` | 263–271 | 截断 prompt | ok |
| `_compact_kb_context` | 274–291 | 压缩 KB 上下文 | ok |
| `_parse_file_search_terms` | 294–329 | 解析文件检索词 JSON | ok |
| `_extract_file_search_terms` | 332–443 | **async** LLM 生成文件检索词 | 宽泛 except；依赖外部 model |

---

## 3. `class LightweightQAAgent`（L446–1349）

### 3.1 构造与常量

| 成员 | 说明 |
|------|------|
| `STREAM_HEARTBEAT_INTERVAL_SECONDS = 15` | 与 full agent 空闲心跳一致 |
| `__init__` | 持有 `CompiledStateGraph`、kb_ids、use_search、thinking_enabled、thread_id 等 |

**问题**：传入 `graph` 但轻量路径主要用 `create_chat_model().astream(messages)`，graph 仅用于 `_load_recent_messages` / checkpoint，职责混杂。

---

### 3.2 `aquery_stream`（L475–1035）— 主管线 ~560 行

**建议拆为 5 个阶段函数**：

| 阶段 | 行约 | 逻辑 | 发出 SSE type |
|------|------|------|----------------|
| **A. 初始化** | 485–495 | status；计时 | `status` |
| **B. 历史** | 489–495 | `_load_and_compress_history` | — |
| **C. 检索词** | 497–558 | skip_kb、file 检索词 LLM | `status` |
| **D. 并行检索** | 560–770 | asyncio.gather KB/Web/File | `status`, `event`(tool_start/end) |
| **E. Prompt 构建** | 772–896 | 拼 system/human/图片 | `status` |
| **F. 流式生成** | 898–996 | 队列 + heartbeat + chunk/thinking | `ping`, `thinking`, `chunk` |
| **G. 收尾** | 998–1035 | 存历史、压缩、metrics | `event`(kind=answer) |

#### 阶段 D 细节

- 嵌套 `_do_kb_search` / `_do_web_search` / `_do_file_search`
- `skip_kb` 时仅文件检索，仍发 `multi_kb_search` 相关 event 需注意（已用 `not skip_kb` 守卫）
- 检索失败：`except Exception` 记入日志，**不向前端发 error 事件**

#### 阶段 F 细节（与 SSE 协议相关）

| 行为 | 协议 |
|------|------|
| 15s 无 chunk → `yield {"type": "ping", "timestamp": ms}` | agent.py 转为 `: ping {ts}\n\n` comment |
| reasoning → `type: thinking` | ✅ 与文档一致 |
| 正文 → `type: chunk` | ✅ |
| `StreamingTokenResolver` 处理 `[IMAGE:...]` | 依赖 image_url_registry |

**未发出**：`draft_chunk`、`follow_up_suggestions`（由 API 层 `agent.py` 在流结束后生成）

---

### 3.3 检索与历史方法

| 方法 | 行约 | 职责 | 问题 |
|------|------|------|------|
| `_extract_search_query_v2` | 1040–1168 | LLM 决策检索词 + 是否 skip_kb | ~130 行；timeout/except 回退原问题 |
| `_run_kb_search` | 1171–1183 | 调 multi_kb / retrieve | 同步包装 async 工具？需读实现 |
| `_run_web_search` | 1185–1196 | 联网搜索 | except 吞掉 |
| `_load_recent_messages` | 1199–1220 | 从 graph checkpoint 读历史 | except 返回 [] |
| `_format_messages` | 1222–1241 | 格式化进 prompt | ok |
| `_load_and_compress_history` | 1244–1270 | 读历史 + 压缩缓存 | ok |
| `_trigger_async_compression` | 1272–1280 |  fire-and-forget 压缩任务 | 竞态：同 thread 并发请求 |
| `_do_background_compression` | 1282–1324 | 后台 LLM 压缩 | 双模型 fallback；except 多 |
| `_save_turn` | 1330–1349 | 写入 checkpoint | except warning |

---

## 4. 与 `NEAgentStreamHandler` 的行为差异

| 维度 | LightweightQAAgent | NEAgentStreamHandler |
|------|-------------------|----------------------|
| 工具调用 | 代码内直接 await 检索，**无 LangGraph tool** | `on_tool_start/end` 转 event |
| process event | 手写 `_make_process_event` | `make_process_event` |
| 深研/子 agent | 不支持 | task_*、artifact、live markdown |
| 输出上限 | 无 `max_output_chars` 截断 | 有 NORMAL_MAX_OUTPUT_CHARS |
| think 泄漏 | 依赖 `thinking` 字段 + guest 才 split chunk | handler 可发独立 thinking |

---

## 5. 质量问题汇总

| 严重度 | 问题 |
|--------|------|
| **high** | 单文件 1349 行，`aquery_stream` ~560 行 |
| **medium** | `_make_process_event` 与 `stream_utils` 重复 |
| **medium** | 模块级 `_compress_cache` 无跨 worker 一致性说明 |
| **medium** | 检索/压缩多处 `except Exception`，前端无 error 事件 |
| **low** | `logging.getLogger` 非 `get_logger` |
| **low** | 超长 prompt 常量内嵌 |
| **low** | `graph` 参数命名易误解（非 graph.astream 主路径） |

---

## 6. 建议拆分目录（设计）

```
custom/agents/lightweight/
  prompts.py           # SYSTEM + ANSWER_TEMPLATE
  text_utils.py        # clean, strip, compact, sanitize
  search_query.py      # _extract_search_query_v2, file terms
  retrieval.py         # kb/web/file 并行检索 + events
  stream.py            # aquery_stream 编排
  history.py           # load/compress/save_turn
  agent.py             # LightweightQAAgent 薄壳
```

---

## 7. 建议单测锚点

1. `_is_skip_kb_decision` / `_parse_file_search_terms` 纯函数  
2. `_extract_search_query_v2` mock LLM 返回 SKIP_KB  
3. `aquery_stream` 集成：无 kb、仅 file index、skip_kb 三路  
4. heartbeat 15s 发出 ping 字典（经 API 转 comment）
