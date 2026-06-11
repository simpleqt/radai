# 深度审查：custom/api/v2（Agent SSE 层）

> 只读。对照 [sse_events_module.md](../../docs/docs_backend/sse_events_module.md) 与 [agent_architecture_redesign.md](../../docs/agent_architecture_redesign.md)。

---

## 1. 模块结构

```
custom/api/v2/
├── __init__.py          # 挂载 agent / guest / csh_agent / explore
├── agent.py             # POST /api/v2/agent/query  (NE/RadAI)  1354 行
├── csh_agent.py         # POST /api/v2/csh_agent/query          1243 行
├── guest.py             # 访客流式                             204 行
└── explore.py           # 探索学习流式                         271 行
```

| 端点 | 文件 | 智能体入口 |
|------|------|------------|
| `/api/v2/agent/query` | `agent.py` | `NEAgentRouter` → `NEFullFeatureAgent` |
| `/api/v2/csh_agent/query` | `csh_agent.py` | `CSHAgentRouter` → `CSHFullFeatureAgent` |
| `/api/v2/guest/query` | `guest.py` | 轻量/访客路径 |
| `/api/v2/explore/*/stream` | `explore.py` | `explore_learning` 服务 |

---

## 2. agent.py 与 csh_agent.py 重复度分析

| 指标 | 值 |
|------|-----|
| agent.py 行数 | 1354 |
| csh_agent.py 行数 | 1243 |
| **行级相似度** | **91.3%** |
| 完全相同行 | 1186 |
| 替换块 | 65 行量级 |
| 删除块（仅 agent 有） | 103 行 |

### 2.1 共享符号（应抽到 `v2/_common.py`）

两文件均定义且实现相同：

- `_coerce_str_list`, `_normalize_intent_payload`, `_normalize_plan_payload`
- `_make_process_event`, `_build_runtime_time_context`, `_inject_time_context`
- `_ensure_conversation_record`, `_persist_research_workflow_state`
- Pydantic：`QueryRequest`, `SkillInfoResponse`, `ModelOptionResponse`, `KbOptionResponse`, `AgentInfoResponse`, `AgentSkillsResponse`, `TaskStatusResponse`
- `_get_agent_default_kb_ids`, `_fetch_kb_options`
- 路由：`get_agent_info`, `get_available_skills`, `query_agent`, `get_task_status`, `stream_task_events`, `cancel_task`, `get_thread_active_task`

### 2.2 仅 agent.py 存在（差异 / 债务）

| 区域 | 行约 | 内容 | 风险 |
|------|------|------|------|
| think-tag 工具函数 | 244–295 | `_extract_possible_tag_prefix`, `_split_thinking_segments` | **死代码**：`query_agent` 流循环未调用；`in_thought`/`think_carry` 仅初始化 |
| 深研工作流 SSE | 942–1004 | `set_workflow_state` 与 clarification/report 状态机 | CSH 侧简化或缺失时需确认产品一致性 |
| 完成日志 | 1015–1018 | `forwarded_text_chars < 300` 告警 + NEAgentStreamHandler 提示 | CSH 无同等诊断 |

### 2.3 仅 csh_agent.py 差异

- 无 `_split_thinking_segments`（与 agent 「有但未用」形成不对称）
- 日志前缀 `[CSH-Agent]` vs `[Agent V2]`
- import：`CSHAgentRouter` / `CSHFullFeatureAgent` vs NE 系列

### 2.4 建议抽取的公共模块

```
custom/api/v2/
  _sse_common.py      # think-tag（若恢复使用）、process_event、time_context
  _conversation.py    # ensure/persist conversation + research_workflow
  _schemas.py         # 全部 Pydantic 模型
  _task_routes.py     # status/stream/cancel/active_task 四端点
  agent.py            # 仅 NE 特有：draft 流程、workflow_state、告警
  csh_agent.py        # 薄包装：router + agent 类 + prefix
```

**预估**：合并后可减少 **~1100 行**重复，修复时只需改一处。

---

## 3. query_agent 函数级职责（单函数 ~650 行嵌套）

`query_agent` → 内部 `stream_generator()` 承担：

1. 创建 `stream_session`（失败仅 warning）
2. 并行 `_ensure_conversation_record`
3. `ResearchDraftAgent` + 深研草案/确认分支
4. `get_checkpointer_from_request`
5. 后台 `run_deep_research_background` 或实时 `NEFullFeatureAgent.aquery_stream`
6. 上传文件注入（short_doc / long_doc / multimodal）— **与 csh 逐行重复 ~80 行**
7. SSE 事件转发（chunk/status/ping/thinking/event）
8. `follow_up_suggestions` 生成
9. 异常与 disconnect 处理

**问题**：

- 单函数认知负担超过 IDE 可维护阈值（建议拆为 `ConversationContext`、`FileInjectionBuilder`、`SSEForwarder`）
- `stream_session` 与 Depends `session` 双 session，生命周期不清晰
- 多处 `except Exception` 后 `pass` 或仅 warning

---

## 4. 与下游智能体边界

```
agent.py::query_agent
    → NEAgentRouter.route(...)
    → NEFullFeatureAgent.aquery_stream(...)
        → NEAgentStreamHandler (stream_handler.py ~987 行)
        → ToolAssemblyPipeline
        → LangGraph compiled graph
```

| 层 | 应负责 | 实际 |
|----|--------|------|
| API (agent.py) | 鉴权、SSE 封装、会话元数据 | 含文件注入、深研状态机、追问服务 |
| Agent | 图执行、工具、prompt | 仍含超时/深研兜底 |
| StreamHandler | LangGraph event → 统一 dict | 已基本承担 |

与架构文档「API 只路由」目标仍有差距（见 [architecture_gap.md](./architecture_gap.md)）。

---

## 5. 任务 API（深研后台）

两文件均暴露：

| 路由 | 作用 |
|------|------|
| `GET .../tasks/{task_id}` | 轮询状态 |
| `GET .../tasks/{task_id}/stream` | DB 轮询 SSE（`tasks/manager.py`） |
| `POST .../tasks/{task_id}/cancel` | 取消 |
| `GET .../threads/{thread_id}/active-task` | 活跃任务 |

**质量点**：`manager.py` 1s DB 轮询 → 延迟与负载；与 [sse_events_module.md](../../docs/docs_backend/sse_events_module.md) 一致但缺背压说明。

---

## 6. guest.py / explore.py 要点

| 文件 | 严重度 | 问题 |
|------|--------|------|
| `guest.py` | medium | 自有 `_split_thinking_segments`（第三份）；访客鉴权与主站隔离需审计 |
| `explore.py` | medium | `noqa: BLE001` 宽泛 except；与 `explore_learning.py` 676 行服务层耦合 |

---

## 7. 本模块 P0/P1

| 优先级 | 项 |
|--------|-----|
| P1 | 抽取 `agent.py`/`csh_agent.py` 公共模块 |
| P1 | 删除或接入 `_split_thinking_segments`（消除死代码） |
| P2 | 统一深研 workflow_state 行为（NE vs CSH） |
| P2 | `stream_session` 生命周期与依赖注入文档化 |
| P3 | 任务 SSE 改 Redis pub/sub 或 LIST 替代 DB 轮询 |
