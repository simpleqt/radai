# P1 修复实施指南（详细版）

**前置**：完成 [backend_custom_p0_implementation_guide.md](./backend_custom_p0_implementation_guide.md) 中 PR-A/B/C  
**摘要版**：[backend_custom_p1_implementation_guide.md](./backend_custom_p1_implementation_guide.md)  
**日期**：2026-05-30

---

## P1-1：抽取 `api/v2/agent_stream_common.py`

### 目标文件结构

```
backend/custom/api/v2/
  agent_stream_common.py   # 新建
  agent.py                 # 瘦身至 ~150 行
  csh_agent.py             # 瘦身至 ~150 行
  task_routes.py           # 可选：共享任务 CRUD
```

### 迁移清单（按依赖顺序）

| 序号 | 符号（agent.py 近似行） | 迁入 common 后签名建议 |
|------|-------------------------|------------------------|
| 1 | `_split_thinking_segments` | **删除**；改用 `agents/streaming/thinking_filter.py`（P0-3 后） |
| 2 | `ensure_conversation_record` | 保持 async，参数 `agent_code`, `user_id`, `thread_id` |
| 3 | `inject_file_context` | 同上 |
| 4 | `persist_research_workflow_state` | 同上 |
| 5 | `_stream_agent_events` / 主 generator | `async def stream_agent_sse(*, router, agent_code, request, ...)` |
| 6 | chunk 处理循环 | 内联调用 `filter_thinking_from_chunk` |
| 7 | 后台深研启动 | `schedule_deep_research(..., domain: str)` |

### 参数化差异点

| 维度 | NE (`agent.py`) | CSH (`csh_agent.py`) |
|------|-----------------|----------------------|
| `agent_code` | `ne-full` | `csh-full` |
| Router | `NEAgentRouter` | `CSHAgentRouter` |
| 日志前缀 | `[NE Agent]` | `[CSH Agent]` |
| `get_agent_info` skills 列表 | NE 工具集 | CSH 工具集 |

### 回归清单

- [ ] `POST /api/v2/agent/query` SSE：thinking 不泄漏（P0-3 后）  
- [ ] `POST /api/v2/csh-agent/query` 同上  
- [ ] 文件上传 + 多轮对话  
- [ ] 深研 `mode=research` 启动后台任务  
- [ ] 前端 `agent-v2.ts` 解析 `event` 字段无变化  

### 风险

- 合并冲突大：建议 **先 P0-3 抽 thinking_filter**，再抽 common，避免同一 hunk 改两次。  
- 预估：**2 人日**（含回归）。

---

## P1-2：TaskManager 任务回收

### 现状

- `tasks/manager.py`：`remove_task` 已实现，**无调用方**（见 round5 文档）。  
- `_tasks: dict[str, BackgroundTask]` 常驻内存；`metadata.background_tasks` JSON 可膨胀。

### 实施步骤

1. **`tasks/runner.py`** — 在 `run_deep_research_background` / 通用 runner 的 `finally`：

```python
try:
    ...
finally:
    mgr = get_task_manager()
    await mgr.remove_task(task_id)  # 或 sync 包装
```

2. **可选延迟删除**（避免前端刚 poll 完就 404）：

```python
async def _delayed_remove(task_id: str, delay: float = 60.0):
    await asyncio.sleep(delay)
    await get_task_manager().remove_task(task_id)
asyncio.create_task(_delayed_remove(task_id))
```

3. **`_flush_to_db`** — terminal 任务写入 DB 后 `pop` 内存项。

### 验收

- 脚本循环 100 次深研：`len(get_task_manager()._tasks) <= 并发数`。  
- `GET /tasks/{id}` 完成后 60s 内仍可查（若采用延迟删除）。

---

## P1-3：Explore API 鉴权/限流

### 涉及路由（`api/explore.py`）

对以下 **POST** 增加 `Depends(get_current_user)` 或 optional + 严格限流：

- `expand_node`, `gather_resources`, `generate_quiz`, `generate_learning_path`, …

### 最小实现（方案 A）

```python
from backend.api.deps import get_current_user_optional
from slowapi import Limiter  # 若项目已有

@router.post("/expand")
async def expand(..., user=Depends(get_current_user_optional)):
    if user is None and not settings.EXPLORE_ALLOW_ANONYMOUS:
        raise HTTPException(401, "需要登录")
```

