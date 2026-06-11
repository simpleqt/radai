# 14 · 速查手册（Flashcards）

> 一页一概念，适合考前 15 分钟过一遍。格式：**问题 → 一句话答案**。

---

## 架构

| # | 问题 | 一句话答案 |
|---|------|-----------|
| 1 | 主/访客端口 | 8082 全功能 / 8083 精简 + guest checkpoint |
| 2 | Agent 建图入口 | `core/agents/factory.py` → `create_agent_graph()` |
| 3 | 配置优先级 | DB llm_models → config.yaml → .env |
| 4 | CLI 启 Worker | `run-worker` Kafka / `run-ingest-worker` DB 轮询 |

---

## WorkflowRegistry

| # | 问题 | 一句话答案 |
|---|------|----------|
| 5 | 三 mode | deep_research / lightweight（无 Skill）/ full（+ plan_solve / multi_agent_*） |
| 6 | NE vs CSH 域差异 | 同 ProfiledAgentExecutor，不同 AgentProfile YAML 与 Prompt 域 |
| 7 | lightweight 委托 | ProfiledAgentExecutor(lightweight_mode=True) → LightweightQAAgent |

---

## RAG

| # | 问题 | 一句话答案 |
|---|------|-----------|
| 8 | 向量+图 | Milvus + NebulaGraph（LightRAG patch） |
| 9 | 四 Path | A 实体 / B 关系 / C chunk 向量 / D 图扩展 |
| 10 | domain Profile | 开 Path-A，Stage-2 MMR_ONLY |
| 11 | general_qa Profile | 关 Path-A，Stage-2 RAW |
| 12 | multi_kb 提速 | 关键词一次提取 + PATH_A_QUERY_ONLY |
| 13 | RRF 用在哪 | 跨 KB chunk 融合 |

---

## 上下文

| # | 问题 | 一句话答案 |
|---|------|-----------|
| 14 | ContextPruning | 大 Tool 输出 → State → JIT 注入 SystemMessage |
| 15 | State 字段 | `retrieved_evidence` / `loaded_skills` |
| 16 | JIT 上限 | 默认 2 块 / 8000 字符 |

---

## 流式

| # | 问题 | 一句话答案 |
|---|------|-----------|
| 17 | SSE 事件 | chunk / process / tool / artifact / done |
| 18 | StreamHandler | LangGraph events → 前端 JSON |
| 19 | think 剥离 | 状态机 + carry buffer |

---

## 数据库

| # | 问题 | 一句话答案 |
|---|------|-----------|
| 20 | async NullPool | 防子 Agent 跨 event loop 连接污染 |
| 21 | sync QueuePool | 同步 CRUD 连接复用 |
| 22 | checkpoint | SQLite app_checkpoints.db |
| 23 | Token 快路径 | Redis auth:token:{token} |

---

## 工具 / Skill

| # | 问题 | 一句话答案 |
|---|------|-----------|
| 24 | Skill 形态 | Markdown SKILL.md 渐进加载 |
| 25 | Tool 形态 | Python @tool 可执行 |
| 26 | 会话文件检索 | chat_file BM25，无 Milvus |
| 27 | ToolGuardrail | AllowlistProvider 执行前拦截 |

---

## Worker

| # | 问题 | 一句话答案 |
|---|------|-----------|
| 28 | Task Worker | Kafka 文档 OCR/抽取 |
| 29 | Ingest Worker | pending_files.status=1 入库 |
| 30 | 深度研究 | API 后台 runner，非 Kafka |

---

## 扩展服务

| # | 问题 | 一句话答案 |
|---|------|-----------|
| 31 | Explore API | /api/v2/explore 导图/习题/详解 |
| 32 | 追问建议 | follow_up_suggestions 读 tool 事件生成 |
| 33 | Explore grounding | 先 retrieve_context 再 LLM |

---

## 关键环境变量

| 变量 | 作用 |
|------|------|
| `MODEL_CONFIG_SOURCE` | db / yaml 模型配置源 |
| `RAG_RETRIEVAL_PROFILE` | 全局强制 Profile |
| `MULTI_KB_PATH_A_QUERY_ONLY` | Path-A 单向量优化（默认开） |
| `JIT_MAX_EVIDENCE_CHARS` | JIT 注入字符上限 |
| `MULTI_KB_SEARCH_MAX_TOP_K` | 多库检索 top_k 上限 |

---

## 关键日志

| 文件 | 查什么 |
|------|--------|
| `logs/api.log` | SSE / Agent 路由 |
| `logs/app.error.log` | 未捕获异常 |
| `logs/worker.log` | Kafka 任务 |
| `logs/ingest_worker.log` | KB 入库 |
| `logs/ingest.log` | RAG 管道细节 |

---

## 技术债 Top 5（2026-06）

1. 主站 think 标签过滤未统一
2. Skill → Workflow 未落地
3. ne_full_feature_agent 仍偏大
4. SQLite checkpoint 多 worker
5. ToolAssembly if-else 硬编码

---

## 源码入口 mnemonic

**「双服三库四路五层」**

- **双服** — 8082 + 8083
- **三库** — MySQL + SQLite checkpoint + Milvus
- **四路** — RAG Path A/B/C/D
- **五层** — API → Router → Agent → Tools/RAG → Storage
