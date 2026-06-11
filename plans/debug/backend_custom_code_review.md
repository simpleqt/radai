# backend/custom 代码质量审查报告

**审查范围**：`backend/custom`（52 个 Python 文件 + 1 个计划文档）  
**审查日期**：2026-05-30  
**说明**：仅输出问题与建议，未修改 `backend/custom` 内任何代码。

**逐文件明细**：
- [backend_custom_per_file_review.md](./backend_custom_per_file_review.md)（摘要表）
- [backend_custom_per_file_review_detailed.md](./backend_custom_per_file_review_detailed.md)（深度：符号、行号、依赖、测试）

**延伸**：
- [backend_custom_vs_architecture_redesign.md](./backend_custom_vs_architecture_redesign.md)（对照架构重构文档）
- [backend_custom_p0_fix_checklist.md](./backend_custom_p0_fix_checklist.md) / [p0_implementation_guide](./backend_custom_p0_implementation_guide.md)
- [backend_custom_sse_and_api_contract_gaps.md](./backend_custom_sse_and_api_contract_gaps.md)
- [backend_custom_round5_core_security_tests.md](./backend_custom_round5_core_security_tests.md)（core 耦合·安全·测试）
- [backend_custom_github_issues.md](./backend_custom_github_issues.md)（Issue 模板）
- [backend_custom_executive_summary.md](./backend_custom_executive_summary.md)（第六轮：1 页摘要）
- [backend_custom_deep_dive_multi_kb_search_tool.md](./backend_custom_deep_dive_multi_kb_search_tool.md) / [html_ppt](./backend_custom_deep_dive_html_ppt_tool.md)
- [backend_custom_p1_implementation_guide_detailed.md](./backend_custom_p1_implementation_guide_detailed.md)
- [backend_custom_issues.csv](./backend_custom_issues.csv)
- [plans/debug/README.md](./README.md)（全目录索引）

---

## 1. 总体结论

`backend/custom` 是业务智能体、工具、SSE API 与后台任务的核心层，功能完整、容错与降级思路较多（DB 失败不阻断流式、跨进程任务轮询等）。主要质量问题集中在：

1. **大量重复代码**（NE / CSH 双端 API、思考标签拆分、工作流持久化）  
2. **领域耦合错误**（CSH 路径复用 NE 专用编排器 / 轻量 Prompt）  
3. **死代码与不一致实现**（未使用的辅助函数、思考过滤逻辑分叉）  
4. **运维与资源风险**（内存任务不回收、metadata 膨胀、异常信息外泄）  

建议按「正确性 → 安全/成本 → 可维护性」顺序治理。

---

## 2. 严重问题（P0）

### 2.1 CSH 深度研究使用 NE 编排器与 NE 系统 Prompt

| 位置 | 说明 |
|------|------|
| `agents/deep_research_orchestrator_agent.py` | `DeepResearchOrchestratorAgent` **继承** `NEFullFeatureAgent`，深度研究编排规则叠加在 NE 的 `_build_system_prompt()` 之上 |
| `agents/csh_router.py` L77-95 | CSH 深度研究模式仍实例化 `DeepResearchOrchestratorAgent` |
| `tasks/runner.py` L212-222 | 后台深度研究 **固定** 使用 `DeepResearchOrchestratorAgent`，`csh_agent.py` 调用时未传入领域标识 |

**影响**：CSH-AI 用户走「深度研究」或后台任务时，主控智能体行为、工具约束、子智能体委派规则均按**核电子学**域设计，与 `csh_prompts` / `CSHFullFeatureAgent` 不一致，属于**功能性缺陷**。

**建议**：抽象 `DeepResearchOrchestratorAgent` 基类或注入 `prompt_builder` / `agent_factory`；`run_deep_research_background` 增加 `domain: Literal["ne", "csh"]` 参数，CSH 路由到 `CSHFullFeatureAgent` 子类或专用编排器。

---

### 2.2 CSH / 探索式学习轻量模式使用 NE 专用 `LightweightQAAgent` Prompt

| 位置 | 说明 |
|------|------|
| `agents/lightweight_qa_agent.py` L39+ | `LIGHTWEIGHT_SYSTEM_PROMPT` 写死为「核电子学领域专业问答智能体」 |
| `agents/csh_full_feature_agent.py` L552-570 | `lightweight_mode=True` 时直接构造 `LightweightQAAgent`，**无学科参数** |
| `api/v2/explore.py` L224-242 | `domain=csh` 时虽选 `CSHFullFeatureAgent`，轻量链路仍落到上述 NE Prompt |

