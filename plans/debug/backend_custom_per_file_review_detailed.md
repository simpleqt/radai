# backend/custom 逐文件深度审查（第二部分）

**前置文档**：[backend_custom_per_file_review.md](./backend_custom_per_file_review.md)（摘要表）  
**总览**：[backend_custom_code_review.md](./backend_custom_code_review.md)  
**日期**：2026-05-30 | **未改源码**

本文对 **每个文件** 补充：职责、关键符号、具体问题（含行号/行为）、依赖、测试缺口。

---

## 1. `__init__.py`

| 项 | 内容 |
|----|------|
| 职责 | 包标记 |
| 符号 | 无 |
| 问题 | 无 |
| 依赖 | — |
| 测试 | 无 |

---

## 2. `skills_mysql_dynamic_management_plan.md`

| 项 | 内容 |
|----|------|
| 职责 | Skills MySQL+MinIO 动态注册方案（设计稿） |
| 问题 | P3：与运行时代码混放；状态「方案设计」与当前 `ScopedSkillsProvider` 实现可能已部分偏离，需对照 `backend/core/skills` |
| 测试 | 不适用 |

---

## 3. `api/__init__.py`

占位，无问题。

---

## 4. `api/guest_conversations.py`（74 行）

**职责**：访客站点会话列表，仅返回 checkpoint 中确有消息的会话。

| 符号 | 说明 |
|------|------|
| `_has_checkpoint` | 逐条 `checkpointer.aget` |
| `list_guest_conversations` | `limit*3` 预取后过滤 |

**问题**：

| 级别 | 位置 | 说明 |
|------|------|------|
| P2 | L14 vs `agent.py` L27 | `build_internal_thread_key` 分别从 `backend.core.config` 与 `backend.core.agents.thread_key` 导入，行为相同但维护分裂 |
| P2 | L55-71 | **N+1**：每个 conversation 一次 checkpoint 读，20 条可见会话最坏 60 次 DB/SQLite 读 |
| P2 | L51 `limit * 3` | offset 分页在过滤后失真，翻页可能重复/漏项 |
| P3 | L32-34 | 失败静默返回 `False`，会话被隐藏且无指标 |

**依赖**：`Conversation` ORM、`guest_checkpoints.db`（注释 L3-4）

**测试缺口**：无 checkpoint 的会话过滤；分页边界。

---

## 5. `api/v2/__init__.py`（16 行）

聚合 4 个子路由，前缀 `/api/v2`。无问题。

---

## 6. `api/v2/agent.py`（~1354 行）

**职责**：NE 全功能智能体 HTTP API（info/skills/query/SSE、深度研究草案、后台任务 CRUD）。

**关键符号**：

| 符号 | 行号（约） | 说明 |
|------|-----------|------|
| `_split_thinking_segments` | 257-295 | 思考标签拆分 |
| `query_agent` → `stream_generator` | 497-1096 | 主 SSE 生成器 |
| `_ensure_conversation_record` | 139-195 | 会话 UPSERT + IntegrityError 竞态 |
| `run_deep_research_background` | 729 | fire-and-forget 后台任务 |

**问题明细**：

| 级别 | 位置 | 说明 |
|------|------|------|
| P0 | 929-936 vs 257-295 | **chunk 路径直接转发 `raw_text`，未调用 `_split_thinking_segments`**；`in_thought`/`think_carry`/`stripped_chunk_count` 仅统计日志（812-813, 925） |
| P1 | 65-96, 472 | `_normalize_intent_payload`、`_normalize_plan_payload`、`create_chat_model` **死代码** |
| P1 | 309, 480-483 | `intent_mode` 请求字段仅日志，与 L601 注释「不再自动意图识别」矛盾 |
| P1 | 542-544 | `current_workflow_state` 在 `conversation_task` 完成前读取，`conversation` 恒 None → **无法恢复已有 research_workflow** |
| P1 | 506-507, 462 | 另开 `stream_session`，与 Depends `session` 并行写同一 conversation |
| P1 | 729, 1055 | `asyncio.create_task` 无 app 级跟踪，进程退出或异常未 await |
| P1 | 431, 1081, 1109 | `str(e)` 写入 HTTP/SSE |
| P1 | 全文 | 与 `csh_agent.py` ~90% 重复，后续修 bug 易漏 |
| P2 | 1023-1034 | 流正常结束即 `COMPLETED`，若实际在 `AWAITING_CLARIFICATION` 可能被覆盖（取决于事件顺序） |
| P2 | 826-897 | 文件注入：同步 `get_file_meta`，大文档拼入 prompt 无总长度上限（除 long_doc prefix） |

