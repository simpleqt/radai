# backend/custom 逐文件代码审查

**关联总览**：[backend_custom_code_review.md](./backend_custom_code_review.md)  
**审查日期**：2026-05-30  
**说明**：按文件路径列出问题、风险与建议；未修改任何源码。  
**优先级**：P0 严重 / P1 高 / P2 中 / P3 低 / OK 无明显问题

**深度审查（第二部分）**：[backend_custom_per_file_review_detailed.md](./backend_custom_per_file_review_detailed.md)

---

## 根目录

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `__init__.py` | 1 | OK | 仅占位注释，无导出。 |

| `skills_mysql_dynamic_management_plan.md` | ~1220 | P3 | 设计文档放在运行时代码目录，易与模块混淆；建议迁至 `plans/` 或 `docs/`。 |

---

## api/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `api/__init__.py` | 1 | OK | 占位。 |
| `api/guest_conversations.py` | 74 | P2 | **导入路径不一致**：使用 `backend.core.config.build_internal_thread_key`，与同包 `agent.py` 的 `backend.core.agents.thread_key` 功能相同但路径分裂。列表接口对每个会话 `await _has_checkpoint`，N+1 查询 checkpoint，会话多时延迟高。`limit * 3` 预取后过滤，偏移分页在过滤后不准确。 |

### api/v2/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `api/v2/__init__.py` | 16 | OK | 正确聚合 agent/guest/csh/explore 路由。 |
| `api/v2/agent.py` | ~1354 | P0/P1 | **与 csh_agent 大量重复**（见总览 3.1）。**思考标签过滤未接入 chunk 路径**（定义 `_split_thinking_segments` 但流式未调用；`stripped_chunk_count` 等死变量）。**未使用**：`_normalize_intent_payload`、`_normalize_plan_payload`、未使用的 `create_chat_model` 导入。**`intent_mode` 仅打日志**。流式开始前读 `conversation.metadata_` 时 `conversation` 仍为 None（竞态）。`asyncio.create_task(run_deep_research_background)` / `persist_follow_up` 无生命周期管理。异常 `str(e)` 外泄（HTTP/SSE）。`stream_session` 与 Depends session 双开。文件注入逻辑与 csh 重复 ~70 行。 |
| `api/v2/csh_agent.py` | ~1243 | P0/P1 | 与 `agent.py` 同构问题；另：**后台深度研究未区分 CSH 编排器**（调用通用 `run_deep_research_background` → NE 编排器）。`reset_turn_kb_context_budget()` **连续调用两次**（L577-578）。其余同 agent.py。 |
| `api/v2/guest.py` | ~205 | P1/P2 | **正确实现**思考标签拆分（与主站不一致）。固定 `NEFullFeatureAgent` + `lightweight_mode=True`，符合访客 NE 场景。`resolve_device_id` 对 header 清洗合理。缺 `request.is_disconnected()` 检测。异常信息外泄。`_split_thinking_segments` 等与 agent 重复实现。 |
| `api/v2/explore.py` | ~272 | P1/P2 | **多数路由无鉴权**（topics/map/expand/resources/quiz/detail），仅 download 需登录；LLM/检索可被滥刷。`detail` 流式 **无断开检测**（有 explore 有，agent 主站有）。`domain=csh` 时轻量链路仍走 NE Prompt（经 CSHFullFeatureAgent → LightweightQAAgent）。**调用私有 API**：`svc._build_content_disposition`。`_first_kb` 只取列表首个 kb_id，多库场景能力浪费。 |

---

## agents/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `agents/__init__.py` | 15 | P2 | **未导出** `CSHFullFeatureAgent`、`CSHAgentRouter`；外部需深路径导入，易遗漏公共 API 面。 |
| `agents/router.py` | 114 | P1 | `lightweight` 模式通过 `_build_full_agent(lightweight_mode=True)` 间接走 `LightweightQAAgent`，非独立类。`deep_research` 固定 `DeepResearchOrchestratorAgent`（NE）。`kwargs` 透传，类型约束弱。 |
| `agents/csh_router.py` | 118 | P0 | 与 `router.py` 平行；深度研究仍用 **NE** `DeepResearchOrchestratorAgent`（L77-95 长注释未改变行为）。 |
| `agents/ne_full_feature_agent.py` | ~1040 | P2 | 核心主智能体，职责过重（图构建、流式、工具、轻量委托、深研超时）。启动时可能写 `DEER_FLOW_CONFIG_PATH` 环境变量（副作用）。`KB_IDS` / `DEFAULT_SELECTED_SKILLS` 硬编码，与 DB `agent_kb_bindings` 并存需保持一致。历史消息 `[IMAGE:xxx]` 清理逻辑复杂。多处 `except Exception: pass`。 |
| `agents/csh_full_feature_agent.py` | ~966 | P0/P2 | 与 NE 版结构高度平行；轻量模式委托 **NE 向** `LightweightQAAgent`（P0）。`reset_turn_kb_context_budget` 重复调用。复用 `NEAgentStreamHandler`（命名与领域不符，但行为可共用）。 |
| `agents/lightweight_qa_agent.py` | ~1349 | P0/P2 | **系统 Prompt 写死核电子学**（P0）。模块级 `_compress_cache` / `_compress_tasks` **无界增长**。并行检索 + 多阶段 LLM，单文件过大难测。大量 `except Exception`。与 `ne_full_feature` 轻量路径功能重叠，长期应合并或参数化 domain。 |
| `agents/deep_research_orchestrator_agent.py` | 60 | P0 | **继承 NEFullFeatureAgent**；CSH 深度研究语义错误（P0）。`_get_tools` 屏蔽主智能体直接检索，设计合理。 |
| `agents/research_draft_agent.py` | 178 | OK/P2 | 草案生成逻辑清晰，有 thinking 清洗与 fallback。`build_draft` 与 `build_draft_stream` **Prompt 重复**。草案 Prompt **领域中立**（CSH/NE 共用可接受）。`normalize_confirmed_plan` 仅保留 `outline_text`，结构简单。 |