**影响**：CSH 站点默认轻量问答、探索式学习「知识点详解」在轻量路径下会以核电子学角色作答，与 `domain` / 知识库领域矛盾。

**建议**：`LightweightQAAgent` 接受 `subject` 或 `system_prompt_factory`；CSH / explore 传入 `场地污染水文地质` 对应模板（可复用 `csh_prompts` 精简版）。

---

## 3. 高优先级问题（P1）

### 3.1 API 层大规模重复（NE vs CSH）

| 文件 | 行数（约） | 重复内容 |
|------|-----------|----------|
| `api/v2/agent.py` | 1354 | 会话确保、工作流状态、深度研究草案、SSE 流、后台任务 API |
| `api/v2/csh_agent.py` | 1243 | 与上表结构几乎一致，仅日志前缀与 Agent 类不同 |

另：`guest.py`、部分 `runner.py` 再次复制 `_split_thinking_segments` 等工具函数（至少 **4 处**）。

**风险**：修一处漏一处（已出现思考过滤不一致，见 3.2）。

**建议**：抽取 `api/v2/_agent_stream_common.py`（或类似模块）统一 `stream_generator`、任务 API；思考标签拆分迁入 `agents/streaming/` 单模块。

---

### 3.2 思考链标签过滤逻辑不一致（死代码 + 行为分叉）

| 文件 | 行为 |
|------|------|
| `api/v2/agent.py` | 定义 `_split_thinking_segments`，流式循环中 **未调用**；`in_thought` / `think_carry` / `stripped_chunk_count` 仅初始化与打日志 |
| `api/v2/csh_agent.py` | 同样未使用拆分逻辑 |
| `api/v2/guest.py` | **正确使用** `_split_thinking_segments` 过滤 chunk |
| `tasks/runner.py` | 后台任务 **正确使用** 过滤 |

**影响**：主站 NE/CSH 流可能把模型泄漏的 `` / `<thought>` 原文直接推给前端；访客接口反而更安全。日志中的 `思考标签过滤chunk` 在 NE 路径恒为 0，易误导排查。

**建议**：主站 SSE 与 `guest.py` 对齐，或下沉到 `NEAgentStreamHandler` 统一出口过滤。

---

### 3.3 未使用代码与无用导入

| 位置 | 项 |
|------|-----|
| `api/v2/agent.py` L65-96 | `_normalize_intent_payload`、`_normalize_plan_payload` **从未被调用** |
| `api/v2/agent.py` L472 | `from backend.core.models import create_chat_model` **未使用** |
| `api/v2/csh_agent.py` | 同上两套 normalize 函数同样未使用 |
| `api/v2/agent.py` L601 | 注释写「不再做自动意图识别」，但 `intent_mode` 仍在请求体与日志中出现（见 3.8） |

---

### 3.4 `TaskManager` 内存泄漏

| 位置 | 说明 |
|------|------|
| `tasks/manager.py` L298-300 | 提供 `remove_task()` |
| 全仓库 | **无任何调用** `remove_task` |

**影响**：每个已完成的后台任务对象（含事件缓冲、订阅队列、DB flusher 状态）长期留在进程 `_tasks` 字典中，多 worker / 长运行下内存持续增长。

**建议**：任务进入 terminal 状态后在 `finally`（`runner.py`）或 `push_event(done)` 后延迟调用 `remove_task`；或改用弱引用 / TTL 缓存。

---

### 3.5 `Conversation.metadata_` 中后台任务事件无限累积

| 位置 | 说明 |
|------|------|
| `tasks/manager.py` L28 `MAX_EVENTS = 10000` | 单任务事件上限 1 万条 |
| `tasks/manager.py` L151-167 | 刷新 DB 时 **合并** 历史 events，仅截断单任务列表 |
| 无清理逻辑 | 多个 `task_id` 并存时，`background_tasks` 对象持续增大 |

**影响**：MySQL JSON 字段膨胀、读写变慢、跨进程轮询每次加载大量 events。

**建议**：任务完成后归档或只保留最近 N 条；旧 task_id 定期 prune。

---

### 3.6 探索式学习 API 多数接口无鉴权

| 路由 | 鉴权 |
|------|------|
| `POST /api/v2/explore/topics|map|expand|resources|quiz|quiz/grade|detail` | **无** `Depends(get_current_user)` |
| `GET /api/v2/explore/resources/download` | 需要登录 |

文件头注释写明「登录可选」，但开放接口会触发 **LLM + 检索**，存在被滥刷成本的风险。

