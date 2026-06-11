# 第七轮审查：任务、流式、路由与杂项

**日期**：2026-05-30 · **只读**

---

## 1. `tasks/manager.py` + `tasks/runner.py`

### 已做得好的地方

- **DB 双写** + 跨 worker 轮询（模块头注释）。
- `subscribe()` L218–221：订阅与快照 **原子化**，避免漏 `done`（注释与实现一致）。
- `_flush_to_db` L162–163：`events` 超 `MAX_EVENTS=10000` 时截断尾部。
- `runner` 深研流 **已实现** think-tag 拆分（`in_thought` / `think_carry`，与 guest 类似）。

### P1：`remove_task` 仍无调用方

- `TaskManager.remove_task` L298+ 已实现，**runner `finally` 未调用**（与 round5 一致）。
- 内存 `_tasks` 与 `metadata.background_tasks` 长期膨胀。

### P0 关联：`run_deep_research_background` 无 `agent_domain`

```212:222:backend/custom/tasks/runner.py
        agent = DeepResearchOrchestratorAgent(
            model_name=model_name,
            ...
            kb_ids=kb_ids,
```

- 签名（L132–146）**无** `agent_domain` / `orchestrator_cls`。
- CSH API 注释写「passing CSH specific KB ids」（`csh_agent.py` L651）但 **类仍为 NE 编排器** → KB 对、Prompt/子智能体仍错。

### P2：取消与 `cancel()` 副作用

- `BackgroundTask.cancel()` L91–96：`asyncio.create_task(self.push_event(done))` **无 await**，与 `status=cancelled` 竞态；极端情况下订阅方先收到 done 后收到 chunk。

### P2：DB flush 失败重试

- L169–176：失败时将 `events_to_write` 塞回 `_pending_db_events`，**可能重复追加** 若部分 commit 成功（取决于 DB 事务边界；当前单 commit，一般全失败或全成功）。

### P3：订阅队列 `maxsize=10000`

- 慢客户端 → `QueueFull` → 踢订阅（L197–205），合理；可文档化前端需及时消费。

---

## 2. `request_limits.py`（88 行）

### 设计评价：**良好**

- `ContextVar` 预算、`TURN_MAX_EVIDENCE_CHARS`、`asyncio.Lock` 防并行 multi_kb。
- `ne_full_feature_agent` 在流式入口 `set_tool_budgets` + `finally reset_tool_budgets`。

### P1：`csh_full_feature_agent` 重复调用 `reset_turn_kb_context_budget`

```577:578:backend/custom/agents/csh_full_feature_agent.py
        reset_turn_kb_context_budget()
        reset_turn_kb_context_budget()
```

- 每次调用会 **`_MULTI_KB_REQUEST_LOCK.set(asyncio.Lock())` 新锁**（L64–67 `request_limits.py`）。
- 若第一次调用后已有协程持有旧锁，第二次调用会换锁 → **理论上可破坏「禁止并行 multi_kb」语义**（边界：同请求内极短窗口）。
- NE agent 仅调用 **一次**（L620 附近）— **CSH 为笔误或复制残留**。

**建议**：删除重复一行；加 lint/单测断言每请求只 reset 一次。

---

## 3. `guest_conversations.py` — N+1（P1-7 细化）

```46:59:backend/custom/api/guest_conversations.py
        .limit(limit * 3)
    ...
        if not await _has_checkpoint(request, internal_tid):
            continue
```

- 每会话 **1 次** `checkpointer.aget`；`limit=20` 最多 60 次 DB/checkpoint IO。
- offset 分页不准确：跳过无消息会话后，**每页条数不稳定**。
- **建议**：SQL 子查询 / 批量 list checkpoints / 在 `Conversation.metadata_` 缓存 `has_messages`。

---

## 4. `router.py` / `csh_router.py`

### 路由规则（NE 与 CSH **同构**）

```53:60:backend/custom/agents/csh_router.py
    def _resolve_mode(self) -> str:
        is_deep_research = self._task_type == "deep_research" or self._mode in {...}
        if is_deep_research:
            return "deep_research"
        if not self._has_user_selected_skills():
            return "lightweight"
        return "full"
```

| 条件 | 模式 |
|------|------|
| `task_type=deep_research` 或 mode 深研 | `deep_research` |
| **未选任何 skill** | `lightweight` ← 易误判：用户以为「全功能」但未勾选 skill |
| 否则 | `full` |

### P2：轻量模式触发过宽

- 前端未传 `selected_skills` 时 **默认 lightweight**（空列表），走 `LightweightQAAgent` + **无工具**。
- 与产品「默认全功能 Agent」预期可能不符；应在 API 层默认 skill 或改 `_resolve_mode` 默认 `full`。

### P0：CSH `deep_research` 分支

- L77–95：直接 `DeepResearchOrchestratorAgent`（NE），注释承认「currently using the standard」— 与架构文档 CSH 深研编排器 **未实现** 一致。

---

## 5. `async_web_search_tool.py`

- `_sanitize_search_query` 去除 runtime/think 块（L22–30），质量较好。
- 依赖 `ddgs` 必应；超时 `asyncio.wait_for`（L65–67）。
- **无请求级预算** 在工具内；预算在 `set_tool_budgets` 的 `web_search: 10`。
- P3：返回 URL 未校验 scheme，模型可见任意链接（一般可接受）。

---

## 6. `stream_handler.py`（NEAgentStreamHandler，~988 行）

- 处理 `reasoning_content`、工具事件、artifact、低输出 WARNING（L30–31）。
- **不处理** `` 标签（依赖上游 API 或 P0-3 过滤）。
- 深研/普通共用 handler 名 `NEAgentStreamHandler`，CSH 全功能 agent 也实例化此类（`csh_full_feature_agent.py` L586）— 命名误导 P3。

**建议**：P0-3 后 handler 可只处理原生 reasoning；或重命名为 `AgentStreamHandler`。

---

## 7. `research_draft` SSE 事件

- 事件类型 `draft_chunk` **未** 在 `sse_events_module.md` 中统一文档化（见 docs gap 文档）。
- 思考标签泄漏：见 [backend_custom_deep_dive_research_draft_and_tool_assembly.md](./backend_custom_deep_dive_research_draft_and_tool_assembly.md)。

---

## 8. 本轮新增 Issue 摘要

| ID | 优先级 | 标题 |
|----|--------|------|
| P1-10 | P1 | CSH 重复 `reset_turn_kb_context_budget` |
| P2-4 | P2 | 未选 skill 默认 lightweight 可能不符合产品 |
| P2-5 | P2 | `draft_chunk` SSE 未过滤 think 标签 |
| P2-6 | P2 | DeepResearch 后台 runner 无 agent_domain 参数 |

---

*关联：[backend_custom_p1_implementation_guide_detailed.md](./backend_custom_p1_implementation_guide_detailed.md)*
