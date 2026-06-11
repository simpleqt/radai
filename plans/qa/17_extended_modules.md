# 17 · 扩展模块（ASR / 数据集 / 抽取 / 运维）

> 📊 **图表：** [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 16、19

---

## L1 初级 🟢

### Q1：ASR 模块做什么？

**一句话结论：** 前端录音 → **`api/routers/asr.py`** 代理到 OpenAI 兼容 ASR 网关，返回文本供输入框使用；**不参与 Agent 核心链路**。

#### 配置

- `backend/core/models/asr_config.py`
- 文档：`docs/docs_backend/asr_module.md`

---

### Q2：实验数据集（ExperimentDataset）模块用途？

**一句话结论：** 注册 Excel/CSV **实验数据**，供 Agent/Excel 工具 **DuckDB 查询**与图表生成；与 KB 文档库分离。

#### 在本项目中的实现

- `db/models/dataset.py`
- `api/routers/datasets.py`
- `db/init_datasets.py`

---

### Q3：内容抽取（Extraction）API 与 Worker 关系？

**一句话结论：** v1 抽取 API 创建 **MySQL Task**；**Kafka Task Worker** 消费后 OCR/结构化抽取，结果写 `ExtractionResult`；与 RAG ingest **可串联但不等同**。

```text
┌────────────────┐
│ extraction API │
└────────────────┘

  extraction API ──────────────► KF

  KF ──────────────► TW

  TW ──────────────► MYSQL

```

---

### Q4：`Index Sync Scheduler` 做什么？

**一句话结论：** 定时（默认凌晨）对齐 **Milvus + Nebula + MySQL** 索引状态，修复 drift；**不替代**实时 ingest。

#### 在本项目中的实现

- `rag/sync/`
- `http_server.py` lifespan 启动调度器

---

## L2 中级 🟡

### Q5：Excel 分析工具为什么用 DuckDB？

**一句话结论：** 对上传 Excel **内存/本地 DuckDB** 跑 SQL 聚合，比逐行 Python 快；`excel_engine.py` 封装；图表由 `excel_chart_generator.py` 输出。

| 组件 | 职责 |
|------|------|
| excel_data_analysis_tool | Agent 工具入口 |
| excel_engine | DuckDB + pandas 桥 |
| excel_chart_generator | matplotlib/seaborn |

---

### Q6：模型配置 Redis Pub/Sub 热更新流程？

**一句话结论：** 管理面改 `llm_models` → 发布 Redis 消息 → API worker 订阅 → 刷新内存模型列表，**无需重启**（见 model_management 文档）。

```text
┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
│   管理 API   │ │ llm_models │ │  Pub/Sub   │ │ API Worker │
└────────────┘ └────────────┘ └────────────┘ └────────────┘


```

---

### Q7：Startup Task Processor 与 Ingest Worker 区别？

**一句话结论：** **Startup Task Processor** 在主 API 进程 lifespan 内扫描 `PendingFile status=0/1`（轻量并发）；**Ingest Worker** 是独立进程专做入库，适合生产重负载。

| | Startup Processor | Ingest Worker |
|--|-------------------|---------------|
| 进程 | API 内 | 独立 CLI |
| 扩展 | 受 API 资源限制 | 可水平扩展 |

---

### Q8：Makefile / Docker 部署入口有哪些？

**一句话结论：** 见 `docs/docs_backend/deployment_runbook.md`：镜像构建、`run_services.sh`、环境变量清单、日志路径、健康检查。

#### 常见命令

```bash
doc-agent init-db
doc-agent run-api --workers 4
doc-agent run-worker
doc-agent run-ingest-worker
```

---

## L3 高级 🔴

### Q9：Kafka Task 与 Ingest 双轨如何对账？

**一句话结论：** 文档可能 **Kafka 处理成功但 pending 未标记 ingest** → 需 MySQL 状态字段 + 运维 SQL + sync 调度器；面试应提出 **对账脚本** 而非假设自动一致。

---

### Q10：数据集 Agent 绑定与 KB 绑定冲突吗？

**一句话结论：** 不冲突——**dataset** 供 DuckDB 实验分析；**KB** 供 RAG 检索；同一 Agent 可同时绑 dataset_id 与 kb_ids，ToolAssembly 分别注入工具。

---

### Q11：ASR 音频流的安全与大小限制？

**一句话结论：** 需限制上传 **duration/size**、鉴权、防滥用打 ASR 网关；错误不应泄露网关 key。

---

### Q12：生产发布 checklist（扩展模块视角）

**一句话结论：**

- [ ] Kafka topic 已创建（`kafka/admin.py`）
- [ ] ingest + task worker 进程存活
- [ ] Index sync 时区与维护窗口
- [ ] Redis 模型订阅连通
- [ ] MinIO bucket 与 ScopedStorage 前缀策略
- [ ] ASR/内部 API secret 已配置非空

```text
┌──────────────────────────────────────┐
│ DEP                                  │
└──────────────────────────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
┌────────────────┐  ┌────────────────────────────────┐  ┌────────────────┐  ┌────────────────┐
│ API            │  │ MySQL Redis Milvus MinIO Kafka │  │ Task Worker    │  │ Ingest Worker  │
└────────────────┘  └────────────────────────────────┘  └────────────────┘  └────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ Index Sync Scheduler                 │
└──────────────────────────────────────┘

```

---

### Q13：若只部署「问答不含文档上传」最小集，哪些模块可关？

**一句话结论：** 可关 Kafka/Task Worker/ingest（若无 KB 更新）；**不可关** Milvus（若已有 KB）、Redis、MySQL、LLM；Explore/ASR/数据集均可不部署路由。

---

### Q14：抽取结果如何进入 RAG？

**一句话结论：** 通常 **人工或 pipeline 触发** 将抽取后文件标记 pending → ingest Worker → RAGAnything；非自动「抽取完即向量」 unless 工作流配置。

---

### Q15：扩展模块面试速记：19 个子模块怎么记？

**一句话结论：** 按 **「问、存、算、管」** 四类：

| 类 | 模块 |
|----|------|
| 问 | agent, explore, follow_up, guest, asr |
| 存 | kb, personal_files, artifacts, conversations |
| 算 | rag, excel, datasets, extraction, task_worker |
| 管 | auth, sms, models, deployment, user_isolation |

完整索引：`docs/docs_backend/README.md` 模块导航表。
