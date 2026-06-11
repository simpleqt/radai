# 07 · Worker 与 Kafka

> 📊 完整 ASCII 图见 [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 15、16、**24–25、34**；正式文档 → [infrastructure_module.md](../../docs/docs_backend/infrastructure_module.md) · [task_worker_module.md](../../docs/docs_backend/task_worker_module.md)

> 本模块答案采用**分层讲解**：先给结论，再解释「为什么」，最后用项目里的真实代码和场景串起来。

---

## L1 初级 🟢

### Q1：项目有哪些后台 Worker？

**一句话结论：** 文档与知识库链路有 **Task Worker**（Kafka 消费文档处理）和 **Ingest Worker**（DB 轮询 KB 入库）两个独立进程；**深度研究**走 API 触发的 asyncio 后台任务，不走 Kafka 主链路。

#### 背景

AI 后端不能把 OCR、向量化、图谱写入等重活放在 HTTP 请求里同步做——用户上传 100 页 PDF 可能要跑几分钟。Worker 把**耗时任务异步化**，API 快速返回，后台慢慢处理。

#### 详细解释：三类后台执行体

**Worker 架构（ASCII 架构图）：**

```text
┌───────────┐  上传/解析任务   ┌─────────────┐  status=1   ┌──────────────┐
│ API 8082  │ ───────────────► │ Task Worker │ ──────────► │ Ingest Worker│
└───────────┘   Kafka 生产     │ (Kafka 消费) │  DB 轮询    │ (DB 轮询)    │
                               └──────┬──────┘             └──────┬───────┘
                                      │                           │
                                      ▼                           ▼
                                MySQL tasks              Milvus + NebulaGraph
                                                         RAG 向量入库

                    ┌─────────────────────┐
                    │ 深度研究 (API 进程内) │
                    │ asyncio Background  │
                    │ Task + SSE process  │
                    └─────────────────────┘
                              (不走 Kafka 主链路)
```



| 执行体 | 启动方式 | 代码入口 | 职责 |
|--------|----------|----------|------|
| **Task Worker** | `doc-agent run-worker` | `worker/task_worker.py` | 消费 Kafka TaskMessage：OCR、解析、抽取、增强 |
| **Ingest Worker** | `doc-agent run-ingest-worker` | `worker/ingest_worker.py` | 轮询 `kb_pending_files` status=1，RAG 向量化入库 |
| **深度研究 Runner** | API 确认大纲后触发 | `custom/tasks/runner.py` | 长流程 Agent 编排，SSE 实时进度 |
| **StartupTaskProcessor**（辅助） | API lifespan 内 | `services/startup_task_processor.py` | API 进程内也会扫 status=1 的 pending（与 Ingest Worker 需 Redis 锁协调） |

Task/Ingest Worker **必须与 API 进程分离部署**（独立 systemd/docker 容器），否则 OCR burst 会拖垮 SSE 延迟。

#### 项目实现

```bash
# cli.py
doc-agent run-worker          # → asyncio.run(task_worker.run_worker())
doc-agent run-ingest-worker   # → asyncio.run(ingest_worker.run_worker())
```

Task Worker 组件链：

```
TaskConsumer
  → DocumentProcessingUseCase   # 业务用例
  → ResultPersister             # 写 MySQL documents 等
  → ResultPublisher             # 发 Kafka result/status topic
```

```text
┌──────────────┐  TaskMessage   ┌─────────────┐  消费   ┌──────────────────────┐
│ 上传 API /   │ ─────────────► │ task topic  │ ──────► │ TaskConsumer         │
│ 内部服务     │  (Kafka 生产)  └─────────────┘         │ DocumentProcessing   │
└──────────────┘                                        └──────────┬───────────┘
                                                                   │
                              ┌────────────────────────────────────┼────────────┐
                              ▼                                    ▼            ▼
                       ResultPersister                    result topic    retry topic
                       写 MySQL documents                 status topic    → dead-letter
```



```
生产者侧：
  文档上传 API / 内部服务
    → 构造 TaskMessage（kafka/schemas.py）
    → AIOKafkaProducer 发到 task topic

消费者侧：
  TaskConsumer（worker/components/task_consumer.py）
    → 消费 task topic
    → DocumentProcessingUseCase 执行
    → ResultPersister 写 DB
    → ResultPublisher 发 result / status / event

失败处理：
  重试 → retry topic
  超限 → dead-letter topic（ poison message 隔离）

可观测：
  log topic / log_error topic（可选消费到 ELK）
```

```text
┌─────────────────────────────────────────┐
                    │              文档生命周期                 │
                    └─────────────────────────────────────────┘

  Kafka 路径                          DB 轮询路径
  ─────────                          ───────────
  TaskMessage                        kb_pending_files status=1
       │                                    │
       ▼                                    ▼
┌──────────────┐                    ┌──────────────┐
│ Task Worker  │                    │Ingest Worker │
│ OCR/解析/抽取 │ ───可选串联──────► │ RAG 向量化    │
└──────┬───────┘                    └──────┬───────┘
       │                                    │
       ▼                                    ▼
  MySQL documents                    Milvus + Nebula + keyword
  retry → dead-letter                Redis lock 防重复
```



| 维度 | Task Worker | Ingest Worker |
|------|-------------|---------------|
| **触发** | Kafka 消息（事件驱动） | MySQL `status=1` 轮询（~2s 间隔） |
| **输入** | TaskMessage（document_id、任务类型） | PendingFile 行（file_id、kb_id、minio_path） |
| **输出** | 解析后 Document、抽取结果 | Milvus 向量、Nebula 图谱、keyword 索引 |
| **扩展** | 增加 consumer group 实例（≤ 分区） | 增加 ingest 进程 + Redis 分布式锁 |
| **失败** | retry topic → dead-letter | status=3 failed，可人工改 status 重跑 |
| **耦合** | 与 API 上传流程 via Kafka | 与 KB 业务状态强绑定 |

**PendingFile 状态机（实际代码语义）：**

| status | 枚举名 | 含义 |
|--------|--------|------|
| 0 | PENDING | 已上传 MinIO，待用户确认入库 |
| 1 | PROCESSING | 用户触发 ingest，**待 Worker 处理/处理中** |
| 2 | COMPLETED | 入库成功 |
| 3 | FAILED | 入库失败 |

```text
深度研究（与会话强绑定）              Kafka Worker（文档批处理）
─────────────────────────            ─────────────────────────
用户 API 确认大纲                       上传/OCR 触发 TaskMessage
        │                                      │
        ▼                                      ▼
asyncio Task / TaskManager              TaskConsumer 独立进程
custom/tasks/runner.py                  DocumentProcessingUseCase
        │                                      │
        ▼                                      ▼
SSE process 实时进度                      MySQL documents + retry topic
MySQL tasks + checkpointer              ✗ 无 SSE · ✗ 无用户 cancel 细粒度

为何深研不用 Kafka：需实时 SSE · 用户 cancel · thread/checkpointer 同进程
```



**深度研究触发链：**

```
用户 API → 意图 deep_research → 大纲确认
  → asyncio.create_task / TaskManager 注册
  → custom/tasks/runner.py
  → CSHDeepResearchOrchestratorAgent / NE 等价物
  → 事件广播到 TaskManager → SSE process 事件
  → 状态写 MySQL tasks + LangGraph checkpointer
```

```text
┌─────────────┐
│ TaskMessage │
└─────────────┘

  TaskMessage ──────────────► K2

  K2 ──────────────► OCR/解析

  OCR/解析 ──────────────► API 确认大纲

  API 确认大纲 ──────────────► D2

  D2 ──────────────► D3

  D3 ──────────────► D4

  D4 ──────────────► SSE process 事件

```

**Kafka Worker 特征：**
- 与 HTTP 会话无强绑定
- 消费即处理，进度写 topic/DB
- 适合「上传解析」类批处理

#### 项目实现

`runner.py` 关键依赖：

- `BackgroundTask`、`TaskEvent`（`custom/tasks/manager.py`）
- `check_cancel_flag_from_db()` — **协作式取消**
- `ResearchWorkflowState` — 状态机
- `create_deep_research_orchestrator()` — 域感知工厂

取消语义：API 设 cancelled flag → runner 循环检查 → **不能强杀**正在进行的 LLM HTTP 请求（600s 超时内仍可能跑完一段）。

#### 具体例子

| 任务类型 | 传输 | 进度 | 取消 |
|----------|------|------|------|
| PDF OCR | Kafka task topic | status topic / DB | 较难即时 |
| KB ingest | DB polling | pending status / ingest log | 等当前 file 完成 |
| 深度研究 | asyncio | SSE process | DB cancel flag |

#### 常见误区

- ❌ 「深度研究也应该 Kafka 化」→ 实时 SSE + 会话状态使 in-process 更合适。
- ❌ 「cancel 会立刻停 LLM」→ 协作式，单次 LLM 调用可能仍跑完。
- ❌ 「runner 与 Agent query 同线程」→ asyncio Task 独立调度。

#### 延伸追问（L2/L3）

- L3：Q13 深度研究如何 cancel？与 LangGraph interrupt 关系？

---

## L2 中级 🟡

### Q5：TaskConsumer 的处理流程是什么？

**一句话结论：** **Kafka 消费 TaskMessage → DocumentProcessingUseCase 执行业务 → ResultPersister 持久化 → ResultPublisher 发布结果**；失败进 retry，超限进 dead-letter。

#### 背景

Consumer 应「薄」：只负责消息 ack、重试、metrics；业务逻辑在 UseCase，便于 API 同步路径（若有）复测同一 UseCase。

#### 详细解释：逐步链路

```
① TaskConsumer.start()
   subscribe task topic（settings.kafka_task_topic）
   consumer group = settings 配置

② 收到单条 TaskMessage
   解析 kafka/schemas.py 定义的结构
   校验 document_id / task_type

③ DocumentProcessingUseCase.execute()
   按任务类型：OCR（MinerU/Docling）、文本抽取、增强
   读写 MinIO、MySQL Document、FileInfo

④ ResultPersister.persist()
   更新 documents 表状态、parse_status、storage_status 等
   幂等 key：document_id / file_id

⑤ ResultPublisher.publish()
   result topic → 下游通知
   status topic → 进度百分比
   event topic → 业务事件（可选）

⑥ 异常路径
   可重试 → retry topic（延迟再消费）
   Poison / 超限 → dead-letter topic
   日志 → worker.log + 可选 log_kafka_topic
```

#### 项目实现

`worker/task_worker.py` 组装：

```python
doc_processor = DocumentProcessingUseCase()
persister = ResultPersister()
publisher = ResultPublisher()
consumer = TaskConsumer(doc_processor, persister, publisher)
```

具体 Consumer 实现在 `worker/components/task_consumer.py`（含 ack 策略、并发度）。

#### 具体例子

TaskMessage `{document_id: "abc", task_type: "ocr"}`：

```
Consumer 收到 → UseCase 从 MinIO 拉 PDF → MinerU 解析 → markdown 写回
→ Persister 更新 Document.parse_status=1
→ Publisher 发 result {status: "success", document_id: "abc"}
→ 若后续要进 KB，业务层创建/更新 PendingFile status=1
```

#### 常见误区

- ❌ 「Consumer 直接调 RAG ingest」→ ingest 在 **Ingest Worker**，不在 TaskConsumer。
- ❌ 「失败消息会无限 retry」→ 应有上限进 **dead-letter**。
- ❌ 「ack 前不做 persist」→ 可能导致消息丢失但 DB 未更新（取决于 ack 策略，实现需确认 exactly-once 语义）。

#### 延伸追问（L3）

- L3：Q12 水平扩展时 Consumer group 与幂等？

---

### Q6：Kafka topic 有哪些？各做什么？

**一句话结论：** `kafka/admin.py` 初始化 **task、status、result、event、retry、dead-letter、log、log_error** 等 topic；具体名字在 `settings.py` 的 `kafka_*_topic` 配置项。

#### 背景

Topic 拆分是为了**消费方解耦**：上传 API 只关心 task 投递；进度 UI 只订阅 status；监控系统订 log_error；失败运维订 dead-letter。

#### 详细解释

| Topic 类型 | settings 典型键 | 用途 | 分区策略（admin.py） |
|------------|-----------------|------|----------------------|
| **task** | `kafka_task_topic` | 待处理文档任务队列 | `kafka_num_partitions` |
| **status** | `kafka_status_topic` | 处理进度更新 | 同 task |
| **result** | `kafka_result_topic` | 最终结果通知 | 同 task |
| **event** | `kafka_event_topic` | 业务事件广播 | 3 分区 |
| **retry** | `kafka_retry_topic` | 失败重试队列 | 3 分区 |
| **dead-letter** | `kafka_dead_letter_topic` | 永久失败 poison message | 1 分区 |
| **log** | `log_kafka_topic` | 处理日志（可选） | 3 分区 |
| **log_error** | `log_kafka_error_topic` | 错误日志 | 1 分区 |

创建命令通常：`python -m backend.kafka.admin` 或部署脚本调用 `create_topics()`。

#### 项目实现

```python
# kafka/admin.py  excerpt
NewTopic(name=settings.kafka_task_topic, num_partitions=settings.kafka_num_partitions, ...)
NewTopic(name=settings.kafka_dead_letter_topic, num_partitions=1, ...)
```

Producer/Consumer 客户端在 `kafka/client.py`、`kafka/consumer.py` 封装 aiokafka。

#### 具体例子

运维排查卡单：

```
1. MySQL documents 表 status 卡住
2. kafka consumer lag on task topic 飙升 → Worker 不够或 hung
3. dead-letter topic 有新消息 → 某 document 永久失败，看 payload
4. worker.log 搜 document_id
```

#### 常见误区

- ❌ 「topic 名硬编码在代码里」→ 应通过 **settings** 环境化，dev/prod 不同前缀。
- ❌ 「dead-letter 会自动重放」→ 需**人工或脚本**重新投递。
- ❌ 「ingest 用 task topic」→ ingest 走 DB，**不是** task topic。

#### 延伸追问（L3）

- L3：Q11 若统一 ingest 到 Kafka，topic 如何设计？

---

### Q7：Ingest Worker 如何避免重复入库？

**一句话结论：** 靠 **PendingFile 状态机 + Redis 分布式锁 `kb_ingest_lock:{file_id}` + 处理前二次校验 status==1**；多 Worker/API 内 StartupTaskProcessor 并发时，锁失败者跳过。

#### 背景

水平扩展 Ingest Worker 时，两个进程可能同时 `SELECT ... WHERE status=1` 拿到同一 file_id。若无锁，会**双重 embedding**、Milvus 重复 upsert、浪费 GPU/API 配额。

#### 详细解释：三层防重

**① DB 状态门槛**
- 只处理 `status==1`
- 处理完成后更新为 2（成功）或 3（失败）
- `process_file` 开头再查一次，非 1 则 skip

**② Redis NX 锁**

```python
# ingest_pending_processor.py
lock_key = f"kb_ingest_lock:{file_id}"
acquired = await redis_client.set(lock_key, "1", nx=True, ex=7200)
if not acquired:
    return False  # 其他 worker 正在处理
```

```text
┌──────────────────────────────────────┐
│ Ingest Worker 轮询 status=1            │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ Lock                                 │
└──────────────────────────────────────┘
         (失败)
           │
           ▼
         (成功)
           │
           ▼
┌────────────────┐  ┌────────────────┐
│ ReCheck        │  │ 跳过 file_id     │
└────────────────┘  └────────────────┘
         (否)
           │
           ▼
         (是)
           │
           ▼
┌──────────────────────────────────────┐
│ Ingest                               │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ OK                                   │
└──────────────────────────────────────┘
         (是)
           │
           ▼
         (否)
           │
           ▼
┌────────────────┐  ┌────────────────┐
│ status=2       │  │ status=3       │
└────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
┌──────────────────────────────────────┐
│ 释放锁                                  │
└──────────────────────────────────────┘

```

TTL 7200s 防死锁（进程 crash 后锁自动释放）。

**③ StartupTaskProcessor 同锁**

API 进程内的 `StartupTaskProcessor` 也使用相同 lock key，避免与独立 Ingest Worker **重复 ingest**。

**未来可增强：** `UPDATE pending SET status=processing WHERE file_id=? AND status=1` 原子抢占（当前主要依赖 Redis）。

#### 项目实现

Ingest Worker 批量拉 candidate_limit 条 file_id，逐个 `process_file`；若整批 **processed_count==0**（全被锁跳过），强制 sleep 防刷日志死循环。

#### 具体例子

2 个 ingest-worker 实例 + 1 个 API StartupTaskProcessor：

```
三者同时看到 file_id=F1 status=1
Redis：仅一个 set nx 成功 → 执行 ingest
其余 debug log「文件正在由其他 worker 处理，跳过」
成功 → status=2，锁 delete
```

#### 常见误区

- ❌ 「MySQL 行锁足够」→ 当前实现**主要靠 Redis**，不是 `FOR UPDATE SKIP LOCKED`。
- ❌ 「status=1 会永远卡住」→ 失败应改 3；锁 TTL 过期后可重试。
- ❌ 「重复 ingest 只浪费钱没副作用」→ 可能导致 Milvus 重复 chunk、检索质量下降。

#### 延伸追问（L3）

- L3：Q12 高并发 ingest 对 Milvus 写压力？

---

### Q8：文档处理用例（UseCase）层在哪里？

**一句话结论：** 业务逻辑封装在 **`worker/components/doc_processing.py`** 的 `DocumentProcessingUseCase`，由 TaskConsumer 调用，把 Kafka 消息与 MinIO/MySQL/OCR 细节解耦。

#### 背景

若 Consumer 内联 500 行 OCR 代码，会导致：
- 无法单测业务（必须 mock Kafka）
- API 同步小文件处理无法复用
- 换 MinerU 版本要改 Consumer

UseCase 层是 **Clean Architecture** 的「应用服务」。

#### 详细解释

**UseCase 职责边界：**

| 属于 UseCase | 不属于 UseCase |
|--------------|----------------|
| 解析 TaskMessage 意图 | Kafka ack/commit |
| 调 MinIO 下载、上传 | Consumer group 再平衡 |
| 调 OCR/Docling/MinerU | retry topic 投递策略 |
| 更新 Document ORM 对象 | HTTP 响应格式化 |

**Ingest 侧类比：** `PendingIngestProcessor`（`ingest_pending_processor.py`）是 ingest 的「用例」，虽不叫 UseCase，角色相同。

#### 项目实现

```
worker/
  task_worker.py              # 入口
  components/
    task_consumer.py          # 消息适配器
    doc_processing.py         # DocumentProcessingUseCase ★
    result_persister.py
    result_publisher.py
    ingest_pending_processor.py  # ingest 用例
```

#### 具体例子

单测策略：

```python
# 伪代码
async def test_ocr_use_case():
    use_case = DocumentProcessingUseCase(minio=FakeMinio, ocr=FakeOCR)
    result = await use_case.execute(task_message)
    assert result.markdown_path is not None
# 无需启动 Kafka
```

#### 常见误区

- ❌ 「UseCase 在 services/ 目录」→ 文档处理 UseCase 在 **worker/components/**。
- ❌ 「API 路由直接调 UseCase」→ 主路径经 Kafka；同步 fallback 若有才复用。
- ❌ 「ingest 也走 DocumentProcessingUseCase」→ ingest 走 **PendingIngestProcessor + rag_service**。

#### 延伸追问（L3）

- L3：Q14 小文件 Kafka fallback 是否应直调 UseCase？

---

### Q9：Worker 日志如何排查任务失败？

**一句话结论：** 分 **文件日志**（worker/ingest/api）+ **Kafka dead-letter** + **MySQL 状态表** 三线排查，用 document_id / file_id 关联。

#### 背景

异步系统故障点分散：消息丢了？OCR 挂了？Milvus 满了？没有统一日志路径会盲查。

#### 详细解释：排查资源表

| 日志/数据源 | 路径/位置 | 看什么 |
|-------------|-----------|--------|
| Task Worker 主日志 | `logs/worker.log` | Consumer 异常、UseCase stack trace |
| Ingest Worker | `logs/ingest_worker.log` | pending 处理、Redis 锁、route=md-direct/mineru |
| RAG ingest 细节 | `logs/ingest.log` | embedding、Milvus upsert、LightRAG |
| API 错误 | `logs/app.error.log` | 上传投递 Kafka 失败 |
| Kafka dead-letter | `kafka_dead_letter_topic` | poison message 原文 |
| MySQL Document | `documents` 表 | parse_status、error 字段 |
| MySQL PendingFile | `kb_pending_files` | status=3 + error_message |
| MySQL Task | `tasks` 表 | 深度研究状态（非 Kafka） |

**标准路径：**

```
SSE/前端显示失败
  → 拿 document_id 或 file_id
  → grep worker.log + ingest_worker.log
  → 查 DB 状态
  → 若 Kafka 链路：看 consumer lag + dead-letter
```

#### 项目实现

日志由 `config/logging.py` + `config/filters.py` 配置，ContextVar 注入 request_id（Worker 进程可能无 user_id）。

ingest 日志前缀：`[ingest-worker]`、`[KB xxx]`（Manager 内）。

#### 具体例子

**现象：** pending 永远 status=1

```
1. ingest_worker.log 有无「ingest worker started」→ 进程是否启动
2. Redis 是否可达 → 锁是否假失败
3. grep file_id → 是否「跳过非入库中状态」
4. StartupTaskProcessor 是否与独立 Worker 抢锁导致饥饿（罕见）
```

#### 常见误区

- ❌ 「只看 api.log」→ ingest 在 **ingest_worker.log**。
- ❌ 「dead-letter 会自动告警」→ 需自建监控消费 dead-letter。
- ❌ 「status=1 就是失败」→ 1 是**处理中/待处理**，3 才是失败。

#### 延伸追问（L3）

- L3：Q13 ingest 失败 Milvus schema 不匹配如何修？

---

### Q10：`cli.py` 中 run-worker 启动参数？

**一句话结论：** `doc-agent run-worker --workers 1` 与 `doc-agent run-ingest-worker --workers 1`；**当前实现中 `--workers` 对 worker 命令尚未真正起多进程**，生产应用 systemd/docker **多副本** 水平扩展。

#### 背景

CLI 与 `run-api --workers 4` 对称设计，但 task/ingest worker 入口是 `asyncio.run(run_worker())` 单 loop，不同于 uvicorn 多 worker。

#### 详细解释

```bash
# 文档处理 Kafka Consumer
doc-agent run-worker [--workers 1]

# 知识库入库 DB 轮询
doc-agent run-ingest-worker [--workers 1]
```

**cli.py 实际行为：**

- `run-worker` → `backend.worker.task_worker.run_worker()`（单 asyncio 循环）
- `run-ingest-worker` → `backend.worker.ingest_worker.run_worker()`
- `--workers` 参数**已定义但未传给多进程 fork**（与 run-api 不同）

**生产推荐：**

```
# systemd 示例：3 个 ingest worker 实例
ingest-worker@1.service
ingest-worker@2.service
ingest-worker@3.service
# 依赖 Redis 锁防重复，非 CLI --workers
```

Task Worker 扩展靠 **Kafka consumer group** 实例数 ≤ task topic 分区数。

#### 项目实现

ingest 环境变量（`ingest_worker.py`）：

| 变量 | 默认 | 含义 |
|------|------|------|
| `INGEST_WORKER_POLL_INTERVAL` | 2 | 空轮询 sleep 秒 |
| `INGEST_WORKER_BATCH_SIZE` | 5 | 批次处理目标 |
| `INGEST_WORKER_CANDIDATE_MULTIPLIER` | 10 | candidate_limit = batch × multiplier |

#### 具体例子

docker-compose：

```yaml
ingest-worker:
  command: doc-agent run-ingest-worker
  deploy:
    replicas: 2
task-worker:
  command: doc-agent run-worker
  deploy:
    replicas: 3  # 需 task topic 分区 ≥ 3
```

#### 常见误区

- ❌ 「--workers 3 会起 3 个 consumer」→ 当前代码**未实现**，需多容器。
- ❌ 「ingest 靠 Kafka 分区扩展」→ ingest 靠 **多进程 + Redis 锁**。
- ❌ 「run-api --workers 与 run-worker 语义相同」→ api 用 uvicorn workers，worker 是 asyncio 单 loop。

#### 延伸追问（L3）

- L3：Q12 Task Worker 水平扩展 checklist？

---

## L3 高级 🔴

### Q11：Kafka 与 DB 轮询双轨并存的原因？能否统一？

**一句话结论：** **历史演进**：Kafka 适合 burst 文档解析解耦 API；DB 轮询 ingest 与 KB 状态强绑定、运维可手工改 status 重跑。**能统一**，但需引入 ingest-consumer 或 Temporal/Celery，现状代价是**双套监控 + 状态对账**。

#### 背景

理想架构「一个队列统治一切」。现实中 ingest 与 KB 产品逻辑（pending 列表、用户取消、按 kb_id 隔离）耦合深，早期用 MySQL status 列最快落地；文档 OCR 则已有 Kafka 基础设施。

#### 详细解释

**为何 Kafka 做 Task：**
- 上传 burst、需 backpressure
- OCR 失败 retry/dead-letter 成熟
- API 与计算完全解耦

**为何 DB 做 Ingest：**
- `kb_pending_files` 即产品「待入库列表」UI
- 运维：`UPDATE status=1` 手动重跑
- 无需维护 ingest topic 消费 lag 另一套告警

**统一方案（设计题）：**

| 方案 | 描述 | 优点 | 代价 |
|------|------|------|------|
| **全 Kafka** | 触发 ingest → ingest topic → ingest consumer group | 单一监控栈 | pending UI 需同步消费 offset |
| **Temporal/Celery** | 工作流编排 OCR→ingest | 可见性、重试、DAG | 新依赖 |
| **Outbox 模式** | DB 事务写 pending + outbox 表 → relay 发 Kafka | 一致性 | 实现复杂 |

**现状对账问题：**
- Kafka OCR 成功但 pending 未设 status=1 → 文档解析了但未入库
- ingest 成功但 keyword 索引未建 → 需 `rag/sync/` 调度器补（见 Q15）

#### 项目实现

- Task：`worker/task_worker.py` + Kafka
- Ingest：`worker/ingest_worker.py` + `PendingFile.status`
- API 内：`StartupTaskProcessor` 第三条路径（需 Redis 锁）

#### 具体例子

**不一致场景：**

```
Task Worker OCR 成功 → Document.parse_status=1
但上传流程 bug 未创建 PendingFile status=1
→ 用户看「已解析」但 KB 检索不到
排查：对账脚本比 Document 与 PendingFile
修复：补 PendingFile 或 rag/sync 调度器
```

#### 常见误区

- ❌ 「双轨是设计失误」→ 是 **trade-off**，面试应承认 + 提演进。
- ❌ 「合并很简单」→ 涉及 UI、运维、幂等、监控全链路。
- ❌ 「StartupTaskProcessor 可去掉」→ 去掉则仅依赖独立 ingest worker，需保证必部署。

#### 延伸追问

- Outbox + ingest topic 的 exactly-once 如何保证？

---

### Q12：Task Worker 水平扩展时要注意什么？

**一句话结论：** 关注 **Consumer Group 分区数、处理幂等、Milvus/Nebula 写 QPS、MinIO 带宽、与 Ingest 的 pipeline 顺序**——扩 Worker 不是无脑加实例。

#### 背景

Kafka 扩展定律：同一 group 内**一个分区同一时刻只给一个 consumer**。分区不够，加实例也 idle。

#### 详细解释：Checklist

**1. Consumer Group 与分区**

```
consumer 实例数 ≤ task topic 分区数（settings.kafka_num_partitions）
不同 group 会重复消费 — 勿误配
```

**2. 幂等处理**

OCR/解析可能 **at-least-once** 投递：

```
ResultPersister 用 document_id 幂等 upsert
MinIO 对象用 content hash 或 version 避免覆盖乱序
```

**3. 下游 Milvus/Nebula 压力**

Ingest Worker 若同步扩展，embedding QPS × 实例数可能打满向量库 → P95 检索变慢。

**4. MinIO 带宽**

并发大文件 download/upload  saturate 网络 → 调低 `max_concurrent_files` 或错峰。

**5. 与 Ingest 协调**

```
Task OCR 未完成 → 不应 status=1 ingest（PDF 路由依赖解析结果）
Pipeline 顺序：Task 成功 → 业务设 pending → Ingest
```

**6. 可观测**

- consumer lag 告警
- dead-letter 速率
- worker CPU/OCR GPU 利用率

#### 项目实现

`StartupTaskProcessor._concurrent_limit = settings.max_concurrent_files`（默认 5）限制 API 进程内并发 ingest；独立 Worker 靠 Redis 锁自然限并发。

#### 具体例子

task topic 3 分区、部署 5 个 task-worker → **2 个永远 idle**。应 `kafka_num_partitions=8` 再扩到 8 实例。

#### 常见误区

- ❌ 「lag 高就无限加 Worker」→ 先加分区，再看 OCR GPU 是否瓶颈。
- ❌ 「Task 和 Ingest 扩同一倍率」→ 瓶颈资源不同（CPU vs embedding API）。
- ❌ 「幂等不重要」→ 重复 OCR 浪费 GPU，重复 ingest 污染索引。

#### 延伸追问

- 如何自动 scale consumer 基于 lag（KEDA）？

---

### Q13：深度研究后台任务如何取消？

**一句话结论：** 用户 cancel → API 设 **DB/ResearchWorkflowState cancelled** → `runner.py` **协作式**轮询 `check_cancel_flag_from_db()`；**无法 instant 中断**进行中的 LLM HTTP 调用。

#### 背景

asyncio `Task.cancel()` 对正在 `await llm.ainvoke()` 的协程无效（需等 HTTP 返回）。线程池子 Agent 更难强杀。必须 **协作式取消** + 统一状态机。

#### 详细解释

**取消链路：**

```
前端点取消
  → API 更新 tasks 表 / ResearchWorkflowState.cancelled=True
  → runner 主循环每步 check_cancel_flag_from_db()
  → 若 cancelled：停止派发新子 Agent、不再写 SSE progress、清理 TaskManager
  → 已在飞的 LLM 请求：等 timeout 或自然结束
```

**与子 Agent 差异：**

- 主 runner：asyncio 协程，查 DB flag
- 子 Agent 若跑独立线程 + event loop：需同样 flag 注入或共享 TaskManager 事件

#### 项目实现

`custom/tasks/runner.py` import：

```python
from backend.custom.tasks.manager import check_cancel_flag_from_db
from backend.custom.workflow.research_state import ResearchWorkflowState
```

LangGraph 原生 `interrupt` 在本项目深度研究路径的使用程度需看 Orchestrator 实现——面试可提 **双机制**：flag + graph interrupt。

#### 具体例子

用户在大纲确认后 5 分钟点取消：

```
runner 正在 await 子 Agent LLM（已跑 200s）
→ cancel flag 已设，但要等本次 HTTP 返回（最多 ~600s config timeout）
→ 返回后 runner 检测到 cancelled，不再进入下一阶段
→ SSE 发 cancelled 事件
前端应提示「已请求取消，当前步骤完成后停止」
```

#### 常见误区

- ❌ 「cancel = 立刻停」→ **协作式**，LLM 单次调用可能跑完。
- ❌ 「kill -9 worker 就行」→ 会 corrupt checkpointer 状态。
- ❌ 「Kafka Worker 取消语义相同」→ 文档 Task 通常无用户级 cancel。

#### 延伸追问

- 如何实现 LLM 请求的 cancel token 传递到 httpx client？

---

### Q14：Kafka 不可用时的降级策略？

**一句话结论：** **现状 Kafka 对文档 Task 是硬依赖**——broker 挂掉则投递失败或 API 500；可答改进：**同步小文件 fallback、Redis 缓冲队列、503 + 重试指引**。

#### 背景

高可用 Kafka 集群并非所有部署都有。单 broker 开发环境、网络分区生产事故都真实发生。

#### 详细解释

**现状行为（需诚实）：**

```
上传 API 构造 TaskMessage → produce 失败
  → 可能 HTTP 500 / 任务丢失 / 仅写 DB 未触发处理
具体见 api/routers/document_* 与 upload 路由实现
```

**可答改进方案：**

| 策略 | 做法 | 适用 |
|------|------|------|
| **同步 fallback** | 小文件（<5MB MD）直接 `DocumentProcessingUseCase` inline | 降低依赖 |
| **Redis List 缓冲** | produce 失败 push redis queue，后台 relay | 轻量降级 |
| **503 + Retry-After** | 明确告知前端稍后重试 | 用户体验 |
| **本地磁盘 spool** | 极端降级，daemon 重投 | 边缘部署 |

**ingest 路径：** 不依赖 Kafka，**仅 DB + Redis 锁**——Kafka 挂掉不影响已 status=1 的 ingest（若 MinIO/RAG 仍可用）。

#### 项目实现

排查：`grep kafka` in upload routes；`kafka/client.py` producer 错误处理；monitoring on producer error rate。

#### 具体例子

**事故：** Kafka 集群 rolling restart 30s

```
新上传 PDF → produce timeout → 用户看到上传失败
已 status=1 的 pending → ingest worker 正常继续
恢复后 → 失败上传需用户重试或运维从 DB 补投
```

#### 常见误区

- ❌ 「Kafka 挂了 RAG 也不能用」→ **已入库 KB 检索不受影响**，仅新文档 pipeline 卡住。
- ❌ 「dead-letter 能当缓冲」→ dead-letter 是**失败终点**，不是 buffer。
- ❌ 「面试可说已完美 fallback」→ 除非代码真有，应说明 **现状 + 改进**。

#### 延伸追问

- Outbox 模式如何实现 produce 与 DB 事务一致？

---

### Q15：ingest 与 RAG 索引同步调度器如何配合？

**一句话结论：** **Ingest Worker 负责实时入库**；**`rag/sync/` 调度器负责周期性对账/补建**（keyword 索引 drift、MySQL 与 Milvus 不一致）——不能把调度器当实时 ingest 用。

#### 背景

LightRAG + Milvus + Nebula + keyword index 多存储，任一环节 partial failure 会导致「DB 说成功但检索空」。纯实时链路难以 100% 一致，需要 **reconciliation job**。

#### 详细解释

**组件：**

- `rag/sync/rag_index_sync_scheduler.py` — 定时触发
- `rag/sync/rag_index_sync_manager.py` — 对账逻辑：比 MySQL 文档状态 vs 向量库 vs keyword 图

**与 Ingest Worker 分工：**

| 角色 | 时效 | 场景 |
|------|------|------|
| Ingest Worker | 秒~分钟级 | 用户触发 ingest 的主路径 |
| Sync 调度器 | 分钟~小时级 | ingest 成功但 keyword 未建、orphan 向量、索引版本升级 |

**避免：** 仅依赖调度器做 ingest → 用户上传后 **分钟级延迟** 才能检索，产品不可接受。

#### 项目实现

API lifespan 可能启动 scheduler（见 `http_server.py` lifespan 与 rag service 预热）。与 `StartupTaskProcessor` 不同：scheduler 是 **一致性修复**，不是主 ingest。

#### 具体例子

**现象：** multi_kb_search 无结果，但 pending status=2

```
1. ingest.log 显示 Milvus upsert 成功
2. keyword_index 构建失败（日志 warning）
3. 等待 sync scheduler 下一周期补建 keyword
4. 或运维手动触发 sync API / 重启 API 预热
```

#### 常见误区

- ❌ 「ingest 成功 = 立即可检索」→ keyword Path-A 可能滞后。
- ❌ 「scheduler 取代 ingest worker」→ 延迟太大。
- ❌ 「对账会自动删 orphan」→ 取决于 manager 实现，可能有 dry-run。

#### 延伸追问

- 如何设计 ingest 与 sync 的 metrics（drift 数量、补建耗时）？
