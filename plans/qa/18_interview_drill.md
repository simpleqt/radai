# 18 · 快问快答冲刺（140 题）

> 每题一行问题 + 短答，用于**考前 30 分钟**或面试官「连环追问」。详细版见各模块文档。

---

## 架构（01）

| # | 问题 | 短答 |
|---|------|------|
| 1 | 默认主 API 端口？ | 8082 |
| 2 | 访客 API 端口？ | 8083 |
| 3 | CLI 启 API 命令？ | `doc-agent run-api` |
| 4 | Agent 建图入口？ | `create_agent_graph()` |
| 5 | 配置三层？ | DB / config.yaml / .env |
| 6 | checkpoint 存哪？ | SQLite checkpoints.db |
| 7 | Agent 四层架构？ | domain/agents → application/ → core/agents → langchain.agents |
| 8 | NE 与 CSH 域差什么？ | Prompt、AgentProfile YAML、KB 绑定 |
| 9 | lifespan 预热什么？ | RAG、keyword、checkpointer |
| 10 | MCP 与 v2 SSE 同通道吗？ | 否 |

---

## Agent（02）

| # | 问题 | 短答 |
|---|------|------|
| 11 | WorkflowRegistry 三 mode？ | lightweight / full / deep_research（+ plan_solve / multi_agent_*） |
| 12 | 何时 lightweight？ | 未选 Skill |
| 13 | Lightweight 走 LangGraph 工具循环吗？ | 通常否，预检索+单次 LLM |
| 14 | ContextPruning 两大机制？ | State Elevation + JIT |
| 15 | evidence 存在 state 哪字段？ | retrieved_evidence |
| 16 | Skill 内容存 state 哪字段？ | loaded_skills |
| 17 | 子 Agent 为何独立 loop？ | 防嵌套 await 死锁 |
| 18 | ToolGuardrail 在哪一层？ | 工具执行前 |
| 19 | 深研状态机第一步？ | draft 大纲 |
| 20 | CoordinatedAgentWorkflow 大纲阶段 SSE？ | draft_chunk |
| 21 | ThreadState reducer 作用？ | checkpoint 合并去重 |
| 22 | clarification 何时开？ | full 且非 deep/lightweight |
| 23 | create_agent_graph 生产入口？ | 是，由 ProfiledGraphBuilder 调用 |
| 24 | JIT 默认字符上限？ | 8000 |
| 25 | 深研确认后谁跑后台？ | CoordinatedAgentWorkflow（via runner） |

---

## RAG（03）

| # | 问题 | 短答 |
|---|------|------|
| 26 | RAG 框架？ | LightRAG + RAGAnything |
| 27 | 向量库？ | Milvus |
| 28 | 图谱？ | NebulaGraph |
| 29 | Stage-1 四 Path？ | A/B/C/D |
| 30 | domain Profile Stage-2？ | MMR_ONLY |
| 31 | general_qa 关哪 Path？ | Path-A |
| 32 | multi_kb 跨库融合？ | RRF |
| 33 | 关键词 hybrid 含？ | vector+BM25+LLM+RRF |
| 34 | PATH_A_QUERY_ONLY 效果？ | 减少 keyword_index 次数 |
| 35 | get_rag_service 作用？ | RAG 单例 |
| 36 | PPR 在哪个 Profile？ | default FULL |
| 37 | RAW 模式含义？ | 跳过 Stage-2 |
| 38 | ingest 谁消费 pending？ | Ingest Worker |
| 39 | Nebula 挂掉影响？ | Path-B/D 降级 |
| 40 | Profile 硬编码 kb_id 原因？ | 子线程无法查 DB |

---

## API / SSE（04）

| # | 问题 | 短答 |
|---|------|------|
| 41 | SSE Content-Type？ | text/event-stream |
| 42 | 正文事件 type？ | chunk |
| 43 | 工具进度 type？ | event / status |
| 44 | 追问建议 type？ | follow_up_suggestions |
| 45 | StreamHandler 类名？ | StreamHandler + AgentEvent + SSEEncoder |
| 46 | 事件来源？ | LangGraph astream_events |
| 47 | 流式 ASGI 坑？ | 不能二次 response.start |
| 48 | internal thread key 目的？ | user+thread 隔离 |
| 49 | v1 RAG 流式路径？ | /api/v1/rag/query_stream |
| 50 | ping 行格式？ | `: ping timestamp` |