**依赖**：`NEAgentRouter`、`ResearchDraftAgent`、`TaskManager`、`chat_files` 路由

**测试缺口**：草案 confirmed/cancelled/revised 状态机；断开连接后 checkpoint；思考标签泄漏。

---

## 7. `api/v2/csh_agent.py`（~1243 行）

与 `agent.py` 同构，差异点：

| 级别 | 位置 | 说明 |
|------|------|------|
| P0 | 655-669 | `run_deep_research_background` **无 domain**，runner 固定 NE 编排器 |
| P2 | 577-578 | `reset_turn_kb_context_budget()` 调用两次（在 agent 流中由 `CSHFullFeatureAgent` 触发，API 层无此问题） |
| P1 | 同 agent | 思考过滤、死代码、双 session、异常外泄等同上 |

**测试缺口**：CSH 深度研究端到端应断言系统 Prompt 含水文地质而非核电子学。

---

## 8. `api/v2/guest.py`（~205 行）

**职责**：访客流式问答（无登录）。

| 级别 | 位置 | 说明 |
|------|------|------|
| OK | 151-156 | **正确使用** `_split_thinking_segments` |
| P2 | 145-148 | **无** `request.is_disconnected()`，客户端断开可能继续消耗 LLM |
| P1 | 190, 204 | 异常外泄 |
| P2 | 122 | `internal_tid = f"g_{device_id}__t_{external_tid}"` 与 `build_internal_thread_key` 格式不一致，需确认 checkpoint 键规范统一 |
| P3 | 34-85 | 与 agent 重复的思考拆分工具函数 |

**依赖**：`NEFullFeatureAgent(lightweight_mode=True)`

---

## 9. `api/v2/explore.py`（~272 行）

**路由与鉴权**：

| 路由 | 鉴权 | 风险 |
|------|------|------|
| POST topics/map/expand/resources/quiz/quiz/grade/detail | 无 | P1 成本滥刷 |
| GET resources/download | `get_current_user` | OK |

| 级别 | 位置 | 说明 |
|------|------|------|
| P0 | 224-242 | `CSHFullFeatureAgent` + `lightweight_mode` → NE Prompt |
| P1 | 175 | `svc._build_content_disposition` 私有函数 |
| P2 | 97-103 `_first_kb` | 多 kb_ids 仅用第一个 |
| P2 | 210-261 `detail` | 有 `is_disconnected`；其余 POST 无流式 |
| P2 | 434-437 `generate_map` | 服务层 **不检索 KB**（见 explore_learning），与 docstring「均先 retrieve」部分不符 |

---

## 10. `agents/__init__.py`

导出 NE 系符号，**未导出** `CSHFullFeatureAgent`/`CSHAgentRouter`（P2）。

---

## 11. `agents/router.py`（114 行）

| 符号 | 说明 |
|------|------|
| `_resolve_mode` | deep_research / lightweight(无 skill) / full |
| `aquery_stream` | 委托执行器 |

| 级别 | 说明 |
|------|------|
| P1 | L78-91 深度研究 → `DeepResearchOrchestratorAgent`（NE） |
| P2 | L72-75 lightweight 仍构建完整 `NEFullFeatureAgent` 图再委托 `LightweightQAAgent`，**重复建图开销** |
| P2 | `**kwargs` 无白名单，易传入未知参数 |

---

## 12. `agents/csh_router.py`（118 行）

与 `router.py` 相同结构；L77-95 注释讨论 CSH 工具但未改实现（**P0**）。

---

## 13. `agents/ne_full_feature_agent.py`（~1040 行）

**职责**：NE 主智能体——建 LangGraph、工具集、流式事件循环、轻量委托。

| 符号 | 行号（约） |
|------|-----------|
| `KB_IDS` / `DEFAULT_SELECTED_SKILLS` | 59-63 |
| `_build_agent` | 161-222 |
| `aquery_stream` | 550-1040 |
| 深研超时循环 | 722-749+ |