- `settings.EXPLORE_ALLOW_ANONYMOUS` 默认 `False`（生产）。  
- IP 限流：如 30 req/min per IP（`explore_learning` 内 LLM 成本高）。

### 与文档对齐

- 更新 `docs/docs_backend/explore_module.md`：`expand_node` **不检索 KB** 与 `explore_learning.expand_node` L465–478 一致。

---

## P1-4：异常脱敏

### 替换模式

| 场景 | 替换为 |
|------|--------|
| `HTTPException(500, detail=str(e))` | `detail="服务暂时不可用"` + `logger.exception` |
| SSE `{"error": str(e)}` | `{"error": "处理失败", "error_id": "<uuid>"}` |
| `ToolMessage(content=f"Error: {e}")` | 通用文案 + 日志保留 stack |

### 文件与大致位置

| 文件 | 模式出现次数（审查估算） |
|------|--------------------------|
| `api/v2/agent.py` | 多处 yield/raise |
| `api/v2/csh_agent.py` | 同上 |
| `api/v2/guest.py` | 部分已安全 |
| `api/explore.py` | LLM 失败 |
| `tasks/runner.py` | `push_event(error=...)` |

### 验收

- 故意 `raise RuntimeError("secret db password")` → 响应体不含 `password`。

---

## P1-5：死代码与配置清理

| 项 | 操作 |
|----|------|
| `_normalize_intent_payload` / `_normalize_plan_payload` | 删除或接入 planner（若产品需要） |
| `create_chat_model` 未使用 import | 删除 |
| `intent_mode` 仅日志 | 删除参数或实现分支 |
| `config/kb_config.py` | `rg` 全仓库无 import → 删除或合并到 `settings` |

---

## P1-6：metadata 后台任务裁剪

**位置**：`tasks/manager.py` `_flush_to_db` / `push_event`

**策略二选一**：

1. **硬上限**：`events = events[-200:]` 当 `len > 200` 且 `status in terminal_states`。  
2. **归档**：terminal 后把整个 task 移到 `background_tasks_archive[task_id]`，活跃 dict 删除。

**注意**：前端 `useAgentChat` 恢复依赖 event 历史 → 保留 `done` + 最后 N 条 `progress` 即可（与前端确认 N）。

---

## P1-7：`guest_conversations` N+1

**文件**：`api/v2/guest.py`（或 conversations 列表路由）

**问题**：`limit*3` 拉取再过滤无消息 thread。

**方案**：

1. SQL：`WHERE EXISTS (SELECT 1 FROM checkpoints ...)`（依 checkpointer 表结构）。  
2. 或批量 `checkpointer.alist(thread_ids)` 一次取元数据。  

**验收**：列表 20 条对话时 DB 查询次数 O(1) 或 O(2)，非 O(20)。

---

## P1-8（新增）：`multi_kb` 域感知 KB 列表

见 [backend_custom_deep_dive_multi_kb_search_tool.md](./backend_custom_deep_dive_multi_kb_search_tool.md)。

- `execute_multi_kb_search(..., domain=None)`  
- CSH Agent 工具注册时传入 `domain="csh"` 或从 runtime 读取。

**依赖**：P0-1/2 之后做，避免 CSH Agent 仍检索 NE 库。

---

## P1-9（新增）：`deep_research_report` 文件名一致性

**问题**：`save_deep_research_report_tool` 用 `filename`；`save_deep_research_report_direct` 用 `filename or title`（L257–258），行为不一致。

**修复**：direct 路径与 tool 路径共用 `_safe_report_filename(filename or title)`。

---

## 排期（更新）

| 周 | 项 | 人日 |
|----|-----|------|
| W1 | P1-5, P1-4 | 1–1.5 |
| W2 | P1-2, P1-6, P1-9 | 1–1.5 |
| W3 | P1-1 | 2 |
| W4 | P1-3, P1-7 | 1.5 |
| W5 | P1-8 + multi_kb 测试 | 2 |

---

*Issue 跟踪：[backend_custom_github_issues.md](./backend_custom_github_issues.md) · CSV：[backend_custom_issues.csv](./backend_custom_issues.csv)*