---

## DB / 认证（05）

| # | 问题 | 短答 |
|---|------|------|
| 51 | async MySQL 连接池？ | NullPool |
| 52 | sync MySQL 连接池？ | QueuePool |
| 53 | Token Redis key？ | auth:token:{token} |
| 54 | API Key 查哪？ | MySQL api_keys |
| 55 | 会话元数据表？ | conversations |
| 56 | CAS 防什么攻击？ | 开放重定向 |
| 57 | require_admin 查什么？ | role==admin |
| 58 | internal_api_secret 防什么？ | 内部检索接口 |
| 59 | 删会话清 MinIO 吗？ | 是 ScopedStorage |
| 60 | rollback 参数？ | keep_user_turns |

---

## 工具 / Skills（06）

| # | 问题 | 短答 |
|---|------|------|
| 61 | Skill 文件入口？ | SKILL.md |
| 62 | 读 Skill 工具？ | read_local_skill_file |
| 63 | multi_kb 默认几库？ | 核电子+论文库 |
| 64 | chat_file 用什么索引？ | BM25+TF-IDF 内存 |
| 65 | ToolAssembly 输入？ | selected_skills 等 |
| 66 | html-ppt 对应工具？ | html_ppt_tool |
| 67 | 工具限流模块？ | request_limits |
| 68 | Skill 是 Tool 吗？ | 否，Markdown 工作流 |
| 69 | ScopedSkillsProvider 作用？ | Skill 可见范围 |
| 70 | sandbox 与主路径？ | 可选沙箱运行时，主路径默认不用 |

---

## Worker（07）

| # | 问题 | 短答 |
|---|------|------|
| 71 | Task Worker 触发？ | Kafka |
| 72 | Ingest Worker 触发？ | DB pending |
| 73 | 深研走 Kafka 吗？ | 否 |
| 74 | PendingFile 成功 status？ | 2（见模块文档） |
| 75 | kafka admin 做什么？ | 创建 topic |
| 76 | 文档 OCR 在哪 Worker？ | Task Worker |
| 77 | index sync 何时跑？ | 定时调度 |
| 78 | 双轨为何存在？ | 历史+实时 ingest 分工 |
| 79 | ingest 日志？ | ingest_worker.log |
| 80 | CLI 启 ingest？ | run-ingest-worker |

---

## 模型 / KB / 其他（09–17）

| # | 问题 | 短答 |
|---|------|------|
| 81 | create_chat_model 默认配置源？ | db |
| 82 | thinking 默认策略？ | 关，流式仍剥离 |
| 83 | KB Manager loop 本地原因？ | 跨 event loop |
| 84 | Explore 先检索再 LLM？ | 是 retrieve_context |
| 85 | PRESET_TOPICS 目的？ | 零等待主题列表 |
| 86 | ASR 路由？ | api/routers/asr.py |
| 87 | 数据集查询引擎？ | DuckDB |
| 88 | artifacts 事件用途？ | 报告/PPT 下载元数据 |
| 89 | Agent-KB 绑定表？ | AgentKnowledgeBase |
| 90 | Redis 模型热更新？ | Pub/Sub |
| 91 | 优雅降级例子？ | RAG 预热超时仍启动 API |
| 92 | 技术债 P0 think？ | 主站过滤未统一 |
| 93 | 多 worker checkpoint 问题？ | SQLite 写锁 |
| 94 | 间接 Prompt 注入来源？ | 恶意 KB chunk |
| 95 | Allowlist vs Prompt？ | Allowlist 硬拦截 |
| 96 | 访客 checkpoint 文件？ | guest_checkpoints.db |
| 97 | WorkflowRegistry 解析优先级？ | 显式 workflow > Skill preferred > profile default |
| 98 | 部署最小三件套？ | MySQL+Redis+LLM |
| 99 | 模块文档导航？ | docs/docs_backend/README.md |
| 100 | 图表总集？ | plans/qa/00_architecture_diagrams.md |