| 级别 | 位置 | 说明 |
|------|------|------|
| P2 | L43-47 | 导入时写 `DEER_FLOW_CONFIG_PATH` 环境变量，全局副作用 |
| P2 | L140 | `_normalize_selected_skills` **强制合并** `DEFAULT_SELECTED_SKILLS`，用户传 `selected_skills=[]` 仍含 nuclear-electronics |
| P2 | L146 | Summarization 硬编码 `MiniMax-M2.7` |
| P2 | L575 | `recursion_limit=100` 写死 |
| P2 | L591-592 vs 619 | 轻量路径 `reset_registry` 两次导入别名，全功能路径再 reset |
| P2 | L636-646 | 工具预算 dict 嵌套三元表达式，难读且 academic-search 分支易错 |
| P2 | L686-710 | 非深研模式 checkpoint **续跑**（`INCOMPLETE_ROUND_STREAM_RETRY_MAX`），复杂度高 |
| OK | L628-634 | `NEAgentStreamHandler` 统一出口 + 图片 token 解析 |

**依赖**：`ToolAssemblyPipeline`、`NEAgentStreamHandler`、`create_agent_graph`

**测试缺口**：深研总超时 + grace；html-ppt finalize 注入（stream_handler L401-424）。

---

## 14. `agents/csh_full_feature_agent.py`（~966 行）

与 §13 平行，差异：

| 级别 | 位置 | 说明 |
|------|------|------|
| P0 | 552-570 | 轻量 → `LightweightQAAgent`（NE Prompt） |
| P2 | 574-578 | **双次** `reset_turn_kb_context_budget` |
| P2 | 35 | 仍用 `NEAgentStreamHandler` 类名 |
| P2 | `KB_IDS` | 单库 UUID，与 NE 双库不同 |

---

## 15. `agents/lightweight_qa_agent.py`（~1349 行）

**职责**：旁路检索（KB/Web/文件索引）+ 单次 `astream` 作答，不走路由工具循环。

| 符号 | 说明 |
|------|------|
| `LIGHTWEIGHT_SYSTEM_PROMPT` | L39+ **核电子学** |
| `_compress_cache` / `_compress_tasks` | L27-28 模块级 |
| `aquery_stream` | L475+ 分阶段计时日志 |

| 级别 | 位置 | 说明 |
|------|------|------|
| P0 | L39-127 | 领域写死，CSH/explore 误用 |
| P1 | L27-28 | 缓存/任务 dict **无 TTL**，thread_id 泄漏 |
| P2 | L501-508 | `self._thread_id` 为 API 传入的 **request_thread_id**，与 `effective_config` 内 internal key 可能不一致，文件索引或找错会话 |
| P2 | L926+ | `asyncio.create_task(_produce_model_chunks())` 生产者模式，取消时需验证 |
| P2 | 全文 | 与 `NEFullFeatureAgent` 轻量能力重叠，双路径行为难对齐 |

**测试缺口**：仅 KB / 仅 Web / 有文件索引；`skip_kb` 决策；压缩缓存命中。

---

## 16. `agents/deep_research_orchestrator_agent.py`（60 行）

| 级别 | 位置 | 说明 |
|------|------|------|
| P0 | L11, L17 | `class DeepResearchOrchestratorAgent(NEFullFeatureAgent)` |
| OK | L49-58 | 主控移除 `multi_kb_search`/`web_search`，委派子智能体 |
| OK | L34-45 | 编排硬规则 Prompt 追加 |

**建议**：`CSHDeepResearchOrchestratorAgent(CSHFullFeatureAgent)` + runner 分支。

---

## 17. `agents/research_draft_agent.py`（178 行）

| 级别 | 位置 | 说明 |
|------|------|------|
| OK | L36-42 | `_sanitize_thinking_text` |
| OK | L85-96, 151-163 | 失败 fallback 清单 |
| P2 | L44-74 vs L98-132 | `build_draft` 与 `build_draft_stream` Prompt **逐字重复** |
| P2 | L161-162 | fallback 逐字符 yield，长文本 SSE 事件过多 |
| P3 | — | 草案无领域区分（可接受） |

---

## 18. `agents/support/prompts.py`（~177 行）