### agents/support/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `agents/support/__init__.py` | - | OK | 包初始化。 |
| `agents/support/prompts.py` | ~177 | P2 | NE 专用业务能力与图片规则；与 `csh_prompts` 大量平行。`SUPPRESSED_TOOL_RESULT_TOOLS` 合理。 |
| `agents/support/csh_prompts.py` | ~177 | P2 | CSH 领域 Prompt；与 `prompts.py` 结构镜像，应抽公共模板 + 领域片段。 |
| `agents/support/explore_prompts.py` | ~173 | OK | 探索学习 JSON 约束清晰，`_context_block` 空上下文有兜底说明。 |
| `agents/support/follow_up_prompts.py` | - | OK | 追问建议 Prompt（被 follow_up_suggestions 引用）。 |
| `agents/support/tool_assembly.py` | ~250 | P2 | 工具组装中心；`academic_search` 映射到拼写错误的 `acdemic_search_tool`。`nuclear-electronics` skill 门禁与 `multi_kb_search` 耦合。`lightweight_mode` 返回空工具列表正确。`chat_file_search` 注入依赖 `thread_id` 为**外部** thread_id，需与 API 传入一致（当前 API 传 `request_thread_id`）。 |
| `agents/support/artifact_utils.py` | 201 | OK/P2 | MIME/语言猜测完善。MinIO 注册失败降级合理。日志前缀仍写 `[NEAgentArtifact]`，CSH 共用时应泛化。 |

### agents/streaming/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `agents/streaming/__init__.py` | - | OK | 包初始化。 |
| `agents/streaming/stream_handler.py` | ~987 | P2 | **上帝类**：模型流/工具/链/子任务事件全在一处。低可见输出 WARNING 有助排查。类名 `NEAgentStreamHandler` 被 CSH 复用。`except Exception` 吞工具元数据错误。 |
| `agents/streaming/stream_utils.py` | ~507 | OK | SSE 载荷、工具输出摘要、web_search 解析、子任务元数据提取；工具函数职责清晰。 |
| `agents/streaming/stream_timeouts.py` | 35 | OK | 环境变量可配置超时；`resolve_normal_stream_idle_timeout` 对 deep_research 显式抛错防误用。 |
| `agents/streaming/image_url_registry.py` | 156 | OK | ContextVar + 流式 token 边界处理设计良好；注释说明子 Task 共享引用，便于维护。 |

---

## tools/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `tools/__init__.py` | 1 | OK | 占位。 |
| `tools/request_limits.py` | 88 | OK | ContextVar 预算与 multi_kb 锁设计正确；`TURN_MAX_EVIDENCE_CHARS` 可 env 配置。 |
| `tools/acdemic_search_tool.py` | ~973 | P2/P3 | **文件名拼写错误** `acdemic`。模块级 **`logging.basicConfig`** 可能污染全局日志。多数据源并发、限流、去重实现完整。API Key 均从环境变量读取（OK）。 |
| `tools/multi_kb_search_tool.py` | ~1223 | P2 | 功能密集：RRF、图片白名单、字符预算、Path A 关键词。单文件过大。`asyncio.create_task` 并行检索需注意取消传播。错误 JSON 含 `str(e)` 返回模型。 |
| `tools/chat_file_search_tool.py` | ~102 | OK | 会话作用域工具工厂模式清晰；`top_k` 上下界限制。 |
| `tools/async_web_search_tool.py` | ~143 | P2 | 依赖 `ddgs`/必应；查询清洗会剥掉 runtime_time 块（合理）。超时与空结果处理完善。 |
| `tools/deep_research_report_tool.py` | ~367 | P2 | Pandoc **`subprocess.run`** 转 docx（需部署 pandoc）；命令参数来自受控路径，风险可控。`parse_internal_thread_key` 解析线程。同步/异步 MinIO 上传并存。 |
| `tools/html_ppt_tool.py` | ~949 | P2 | 读本地 skill 模板目录；`parse_internal_thread_key` 解析用户。需确保生成 HTML 不上传任意路径。单文件过大。 |
| `tools/excel_engine.py` | ~524 | P2 | DuckDB 全局 `_cache` **无 LRU/过期**，长进程内存涨。MD5 缓存键含 mtime/size，合理。MinIO 下载与本地缓存。 |
| `tools/excel_data_analysis_tool.py` | ~588 | P2 | 多工具入口聚合；大量 `except Exception` 返回错误字符串给模型。 |
| `tools/excel_chart_generator.py` | ~468 | P2 | 图表生成；部分 bare `except Exception`。 |

