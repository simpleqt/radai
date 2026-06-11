# 日志 Kafka 调用说明

> 版本：v1.0  
> 日期：2026-05-26  
> 集群地址：`<kafka_server_ip>:9092`

本文档介绍项目中 Kafka Topic / 分区的用途，以及日志管理后台如何消费、查询日志。

---

## 目录

1. [Topic 总览](#1-topic-总览)
2. [日志 Topic 详解](#2-日志-topic-详解)
3. [文档处理 Topic 详解](#3-文档处理-topic-详解)
4. [消息格式（LogEntrySchema）](#4-消息格式logentryschema)
5. [消费端调用示例](#5-消费端调用示例)
6. [常用查询场景](#6-常用查询场景)
7. [配置项说明](#7-配置项说明)
8. [Topic 创建与运维](#8-topic-创建与运维)

---

## 1. Topic 总览

### 1.1 先说清楚：日志功能只有 2 个 Topic

本次**日志输出 Kafka** 方案新增/使用的 Topic **只有下面 2 个**，日志管理后台只需消费这两个：

| Topic | 分区数 | 保留建议 | 用途 |
|-------|--------|---------|------|
| `application-logs` | 3 | 7 天 | 全量业务日志（INFO 及以上） |
| `application-logs-error` | 1 | 30 天 | 错误日志（ERROR / CRITICAL，从全量中额外双写） |

> **「3 个分区」不是 3 个 Topic**，而是 `application-logs` 这一个 Topic 内部分成 3 个 Partition，用于按 `service_name`（main-api / worker 等）分流。

---

### 1.2 文档处理 Topic（项目原有，与日志无关）

下面 6 个 Topic 是项目**原本就有的**文档 OCR/VLM 异步处理流水线，在 `.env` 第 90–114 行已配置，**不是日志方案新增的**：

| Topic | 分区数 | 用途 |
|-------|--------|------|
| `document-processing-tasks` | 6 | 文档处理任务提交 |
| `document-processing-status` | 6 | 任务状态 / 进度更新 |
| `document-processing-results` | 6 | 处理结果 |
| `document-processing-events` | 3 | 业务事件通知 |
| `document-processing-retry` | 3 | 失败任务重试 |
| `document-processing-dead-letter` | 1 | 死信（多次重试仍失败） |

日志管理后台**不需要**消费这 6 个 Topic。若文档只关心日志，看 [第 2 节](#2-日志-topic-详解) 即可。

---

### 1.3 全量 Topic 对照（含业务 Topic）

| Topic | 类型 | 分区数 | 谁在用 |
|-------|------|--------|--------|
| `application-logs` | **日志** | 3 | 日志管理后台 |
| `application-logs-error` | **日志** | 1 | 日志告警 |
| `document-processing-tasks` | 业务 | 6 | Worker 消费任务 |
| `document-processing-status` | 业务 | 6 | 状态更新 |
| `document-processing-results` | 业务 | 6 | 结果回调 |
| `document-processing-events` | 业务 | 3 | 事件通知 |
| `document-processing-retry` | 业务 | 3 | 重试队列 |
| `document-processing-dead-letter` | 业务 | 1 | 死信处理 |

---

## 2. 日志 Topic 详解

### 2.1 `application-logs` — 全量业务日志

**写入方**：各服务进程的 `KafkaLogHandler`（自动，无需业务代码改动）

**消息级别**：

| 来源模块 | 写入级别 | 典型内容 |
|---------|---------|---------|
| `backend.*` | INFO+ | 用户提问、Agent 推理、RAG 检索、API 调用、任务进度 |
| `uvicorn` / `fastapi` 等框架 | WARNING+ | HTTP 异常、框架告警 |
| `kafka.*` | 不写入 | 避免 Kafka 客户端日志回灌 |

**分区设计（3 分区）**：

消息 Key = `service_name`，同一服务的日志有序进入同一分区：

| 分区 | Key 路由 | 对应服务 | 说明 |
|------|---------|---------|------|
| Partition 0 | `main-api` | 主 API（8082） | 用户对话、知识库查询、文件上传等 |
| Partition 1 | `worker` / `ingest-worker` | 文档处理 Worker / 入库 Worker | OCR、VLM、知识库入库 |
| Partition 2 | `guest-api` 及其他 | 访客 API（8083）、MCP 等 | 访客对话、其他辅助服务 |

> Kafka 按 Key 哈希分配分区，上表为设计意图。实际分区号取决于 Key 哈希值，消费端应按 `service_name` 字段过滤，而非假设固定分区号。

**`service_name` 取值**：

| 值 | 设置方式 |
|----|---------|
| `main-api` | 默认（`.env` 中 `LOG_SERVICE_NAME=main-api`） |
| `guest-api` | 访客 API 自动识别，或 `LOG_SERVICE_NAME=guest-api` |
| `worker` | 启动时 `LOG_SERVICE_NAME=worker make run-worker` |
| `ingest-worker` | 启动时 `LOG_SERVICE_NAME=ingest-worker make run-ingest-worker` |

---

### 2.2 `application-logs-error` — 错误日志

**写入方**：同上，`KafkaLogHandler` 自动双写

**规则**：级别为 `ERROR` 或 `CRITICAL` 的日志，在写入 `application-logs` 的同时，**额外写入**此 Topic。

**分区设计（1 分区）**：

| 分区 | 说明 |
|------|------|
| Partition 0 | 所有服务的错误日志集中在此，便于告警消费端统一订阅 |

**典型用途**：

- 日志管理后台的错误告警
- 钉钉 / 企业微信 Webhook 推送
- 错误率监控大盘

---

## 3. 文档处理 Topic 详解

这组 Topic 用于文档 OCR / VLM 异步处理流水线，与日志 Topic 独立。

| Topic | 分区数 | 消费者组 | 消息 Key | 说明 |
|-------|--------|---------|---------|------|
| `document-processing-tasks` | 6 | `document-workers` | `task_id` | API 提交处理任务 |
| `document-processing-status` | 6 | `status-updaters` | `task_id` | Worker 上报进度 |
| `document-processing-results` | 6 | `result-handlers` | `task_id` | 处理完成 / 失败结果 |
| `document-processing-events` | 3 | `event-consumers` | `task_id` | 任务生命周期事件 |
| `document-processing-retry` | 3 | `retry-workers` | `task_id` | 失败任务延迟重试 |
| `document-processing-dead-letter` | 1 | `dead-letter-handlers` | `task_id` | 超过最大重试次数的死信 |

**分区说明（6 分区 Topic）**：

6 个分区按 `task_id` 哈希均匀分布，保证同一任务的消息有序，不同任务并行处理。分区号本身无业务含义，消费端按 `task_id` 关联即可。

---

## 4. 消息格式（LogEntrySchema）

日志 Topic 中每条消息为 JSON 字符串，结构如下：

```json
{
  "log_id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp": "2026-05-26T07:02:01.123456Z",
  "timestamp_ms": 1748240521123,
  "level": "INFO",
  "level_no": 20,
  "message": "文档处理服务启动",
  "logger_name": "backend.api.http_server",
  "service_name": "main-api",
  "host": "node-01",
  "process_id": 65963,
  "thread_name": "MainThread",
  "filename": "http_server.py",
  "lineno": 194,
  "func_name": "lifespan",
  "trace_id": null,
  "user_id": "user-123",
  "request_id": "req-abc",
  "exc_text": null,
  "extra": {
    "task_id": "task-001",
    "document_id": "doc-002",
    "kb_id": "kb-003",
    "conversation_id": "conv-004"
  }
}
```

**字段说明**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `log_id` | string | 是 | UUID，日志唯一标识 |
| `timestamp` | string | 是 | UTC ISO8601 时间 |
| `timestamp_ms` | int | 是 | 毫秒时间戳，用于排序和范围查询 |
| `level` | string | 是 | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| `level_no` | int | 是 | 数值级别（INFO=20, ERROR=40），便于 `>=` 过滤 |
| `message` | string | 是 | 日志正文 |
| `logger_name` | string | 是 | Python logger 名称，如 `backend.custom.api.v2.agent` |
| `service_name` | string | 是 | 服务来源，如 `main-api` |
| `host` | string | 是 | 主机名 |
| `process_id` | int | 是 | 进程 PID |
| `thread_name` | string | 是 | 线程名 |
| `filename` | string | 是 | 源文件名 |
| `lineno` | int | 是 | 行号 |
| `func_name` | string | 是 | 函数名 |
| `trace_id` | string | 否 | 请求链路 ID（通过 LoggerAdapter extra 注入） |
| `user_id` | string | 否 | 当前操作用户 ID |
| `request_id` | string | 否 | HTTP 请求 ID |
| `exc_text` | string | 否 | 异常堆栈（ERROR 时有值） |
| `extra` | object | 否 | 业务扩展字段（task_id、document_id 等） |

**常见 `logger_name` 与业务含义**：

| logger_name 前缀 | 业务含义 |
|-----------------|---------|
| `backend.custom.api.v2.agent` | 用户提问、Agent 流式回答 |
| `backend.api.http_server` | 服务启动、路由、预热 |
| `backend.api.routers.*` | HTTP API 请求处理 |
| `backend.rag.*` | RAG 检索、知识库操作 |
| `backend.worker.*` | 文档处理 Worker |
| `backend.kafka.client` | Kafka 任务提交 / 状态 |
| `backend.integrations.*` | 外部 LLM / VLM API 调用 |

---

## 5. 消费端调用示例

### 5.1 Python 消费（kafka-python）

```python
import json
from kafka import KafkaConsumer

consumer = KafkaConsumer(
    "application-logs",
    bootstrap_servers="<kafka_server_ip>:9092",
    group_id="log-management-consumers",   # 消费组名，同组内分区负载均衡
    auto_offset_reset="latest",            # 首次消费从最新开始；历史用 earliest
    enable_auto_commit=True,
    value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    key_deserializer=lambda k: k.decode("utf-8") if k else None,
)

for msg in consumer:
    log = msg.value
    print(
        msg.partition,                   # 分区号
        log["service_name"],
        log["level"],
        log["logger_name"],
        log["message"][:80],
    )
```

### 5.2 只订阅错误日志

```python
consumer = KafkaConsumer(
    "application-logs-error",
    bootstrap_servers="<kafka_server_ip>:9092",
    group_id="log-error-alert-consumers",
    auto_offset_reset="latest",
    value_deserializer=lambda v: json.loads(v.decode("utf-8")),
)
```

### 5.3 按服务名过滤（消费端）

```python
TARGET_SERVICES = {"main-api", "worker"}

for msg in consumer:
    log = msg.value
    if log["service_name"] not in TARGET_SERVICES:
        continue
    # 处理日志...
```

### 5.4 按时间范围过滤

```python
import datetime

start_ms = int(datetime.datetime(2026, 5, 26, 15, 0, 0).timestamp() * 1000)
end_ms   = int(datetime.datetime(2026, 5, 26, 16, 0, 0).timestamp() * 1000)

for msg in consumer:
    log = msg.value
    if start_ms <= log["timestamp_ms"] <= end_ms:
        # 处理日志...
        pass
```

### 5.5 按用户 / 链路追踪

```python
for msg in consumer:
    log = msg.value
    if log.get("user_id") == "target-user-id":
        print(log["message"])
    if log.get("trace_id") == "abc-123":
        print(log)   # 同一请求的全链路日志
```

### 5.6 命令行快速查看（kafka-console-consumer）

```bash
# 查看最新 20 条业务日志
kafka-console-consumer.sh \
  --bootstrap-server <kafka_server_ip>:9092 \
  --topic application-logs \
  --from-beginning \
  --max-messages 20

# 只看错误 Topic
kafka-console-consumer.sh \
  --bootstrap-server <kafka_server_ip>:9092 \
  --topic application-logs-error \
  --from-beginning
```

---

## 6. 常用查询场景

| 场景 | 订阅 Topic | 过滤条件 |
|------|-----------|---------|
| 查看所有业务日志 | `application-logs` | `logger_name` 以 `backend.` 开头 |
| 监控错误告警 | `application-logs-error` | 全量消费 |
| 查某用户的操作记录 | `application-logs` | `user_id == "xxx"` |
| 追踪单次请求链路 | `application-logs` | `trace_id == "xxx"` |
| 查 Worker 处理进度 | `application-logs` | `service_name == "worker"` |
| 查 Agent 对话日志 | `application-logs` | `logger_name` 含 `custom.api.v2.agent` |
| 查 RAG 检索过程 | `application-logs` | `logger_name` 以 `backend.rag.` 开头 |
| 查文档处理任务 | `application-logs` | `extra.task_id == "xxx"` |

---

## 7. 配置项说明

在 `backend/.env` 中配置：

```dotenv
# Kafka 集群
KAFKA_BOOTSTRAP_SERVERS=<kafka_server_ip>:9092

# 日志 Kafka 开关与 Topic
LOG_KAFKA_ENABLED=true                  # true=启用，false=仅写本地文件
LOG_KAFKA_TOPIC=application-logs        # 全量日志 Topic
LOG_KAFKA_ERROR_TOPIC=application-logs-error  # 错误日志 Topic
LOG_KAFKA_LEVEL=INFO                    # 最低发送级别
LOG_SERVICE_NAME=main-api               # 当前进程服务名（写入消息的 service_name）
```

**各进程 `LOG_SERVICE_NAME` 设置**：

```bash
# 主 API（默认，无需额外设置）
make run-api

# 访客 API
LOG_SERVICE_NAME=guest-api make run-guest-api

# 文档处理 Worker
LOG_SERVICE_NAME=worker make run-worker

# 知识库入库 Worker
LOG_SERVICE_NAME=ingest-worker make run-ingest-worker
```

**业务代码注入 trace_id / user_id**（可选）：

```python
from backend.config.loggers import get_logger
import logging

logger = get_logger(__name__)

# 方式一：LoggerAdapter
ctx_logger = logging.LoggerAdapter(logger, {
    "trace_id": "abc-123",
    "user_id": "user-456",
    "request_id": "req-789",
})
ctx_logger.info("用户发起提问: %s", question)

# 方式二：extra 参数
logger.info("任务提交", extra={"task_id": "task-001", "document_id": "doc-002"})
```

注入的字段会出现在 Kafka 消息的 `trace_id`、`user_id`、`request_id` 及 `extra` 中。

---

## 8. Topic 创建与运维

### 8.1 创建 Topic

```bash
cd <project_root>
venv314/bin/python -m backend.kafka.admin create-topics
```

会自动创建全部 Topic（含 `application-logs` 和 `application-logs-error`），已存在的会跳过。

### 8.2 查看 Topic 列表

```bash
venv314/bin/python -m backend.kafka.admin list-topics
```

### 8.3 推荐消费组命名

| 消费组 | 订阅 Topic | 用途 |
|--------|-----------|------|
| `log-management-consumers` | `application-logs` | 日志管理后台持久化 |
| `log-error-alert-consumers` | `application-logs-error` | 错误告警推送 |
| `document-workers` | `document-processing-tasks` | 文档处理 Worker |
| `status-updaters` | `document-processing-status` | 状态更新 |
| `result-handlers` | `document-processing-results` | 结果处理 |
| `event-consumers` | `document-processing-events` | 事件消费 |
| `retry-workers` | `document-processing-retry` | 重试 Worker |
| `dead-letter-handlers` | `document-processing-dead-letter` | 死信处理 |

### 8.4 注意事项

1. **本地文件始终兜底**：即使 Kafka 不可用，日志仍写入 `backend/logs/app.log`，不影响主业务。
2. **敏感信息过滤**：API Key、密码等含敏感关键词的日志不会发往 Kafka（`SensitiveDataFilter`）。
3. **kafka-python 包名冲突**：项目 `backend/kafka/` 目录与第三方包同名，消费端引用 kafka-python 时需从 site-packages 导入（项目内 `KafkaLogHandler` 已处理）。
4. **分区扩容**：若 `application-logs` 当前为 1 分区（自动创建），可通过 Admin 重建为 3 分区以按服务分流。

---

*文档结束*