NE 系统 Prompt、`build_system_prompt()`、业务能力块。与 `csh_prompts.py` 镜像（P2）。`SUPPRESSED_TOOL_RESULT_TOOLS` 控制流式工具结果展示。

---

## 19. `agents/support/csh_prompts.py`（~177 行）

CSH 领域 Prompt 镜像。`AGENT_ROLE_NAME = 场地污染...`（L5 附近）。应抽 `base_prompt + domain_snippet`。

---

## 20. `agents/support/explore_prompts.py`（~173 行）

`build_topics_prompt` / `build_map_prompt` / `build_expand_prompt` / `build_quiz_prompt` 等，JSON 约束明确。`_context_block` 空检索时要求不编造数值（OK）。

---

## 21. `agents/support/follow_up_prompts.py`（63 行）

`build_follow_up_prompt` 按 skill/工具摘要选 hint。LaTeX 格式要求完整。无问题。

---

## 22. `agents/support/tool_assembly.py`（~250 行）

| 级别 | 位置 | 说明 |
|------|------|------|
| P2 | L56 | `acdemic_search_tool` 拼写 |
| P2 | L93-107 | `nuclear-electronics` 未 load skill 时拦截 multi_kb_search |
| OK | L124-126 | lightweight 返回 `[]` |
| P2 | L187-201 | `has_thread_file_index(self._thread_id)` 须与上传时索引的 thread_id 一致（外部 id） |
| OK | L227-248 | skill 声明工具缺失时 warning |

---

## 23. `agents/support/artifact_utils.py`（201 行）

`build_registered_artifact_metadata`：MinIO 登记 + 前端 metadata。P2：日志 `[NEAgentArtifact]`（L181）。`guess_*` 工具函数质量高。

---

## 24. `agents/support/__init__.py`

仅导出 NE 侧符号，未导出 `csh_prompts`（P3）。

---

## 25. `agents/streaming/stream_handler.py`（~987 行）

**职责**：LangGraph `astream_events` → SSE 事件翻译。

| 子模块 | 行号（约） |
|--------|-----------|
| `handle` | 245-316 |
| `_on_model_stream` | 447+ |
| `_on_tool_end` | 688+ |
| `finalize` | 398+ |
| `log_session_diagnostic` | 318-396 |

| 级别 | 说明 |
|------|------|
| P2 | 单类 >900 行，建议按 `on_model_*` / `on_tool_*` 拆分 |
| OK | 低可见输出诊断、html-ppt 失败 hint、工具轮过滤 chunk |
| P2 | L644 等 bare `except Exception` |
| P2 | 类名 NE 前缀，CSH 复用 |

**思考标签**：stream_handler **不负责** `` 拆分，由 API 层或 guest 负责 → 主站 agent 漏洞根因。

---

## 26. `agents/streaming/stream_utils.py`（~507 行）

纯函数：`make_process_event`、`iter_sse_subchunks`、`extract_web_search_metadata`、`extract_task_runtime_event_payload` 等。结构清晰，宜单测。

---

## 27. `agents/streaming/stream_timeouts.py`（35 行）

环境变量：`STREAM_IDLE_TIMEOUT_SECONDS`(180)、`MODEL_ROUND_IDLE_TIMEOUT_SECONDS`(300) 等。`resolve_normal_stream_idle_timeout` 在深研时抛 `ValueError` 防误用（OK）。

---

## 28. `agents/streaming/image_url_registry.py`（156 行）

ContextVar + `StreamingTokenResolver` 防截断 `[IMAGE:xxx]`。文档 L8-11 说明子 Task 共享引用（OK）。`register` 幂等（L50-51）。

---

## 29. `agents/streaming/__init__.py`

合理 re-export。

---

## 30. `tools/request_limits.py`（88 行）

`ContextVar`：`consume_tool_budget`、`TURN_MAX_EVIDENCE_CHARS`(8000)、`get_multi_kb_request_lock`。设计良好。注意 **每次请求** 须调用 `reset_turn_kb_context_budget()`（agent 已调，guest 轻量路径需确认）。

---

## 31. `tools/acdemic_search_tool.py`（~973 行）