**建议**：至少加固 rate limit、API Key、或可选鉴权；与 `tools/request_limits` 联动。

---

### 3.7 异常信息直接返回客户端

多处将 `str(e)` 写入 HTTP `detail` 或 SSE `error` 字段，例如：

- `api/v2/agent.py` L431, L1081, L1109  
- `api/v2/csh_agent.py` L374, L990, L1018  
- `api/v2/guest.py` L190, L204  
- `tasks/runner.py` L337-341  

**风险**：堆栈、路径、内部服务名泄露。

**建议**：对外统一错误码 + 通用文案；详情仅写 `logger.exception`。

---

## 4. 中优先级问题（P2）

### 4.1 `asyncio.create_task` 未跟踪的后台协程

| 位置 | 场景 |
|------|------|
| `api/v2/agent.py` L729, L1055 | 深度研究后台、`persist_follow_up_to_checkpoint` |
| `api/v2/csh_agent.py` L655, L965 | 同上 |
| `tasks/manager.py` L96 | `cancel()` 内 `create_task(push_event)` |

**风险**：进程关闭时任务被静默取消；异常未 await 时仅以 warning 日志出现。

**建议**：应用级 `TaskGroup` 或注册到 lifespan；对关键任务加 `add_done_callback` 记录失败。

---

### 4.2 流式路径重复创建 DB Session

| 位置 | 说明 |
|------|------|
| `api/v2/agent.py` L506 | `stream_session = get_async_session_factory()()` |
| 同文件 L462 | 已有 `Depends(get_async_session)` 的 `session` |

同请求可能并行使用两个 `AsyncSession` 写同一 `Conversation`，依赖 IntegrityError 处理竞态，增加连接池压力。

**建议**：单 session 贯穿流式生命周期，或明确「流式只读 + 独立写 session」的注释与锁策略。

---

### 4.3 工作流初始状态读取竞态

`agent.py` / `csh_agent.py` 在 `conversation_task` 完成**之前**读取：

```python
current_workflow_state = normalize_research_workflow_state(
    ((conversation.metadata_ or {}).get("research_workflow") or {}).get("state") if conversation else None
)
```

首次请求时 `conversation` 恒为 `None`，无法从 DB 恢复已有 `research_workflow` 状态（若存在）。

**建议**：`await conversation_task` 后再初始化 `current_workflow_state`，或从 DB 单独查询 metadata。

---

### 4.4 `csh_full_feature_agent.py` 重复调用

```python
reset_turn_kb_context_budget()
reset_turn_kb_context_budget()  # L577-578 连续两次
```

`ne_full_feature_agent.py` 仅调用一次。虽无功能破坏，体现复制粘贴疏漏。

---

### 4.5 文件名拼写错误

| 文件 | 问题 |
|------|------|
| `tools/acdemic_search_tool.py` | `acdemic` 应为 `academic` |

影响可发现性与 IDE 引用；若对外暴露工具名需评估兼容性。

---

### 4.6 模块级 `logging.basicConfig`

`tools/acdemic_search_tool.py` L33：

```python
logging.basicConfig(level=logging.INFO)
```

在库模块中调用 `basicConfig` 可能覆盖应用全局日志配置。

**建议**：删除，仅使用 `logger = logging.getLogger(__name__)`。

---

### 4.7 跨模块引用「私有」函数

`api/v2/explore.py` L175：

```python
disposition = svc._build_content_disposition(filename)
```

违反封装，重构 `explore_learning` 时易破坏 API 层。

**建议**：改为公开函数 `build_content_disposition` 或移到 `utils`。

---

### 4.8 请求字段 `intent_mode` 名存实亡

`QueryRequest.intent_mode` 在 NE/CSH API 中默认 `manual`，仅用于日志；自动意图识别已移除（`agent.py` L601 注释）。

**影响**：前后端契约误导，增加维护成本。

**建议**：废弃字段或恢复实现；文档与 OpenAPI 同步。

---

### 4.9 `DeepResearchOrchestratorAgent` 与 `NEFullFeatureAgent` 职责重叠

路由层（`router.py`）在 `deep_research` 模式走编排器；API 层又在 `confirmation_status == "confirmed"` 时走 `run_deep_research_background`（再次实例化编排器）。两条路径需文档说明，避免重复启动或状态不一致。

---

## 5. 低优先级 / 可维护性（P3）

### 5.1 超大文件与复杂度