---

## 智能体架构专章（19 专题 · 101–120）

| # | 问题 | 短答 |
|---|------|------|
| 101 | ProfiledGraphBuilder 组装 middleware 与 features 能同传吗？ | 否，ValueError |
| 102 | 8 层链第 0 层？ | DanglingToolCall（始终挂载） |
| 103 | Clarification 在链的什么位置？ | 最后 |
| 104 | ContextPruning 在工厂链内吗？ | 否，extra_middleware |
| 105 | lightweight ToolAssembly 返回？ | 空列表 [] |
| 106 | Lightweight 谁做检索？ | execute_multi_kb_search 内联 |
| 107 | full 模式图事件从哪来？ | graph.astream_events |
| 108 | Handler 管什么？ | 事件→SSE 协议 |
| 109 | task 工具配置在哪？ | task_tool _SUBAGENT_CONFIGS |
| 110 | 子 Agent 能再调 task 吗？ | deep-research 禁止 |
| 111 | set_tool_budgets 作用域？ | 请求级 ContextVar |
| 112 | Guardrail 与 ToolError 区别？ | 执行前 vs 执行后异常 |
| 113 | Summarization 无内置默认？ | 须传 Middleware 实例 |
| 114 | vision=True 挂什么？ | ViewImageMiddleware |
| 115 | @Next 装饰器干什么？ | 锚点插入 extra_middleware |
| 116 | lightweight 为何仍安全？ | 无 tool_call 面 |
| 117 | explore__ thread 优化？ | use_direct_query |
| 118 | deep 模式 multi_kb budget？ | 通常 3/轮 |
| 119 | 子 Agent 跑在哪？ | 独立线程+loop |
| 120 | Agent 专题详解？ | 19_agent_architecture_implementation.md |

---

## Middleware 快问（121–130）

| # | 问题 | 短答 |
|---|------|------|
| 121 | LoopDetection 挂哪层？ | 工厂链第 6 层，始终挂载 |
| 122 | ToolErrorHandling 捕获什么？ | 工具执行异常→友好 ToolMessage |
| 123 | ViewImage 触发条件？ | vision=True，before_model |
| 124 | SubagentLimit 限制什么？ | task 并发/深度，防 token 风暴 |
| 125 | Dangling 与 Guardrail 谁先？ | Dangling 第 0，Guardrail 第 1 |
| 126 | JIT 注入在哪个钩子？ | ContextPruning before_model |
| 127 | evidence 提升在哪个钩子？ | ContextPruning after_agent |
| 128 | Lightweight 走 8 层链吗？ | 否，ProfiledAgentExecutor(lightweight_mode=True) 单次 LLM |
| 129 | Clarification 拦截哪个工具？ | ask_clarification |
| 130 | Middleware 详题？ | 21_middleware_chain_deep_dive.md |

---

## 深度研究快问（131–140）

| # | 问题 | 短答 |
|---|------|------|
| 131 | WorkflowRegistry 深研优先级？ | workflow=deep_research 或 Skill preferred_workflow=deep_research |
| 132 | 大纲阶段由谁负责？ | CoordinatedAgentWorkflow 内置大纲阶段 |
| 133 | 确认后后台入口？ | CoordinatedAgentWorkflow 启动子 Agent 任务 |
| 134 | Coordinated 编排器能直接 KB 吗？ | 否，工具被 filter，子 Agent 独立检索 |
| 135 | 子 Agent type 字符串？ | deep-research |
| 136 | 同轮几个 task？ | Prompt 要求 2 个并行 |
| 137 | 报告工具名？ | save_deep_research_report |
| 138 | 进度 SSE 路径？ | GET task/{id}/stream |
| 139 | 工作流状态存哪？ | Conversation.metadata_.research_workflow |
| 140 | 深研专章？ | 25_deep_research_orchestrator.md |

---

## 使用建议

1. 自测：遮住「短答」列，看能否答出 **112/140**（80%）
2. 面试前：错项回查对应模块 L2 详解
3. 面试官：从 121–140 抽 10 题连环追问（Agent 岗）