| 级别 | 位置 | 说明 |
|------|------|------|
| P3 | 文件名 `acdemic` |
| P2 | L33 `logging.basicConfig` |
| P2 | L91-106 `_run_async` | 在已有 event loop 时 **ThreadPoolExecutor(1)** 跑异步搜索；高并发可能线程堆积 |
| P2 | L875+ | `@tool` **同步** `academic_search`，LangGraph 工具节点同步 invoke |
| OK | 多源并发、去重、限流退避、env API Key |

**测试缺口**：无 Key 时限流；`databases` 默认 None 行为。

---

## 32. `tools/multi_kb_search_tool.py`（~1223 行）

| 符号 | 说明 |
|------|------|
| `execute_multi_kb_search` | L877+ 主入口 |
| `multi_kb_search_tool` | L847 LangChain tool |
| 并行锁 | L914-920 |

| 级别 | 位置 | 说明 |
|------|------|------|
| OK | L914-928 | 禁止并行 multi_kb + 字符预算 |
| P2 | L942 | `effective_kb_ids = kb_ids or [DEFAULT_KB_ID, PAPER_KB_ID]` 默认 NE 库 |
| P2 | L955-956 | 线程名 `subagent-exec-` 判断子智能体超时 |
| P2 | 文件体量 | 建议拆：检索 / 格式化 / 图片白名单 |
| P2 | L991+ | `asyncio.create_task` 并行 KB |

---

## 33. `tools/chat_file_search_tool.py`（~102 行）

`create_scoped_chat_file_search_tool(thread_id)` 闭包绑定索引；`top_k` clamp 1-10。`has_thread_file_index` 供 tool_assembly 使用。质量良好。

---

## 34. `tools/async_web_search_tool.py`（~143 行）

`async_web_search_tool`：ddgs bing cn-zh；`consume_tool_budget("web_search")`；`_sanitize_search_query` 剥 runtime/thinking。`timelimit` 默认 `"y"`。OK/P2：ddgs 不可用时返回 JSON error，无重试退避。

---

## 35. `tools/deep_research_report_tool.py`（~367 行）

| 级别 | 位置 | 说明 |
|------|------|------|
| P2 | L199-215 | `subprocess.run(pandoc ...)` 依赖宿主机 pandoc |
| OK | L45-74 | 中文文件名安全处理 |
| P2 | L222-237 | `_resolve_runtime_thread_id` 兼容 config/context |
| OK | MinIO 上传 + Command 更新 state |

---

## 36. `tools/html_ppt_tool.py`（~949 行）

读 `backend/skills/public/html-ppt` 模板；`_rewrite_asset_urls` 静态资源前缀。P2：体积大；需防路径穿越（应用内模板路径应固定）。`parse_internal_thread_key` 解析用户。

---

## 37. `tools/excel_engine.py`（~524 行）

DuckDB 缓存 `_cache` / `_multi_table_maps`：**无淘汰**（P2）。`_file_hash` 用 mtime+size（OK）。MinIO 下载。线程锁 `_cache_lock`。

---

## 38. `tools/excel_data_analysis_tool.py`（~588 行）

5+ 工具；`file_path` 模式使用用户传入路径（P2：**路径需校验**在 agent 沙箱外，依赖上游只允许受控路径）。大量 `return _error(str(e))`。

---

## 39. `tools/excel_chart_generator.py`（~468 行）

图表生成，依赖 excel_engine。部分 silent except（P2）。

---

## 40. `tools/__init__.py`

占位。

---

## 41. `tasks/__init__.py`

模块 docstring，OK。

---

## 42. `tasks/manager.py`（~395 行）

| 符号 | 说明 |
|------|------|
| `BackgroundTask` | 内存队列 + DB flush |
| `TaskManager` | 单例 `_tasks` |
| `read_task_from_db` 等 | 跨进程 |

| 级别 | 位置 | 说明 |
|------|------|------|
| P1 | L298-300 | `remove_task` **无调用方** |
| P1 | L28 `MAX_EVENTS=10000` | metadata JSON 膨胀 |
| OK | L207-252 | `subscribe` 原子注册防丢 done |
| P2 | L96 | `cancel()` 内 `create_task(push_event)` |
| P2 | L169-174 | flush 失败重排队列 |
| P2 | L327-395 | 每次 `read_task_from_db` 新建 session |

---

## 43. `tasks/runner.py`（~356 行）

