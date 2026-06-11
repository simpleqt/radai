# 深度审查：Kafka + Worker 文档处理流水线

> 目录：`backend/kafka/`、`backend/worker/`  
> 入口：`worker/task_worker.py` → `TaskConsumer`  
> 只读审查。

---

## 1. 架构

```text
┌────────────────────────────────┐
│ upload_routes / chunked_upload │
└────────────────────────────────┘

  upload_routes / chunked_upload ──────────────► K1

  K1 ──────────────► TC

  TC ──────────────► DP

  DP ──────────────► RP

  RP ──────────────► (任务/文档状态)

  (任务/文档状态) ──────────────► Pub

  Pub ──────────────► K2

  K2 ──────────────► RT

  upload_routes / chunked_upload ──(produce)──► K1

```

| 组件 | 文件 | 职责 |
|------|------|------|
| 客户端 | `kafka/client.py` | 启停、生产消费封装 |
| 消费者管理 | `kafka/consumer.py` | `KafkaConsumerManager`、`RetryManager`、`TaskStatusUpdater` |
| 消息模型 | `kafka/schemas.py` | `TaskMessage`、`RetryMessage`、状态枚举 |
| Worker 编排 | `worker/components/task_consumer.py` | 并发锁、pipeline |
| 业务 | `doc_processing.py` | 解析/切块/向量化等 |
| 持久化 | `result_persister.py` | DB 写结果 |
| 发布 | `result_publisher.py` | 成功/失败事件 |

---

## 2. `TaskConsumer` 行为

### 2.1 生命周期

- `start()`：`kafka_client.start()` + 订阅 `kafka_task_topic` + `kafka_retry_topic`  
- `stop()`：取消 in-flight asyncio tasks，停 consumer  
- 主循环 `task_worker.run_worker`：`while True: sleep(1)` — 仅保持进程

### 2.2 单任务 pipeline（`_process_pipeline`）

1. `update_task_status` → PROCESSING  
2. `TaskStatusUpdater` 阶段进度（0.05 → 0.95）  
3. 校验 `file_path` 存在  
4. `DocumentProcessingUseCase.execute(task)`  
5. `ResultPersister.save_result`  
6. `ResultPublisher.publish_success`  
7. 异常 → 失败发布 + retry 逻辑（见 consumer 后半）

### 2.3 并发控制

- `_task_lock` + `_processing_tasks`：同 `task_id` 重复消息 **跳过**（warning）  
- 每消息 `asyncio.create_task` 后台执行 — **无** 全局 semaphore 限制并发文档数

---

## 3. 质量与风险

| ID | 严重度 | 问题 |
|----|--------|------|
| KW-1 | medium | **无并发上限** — 大批量上传可打满 CPU/内存/GPU（取决于 doc_processing） |
| KW-2 | medium | `file_path` 为本地路径 — Worker 与 API **须共享存储**；K8s 多副本易 FileNotFound |
| KW-3 | medium | 重复 task_id 静默跳过 — Kafka 至少一次投递时可能 **丢处理** |
| KW-4 | low | `task_worker` 异常仅 log，进程可能空转 |
| KW-5 | low | 与 Agent checkpointer **无** 直接耦合 — 边界清晰 ✅ |
| KW-6 | info | 日志经 `config/logging.py` 可接 Kafka appenders — 运维向 |

---

## 4. 与 API 层衔接

生产者侧（未全文审）：

- `api/routers/document_processing/upload_routes.py`  
- `chunked_upload_routes.py`  

审查建议：确认 produce 前文件已落盘且 `task.body.file_path` 对 worker 可见。

---

## 5. 配置依赖（`settings`）

典型项（以 `config/settings.py` 为准）：

- `kafka_task_topic` / `kafka_retry_topic` / `kafka_task_group_id`  
- broker 地址、认证  

**部署**：Worker 独立进程；API 可不启 consumer。

---

## 6. 测试与可观测性

| 缺口 | 建议 |
|------|------|
| 集成测试 | mock Kafka + 小 PDF 端到端 |
| 指标 | 队列 lag、处理耗时、失败率 |
| 混沌 | Worker 中途 kill → 消息重投与幂等 |

当前 `tests/` 对 Kafka 覆盖极少（见 per_file 索引）。

---

## 7. 结论

- 流水线结构 **清晰**，职责分离合理。  
- 主要风险在 **部署拓扑**（共享文件系统）、**并发无界**、**幂等语义**（duplicate task_id skip）。  
- 与 Agent/会话审查正交，属 **文档入库子系统**。
