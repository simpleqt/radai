# P1 修复实施指南（摘要）

**前置**：P0 完成后推进 · [backend_custom_code_review.md](./backend_custom_code_review.md)  
**日期**：2026-05-30

---

## P1-1：抽取 `api/v2/agent_stream_common.py`

**目标**：`agent.py` 与 `csh_agent.py` 共享 ~1100 行流式逻辑。

**建议模块职责**：

| 函数/类 | 职责 |
|---------|------|
| `build_stream_generator(...)` | 参数化 `agent_code`, `router_factory`, `log_prefix` |
| `ensure_conversation_record` | 已存在，迁入 common |
| `persist_research_workflow_state` | 迁入 common |
| `inject_file_context` | 迁入 common |
| `make_process_event` | 可保留各文件或迁入 |

**`agent.py` / `csh_agent.py` 仅剩**：

- Router 前缀常量（`_NE_AGENT_CODE` / `_CSH_AGENT_CODE`）
- `get_agent_info` / `get_available_skills`
- `query_agent` 调用 `build_stream_generator(router_cls=NEAgentRouter, ...)`
- 任务 API 可继续重复或再抽 `task_routes.py`

**预估**：1–2 人日 + 全量 SSE 回归。

---

## P1-2：TaskManager 任务回收

**改动**：

```python
# tasks/runner.py finally 块末尾
from backend.custom.tasks.manager import get_task_manager
await get_task_manager().remove_task(task.task_id)
```

**可选**：`BackgroundTask` 进入 terminal 时在 `push_event(done)` 后调度延迟 60s remove。

**验证**：压测 100 次深研后 `len(task_mgr._tasks)` 不持续增长。

---

## P1-3：Explore API 鉴权/限流

**方案 A（最小）**：所有 explore POST 加 `Depends(get_current_user_optional)` + IP 限流中间件。

**方案 B**：API Key header + 与 RadAI 同用户体系。

**涉及**：`explore.py` 各路由、`explore_learning.py` 无需改。

链读：`plans/认证方案.md`。

---

## P1-4：异常脱敏

**模式**：

```python
# 坏
raise HTTPException(500, detail=str(e))
yield f"data: {json.dumps({'error': str(e)})}\n\n"

# 好
logger.exception("...")
raise HTTPException(500, detail="服务暂时不可用，请稍后重试")
yield SSE error 通用文案 + 可选 error_id=uuid
```

**批量替换文件**：`agent.py`, `csh_agent.py`, `guest.py`, `explore.py`, `runner.py`（用户可见处）。

---

## P1-5：死代码清理

| 文件 | 删除/修复 |
|------|-----------|
| `agent.py`, `csh_agent.py` | `_normalize_intent_payload`, `_normalize_plan_payload`, 未用 import |
| `agent.py` L472 | `create_chat_model` import |
| `config/kb_config.py` | 确认无引用后删除或迁 settings |

---

## P1-6：metadata 后台任务裁剪

**`tasks/manager.py` `_flush_to_db`**：

- 任务 `status in terminal` 时，`events` 只保留最后 200 条  
- 或任务完成后将 `background_tasks[task_id]` 迁到 `background_tasks_archive` 并 pop 活跃键

---

## P1-7：`guest_conversations` N+1

- 批量加载 checkpoint 存在性（若 checkpointer 支持 list）  
- 或 SQL 子查询过滤有消息的 thread_id  
- 修正 `limit*3` 分页为子查询分页

---

## 建议排期

| 周 | 项 |
|----|-----|
| W1 | P1-5, P1-4（低风险） |
| W2 | P1-2, P1-6 |
| W3 | P1-1 |
| W4 | P1-3, P1-7 |

---

*详细逐文件说明见 [backend_custom_per_file_review_detailed.md](./backend_custom_per_file_review_detailed.md)。*