| 级别 | 位置 | 说明 |
|------|------|------|
| P0 | L212-222 | `DeepResearchOrchestratorAgent` 无 CSH |
| P1 | L47-84 | 第四份 `_split_thinking_segments`（此处**已使用** L265） |
| OK | L244 | DB + 内存取消检测 |
| P2 | L206-208 | 初始 chunk 硬编码 emoji 文案 |
| P2 | L350-356 | finally 关 session，不 `remove_task` |

---

## 44. `services/__init__.py`

占位。

---

## 45. `services/explore_learning.py`（~676 行）

| 函数 | 检索 KB? | 说明 |
|------|---------|------|
| `generate_topics` | 否 | 预制 shuffle（OK） |
| `generate_map` / `expand_node` | **否** | L434-435 注释故意不检索；与文件头 L4「均先 retrieve」**文档不一致** |
| `gather_resources` | 是 | `retrieve_context` |
| `generate_quiz` | 是 | |
| `download_kb_resource` | — | MinIO 多候选路径 |

| 级别 | 说明 |
|------|------|
| P1 | 开放 API 无鉴权（调用方 explore.py） |
| P2 | L294-315 | 使用 `storage._get_minio_client` 私有 API |
| P2 | `expand_node` fallback 为空时返回 `source: fallback` 但 children 可能 []（L478） |
| OK | `_llm_json` 超时 + 模板兜底 |

---

## 46. `services/follow_up_suggestions.py`（~382 行）

| 级别 | 位置 | 说明 |
|------|------|------|
| OK | L83-141 | LLM + timeout + fallback |
| OK | L237+ | `persist_follow_up_to_checkpoint` best-effort |
| P2 | L44-76 | `note_process_event` 工具摘要启发式，可能漏工具 |
| P2 | — | 模块级 `get_follow_up_service()` 单例（需读文件末尾确认） |

---

## 47. `workflow/__init__.py` / `workflow/research_state.py`

状态枚举 9 态 + `RESEARCH_WORKFLOW_TERMINAL_STATES`。`normalize_research_workflow_state` 拒绝未知值（OK）。无业务逻辑。

---

## 48. `utils/checkpointer.py`（14 行）

`get_checkpointer_from_request` → `app.state.checkpointer`。未初始化抛 `RuntimeError`（非 HTTPException）。类型注解绑定 `AsyncSqliteSaver`，若生产用 Postgres checkpointer 类型 lie（P2）。

---

## 49. `config/kb_config.py`（34 行）

`KB_CONFIGS` / `DEFAULT_KB_GROUP`：**全仓库无 import**（死代码）。ID 与 `NEFullFeatureAgent.KB_IDS` 重叠（P3）。

---

## 50. `config/__init__.py` / `middlewares/__init__.py`

占位；middlewares 无实现（P3）。

---

## 51. 跨文件调用链（深度研究）

```
csh_agent.query → run_deep_research_background
  → DeepResearchOrchestratorAgent (NE prompts)
    → subagent deep-research
      → multi_kb_search (默认 NE kb ids)
```

```
explore.detail → CSHFullFeatureAgent(lightweight)
  → LightweightQAAgent (NE LIGHTWEIGHT_SYSTEM_PROMPT)
```

---

## 52. 建议单测矩阵（按文件）

| 文件 | 优先用例 |
|------|----------|
| `agent.py` | chunk 含 `` 不泄漏；workflow 恢复 |
| `guest.py` | 与 agent 思考过滤行为一致（回归） |
| `runner.py` | CSH domain 应用编排器 |
| `lightweight_qa_agent.py` | subject 参数化后 CSH prompt |
| `manager.py` | terminal 后 remove_task + metadata 裁剪 |
| `multi_kb_search_tool.py` | 并行锁 + 字符预算耗尽 |
| `acdemic_search_tool.py` | `_run_async` 在 running loop 下不死锁 |
| `explore_learning.py` | map/expand fallback children 非空 |

---

## 53. 文档索引

| 文档 | 内容 |
|------|------|
| [backend_custom_code_review.md](./backend_custom_code_review.md) | P0-P3 总览与修复顺序 |
| [backend_custom_per_file_review.md](./backend_custom_per_file_review.md) | 每文件摘要表 |
| **本文件** | 每文件深度：符号、行号、依赖、测试 |

---

*生成方式：静态阅读 + 符号/行号 grep；第二轮补充未覆盖的实现细节。*