---

## tasks/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `tasks/__init__.py` | 5 | OK | 模块说明清晰。 |
| `tasks/manager.py` | ~395 | P1 | **`remove_task` 从未被调用** → 内存泄漏。`MAX_EVENTS=10000` 单任务 events 可很大。DB flush 失败会重排队列（好），但 `except Exception` 过宽。`cancel()` 内 `create_task(push_event)` 未 await。跨进程 DB 辅助函数每次新建 session（OK）。 |
| `tasks/runner.py` | ~356 | P0/P1 | **固定 NE `DeepResearchOrchestratorAgent`**（P0）。**重复** `_split_thinking_segments`（第四份）。思考过滤在此路径**已启用**。取消检测含 DB 标志（好）。`finally` 关闭 session。 |

---

## services/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `services/__init__.py` | 1 | OK | 占位。 |
| `services/explore_learning.py` | ~676 | P1/P2 | 预制主题 + LLM 兜底架构合理。**开放 API 无鉴权** 的成本风险。`download_kb_resource` 使用 `_get_minio_client` 私有 API。`_build_content_disposition` 被 API 层私有引用。`retrieve_context` 失败静默返回空上下文。 |
| `services/follow_up_suggestions.py` | ~382 | OK/P2 | 模板 + LLM + 缓存 hash；`note_process_event` 提取工具摘要。`persist_follow_up_to_checkpoint` 由 API fire-and-forget 调用。 |

---

## workflow/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `workflow/__init__.py` | 13 | OK | 正确 re-export 状态枚举。 |
| `workflow/research_state.py` | 34 | OK | 状态机枚举清晰；`normalize_research_workflow_state` 拒绝未知状态，防脏数据。 |

---

## utils/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `utils/checkpointer.py` | 14 | P2 | 从 `app.state` 取 checkpointer；未初始化时 `RuntimeError` 直接抛出（explore/detail 等需确保 lifespan 已挂载）。返回类型注解为 `AsyncSqliteSaver`，与 guest/主站可能的多后端 checkpointer 耦合。 |

---

## config/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `config/__init__.py` | 1 | OK | 占位。 |
| `config/kb_config.py` | 34 | P3 | **仓库内无引用**（死配置？）。硬编码 KB UUID 与 `NEFullFeatureAgent.KB_IDS`、DB 绑定三套来源，易漂移。若已废弃应删除或迁到 settings/DB。 |

---

## middlewares/

| 文件 | 行数 | 结论 | 问题与说明 |
|------|------|------|------------|
| `middlewares/__init__.py` | 1 | P3 | 仅占位「自定义中间件」，无实现；限流/鉴权若在别处需文档说明。 |

---

## 跨文件问题索引（便于按主题修）

| 主题 | 涉及文件 |
|------|----------|
| CSH 误用 NE 深研/轻量 Prompt | `csh_router.py`, `runner.py`, `deep_research_orchestrator_agent.py`, `csh_full_feature_agent.py`, `lightweight_qa_agent.py`, `explore.py` |
| agent/csh_agent 重复 | `agent.py`, `csh_agent.py` |
| 思考标签拆分 ×4 | `agent.py`, `csh_agent.py`, `guest.py`, `runner.py` |
| 后台任务内存 | `manager.py`, `runner.py`, `agent.py`, `csh_agent.py` |
| 探索无鉴权 | `explore.py`, `explore_learning.py` |
| 异常外泄 | `agent.py`, `csh_agent.py`, `guest.py`, `runner.py` |
| KB ID 多源 | `kb_config.py`, `ne_full_feature_agent.py`, `csh_full_feature_agent.py`, `agent.py` DB 绑定 |
| 拼写 acdemic | `acdemic_search_tool.py`, `tool_assembly.py` |

---

## 建议的逐文件治理顺序

1. **P0 文件**：`deep_research_orchestrator_agent.py` → `runner.py` → `csh_router.py` → `lightweight_qa_agent.py` → `csh_full_feature_agent.py`  
2. **P1 文件**：`agent.py` + `csh_agent.py`（抽公共模块并接上思考过滤）→ `manager.py` → `explore.py`  
3. **P2 文件**：`stream_handler.py` 拆分、`multi_kb_search_tool.py` / `acdemic_search_tool.py` 治理  
4. **P3 清理**：`kb_config.py`、空包、`skills_mysql_*.md` Relocation  

---

*审查方式：静态阅读 + ripgrep；未执行单元测试或运行时 profiling。*