| 文件 | 行数 | 建议 |
|------|------|------|
| `api/v2/agent.py` | ~1354 | 拆路由 / 流式 / 任务子模块 |
| `agents/lightweight_qa_agent.py` | ~1349 | 拆检索、Prompt、流式三节 |
| `tools/multi_kb_search_tool.py` | ~1223 | 拆检索策略与格式化 |
| `agents/streaming/stream_handler.py` | ~987 | 可按事件类型拆 handler |

---

### 5.2 `ne_full_feature_agent.py` 与 `csh_full_feature_agent.py` 高度相似

两者共享 `NEAgentStreamHandler`、`ToolAssemblyPipeline` 模式，约 900+ 行结构平行。长期应抽 `BaseFullFeatureAgent`（模板方法 + 子类只覆写 prompt/kb/agent_code）。

---

### 5.3 `middlewares/` 包为空壳

仅 `__init__.py` 注释「自定义中间件」，无实际中间件实现。若计划迁移限流/鉴权，需补齐或删除占位包。

---

### 5.4 计划文档位于源码树

`backend/custom/skills_mysql_dynamic_management_plan.md` 属于设计文档，不宜与运行时代码同目录，易被打包或混淆。

---

### 5.5 宽泛 `except Exception`

`backend/custom` 内大量 `except Exception`（含 `pass` / 仅 warning）。关键路径（付费 LLM 调用、DB 提交）建议收窄异常类型，避免吞掉 `KeyboardInterrupt` 以外的编程错误。

---

### 5.6 全局可变缓存无界增长

`lightweight_qa_agent.py`：

- `_compress_cache: dict[str, tuple[str, int]]`  
- `_compress_tasks: dict[str, asyncio.Task[None]]`  

无 LRU / 过期清理，thread_id 多时内存上涨。

---

### 5.7 DB 轮询 SSE 效率

`api/v2/agent.py` `db_poll_generator` 每 0.5s 全量 `read_task_from_db`，高并发时 DB 压力大。可考虑 LISTEN/NOTIFY、Redis pub/sub 或增量 cursor。

---

## 6. 做得较好的方面

1. **会话创建竞态**：`_ensure_conversation_record` 用 `IntegrityError` + rollback 处理并发创建，设计合理。  
2. **流式断开检测**：`request.is_disconnected()` 提前结束，减轻 checkpoint 写入。  
3. **工具预算**：`tools/request_limits.py` 用 `ContextVar` 做请求级 multi_kb 字符与并发锁，适合多 worker。  
4. **探索式学习服务**：`explore_learning.py` 预制主题 + LLM 失败兜底，降低前端崩溃概率。  
5. **流式工具类**：`stream_handler.py` / `stream_utils.py` 对 LangGraph 事件翻译较完整，含低输出告警。  
6. **跨进程任务**：DB 持久化 + 轮询 SSE 满足多 worker 部署场景。

---

## 7. 建议修复顺序（不改代码，仅排期参考）

| 阶段 | 项 | 预期收益 |
|------|-----|----------|
| Sprint 1 | 2.1 CSH 深度研究编排器 / 2.2 轻量 Prompt 领域 | 修复错误答案域 |
| Sprint 1 | 3.2 主站思考标签过滤对齐 guest | 安全与体验 |
| Sprint 2 | 3.1 抽取 NE/CSH 公共 API 模块 | 降低回归率 |
| Sprint 2 | 3.4 + 3.5 任务内存与 metadata 清理 | 稳定性 |
| Sprint 3 | 3.6 探索 API 限流/鉴权、3.7 错误脱敏 | 安全与成本 |
| Sprint 3 | 3.3 删除死代码、4.8 清理 intent_mode | 可维护性 |
| 持续 | 5.x 大文件拆分与 Base Agent 抽象 | 长期演进 |

---

## 8. 审查文件清单（按包）

```
backend/custom/
├── api/v2/          agent.py, csh_agent.py, guest.py, explore.py, __init__.py
├── api/             guest_conversations.py
├── agents/          router, csh_router, ne/csh_full_feature, lightweight_qa,
│                    deep_research_orchestrator, research_draft, streaming/*
├── tools/           multi_kb, academic(acdemic), excel*, html_ppt, async_web, ...
├── tasks/           manager.py, runner.py
├── services/        explore_learning, follow_up_suggestions
├── workflow/        research_state.py
├── utils/           checkpointer.py
└── config/          kb_config.py
```

---

*本报告由静态阅读与模式搜索生成，未运行测试或动态探针。修复后建议对 NE/CSH 深度研究、探索 detail、访客流式各补一条集成测试。*
