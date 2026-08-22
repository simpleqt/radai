# 日志输出 Kafka 方案设计

> 版本：v2.0  
> 日期：2026-05-26  
> 项目：langchian_project（RadAI / CSH-AI 后端）

---

## 目录

1. [目标与范围](#1-目标与范围)
2. [现有日志架构](#2-现有日志架构)
3. [改造方案](#3-改造方案)
4. [Kafka Topic 规划](#4-kafka-topic-规划)
5. [日志消息 Schema](#5-日志消息-schema)
6. [KafkaLogHandler 实现](#6-kafkaloghandler-实现)
7. [集成到现有 setup_logging()](#7-集成到现有-setup_logging)
8. [配置变更](#8-配置变更)
9. [降级与容错](#9-降级与容错)
10. [实施步骤](#10-实施步骤)

---

## 1. 目标与范围

**目标**：将现有各服务进程的日志（主 API、访客 API、Worker、入库 Worker）以结构化 JSON 格式输出到 Kafka，供日志管理后台消费处理。

**范围**：
- ✅ 新增 `KafkaLogHandler`，插入现有日志体系
- ✅ 现有业务代码**零改动**，`get_logger(__name__)` 调用不变
- ✅ 本地文件日志保留，作为兜底
- ❌ 不新增对外 API 服务
- ❌ 不新增数据库表

---

## 2. 现有日志架构

```
backend/config/logging.py   ← setup_logging()
backend/config/loggers.py   ← LoggerFactory / get_logger()
backend/config/handlers.py  ← ColoredConsoleHandler、RotatingFileHandlerWithCompression
backend/config/filters.py   ← SensitiveDataFilter、ErrorFilter
```

**当前 Handler 链**：

```
root logger
  ├─ ColoredConsoleHandler          → stdout（彩色）
  ├─ RotatingFileHandler            → backend/logs/app.log（100MB 轮转 + gzip）
  └─ RotatingFileHandler（ERROR）   → backend/logs/app.error.log
```

**现有 Kafka 配置**（`backend/.env` 第 90-114 行）：

```dotenv
KAFKA_BOOTSTRAP_SERVERS=kafka:9092       # compose 内部；宿主机调试可用 localhost:9093
KAFKA_TOPICS_PREFIX=document-processing-
KAFKA_TASK_TOPIC=document-processing-tasks
KAFKA_STATUS_TOPIC=document-processing-status
KAFKA_RESULT_TOPIC=document-processing-results
KAFKA_EVENT_TOPIC=document-processing-events
KAFKA_RETRY_TOPIC=document-processing-retry
KAFKA_DEAD_LETTER_TOPIC=document-processing-dead-letter
KAFKA_NUM_PARTITIONS=6
KAFKA_REPLICATION_FACTOR=1
```

---

## 3. 改造方案

在现有 Handler 链末尾**追加一个 `KafkaLogHandler`**，其余结构不变：

```
root logger
  ├─ ColoredConsoleHandler          → stdout（不变）
  ├─ RotatingFileHandler            → app.log（不变，兜底）
  ├─ RotatingFileHandler（ERROR）   → app.error.log（不变）
  └─ KafkaLogHandler（新增）        → Kafka: application-logs
                                          ↑
                               非阻塞队列 + 后台线程
```

**改动文件清单**：

| 文件 | 改动内容 |
|------|---------|
| `backend/kafka/log_schemas.py` | 新增文件：`LogEntrySchema` Pydantic 模型 |
| `backend/config/handlers.py` | 追加 `KafkaLogHandler` 类 |
| `backend/config/logging.py` | `setup_logging()` 末尾追加 Kafka handler 注册 |
| `backend/config/settings.py` | 追加 Log Kafka 配置字段 |
| `backend/.env` | 追加 `LOG_KAFKA_*` 配置项 |

---

## 4. Kafka Topic 规划

在现有 `document-processing-*` Topic 基础上新增两个日志专用 Topic：

| Topic | 用途 | 分区数 | 保留时间 |
|-------|------|--------|---------|
| `application-logs` | 全量日志（INFO 及以上） | 3 | 7 天 |
| `application-logs-error` | 仅 ERROR / CRITICAL | 1 | 30 天 |

**分区 Key**：`service_name`（如 `main-api`、`worker`），保证同一服务的日志有序。

**Topic 创建命令**（使用现有 `backend.kafka.admin` 模块）：

```bash
cd /home/inspur/myenv/langchian_project
venv314/bin/python -m backend.kafka.admin create-topics
```

该命令按 `backend/config/settings_sections.py` 中的 `KAFKA_*` 与 `LOG_KAFKA_*` 配置一次性创建文档处理 6 个 Topic 以及 `application-logs`、`application-logs-error`。当前脚本不支持 `create-topic <name> --partitions ...` 这种单 Topic CLI；如需单独调整保留时间或扩容分区，使用 Kafka 原生命令或扩展 `backend/kafka/admin.py`。

---

## 5. 日志消息 Schema

**新增文件**：`backend/kafka/log_schemas.py`

```python
"""
日志 Kafka 消息 Schema
结构化日志条目，发送至 application-logs topic
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

class LogEntrySchema(BaseModel):
    """发送到 Kafka 的结构化日志条目"""

    # 唯一标识
    log_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # 时间（UTC ISO8601 + 毫秒时间戳，便于消费端排序）
    timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat() + "Z"
    )
    timestamp_ms: int = Field(
        default_factory=lambda: int(datetime.utcnow().timestamp() * 1000)
    )

    # 日志内容
    level: LogLevel
    level_no: int           # logging.levelno，便于消费端数值过滤
    message: str
    logger_name: str        # record.name，如 backend.api.routers.rag

    # 来源定位
    service_name: str       # 服务名：main-api / guest-api / worker / ingest-worker
    host: str               # 主机名
    process_id: int
    thread_name: str
    filename: str
    lineno: int
    func_name: str

    # 链路追踪（可选，由 LoggerAdapter extra 注入）
    trace_id: str | None = None
    user_id: str | None = None
    request_id: str | None = None

    # 异常堆栈（可选）
    exc_text: str | None = None

    # 业务扩展字段（如 task_id、document_id 等）
    extra: dict[str, Any] = Field(default_factory=dict)
```

**JSON 消息示例**：

```json
{
  "log_id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp": "2026-05-26T06:00:00.123Z",
  "timestamp_ms": 1748239200123,
  "level": "ERROR",
  "level_no": 40,
  "message": "Failed to start Kafka producer: connection refused",
  "logger_name": "backend.kafka.client",
  "service_name": "main-api",
  "host": "node-01",
  "process_id": 12345,
  "thread_name": "MainThread",
  "filename": "client.py",
  "lineno": 71,
  "func_name": "start",
  "trace_id": null,
  "user_id": null,
  "request_id": null,
  "exc_text": "Traceback (most recent call last):\n  ...",
  "extra": {}
}
```

---

## 6. KafkaLogHandler 实现

**修改文件**：`backend/config/handlers.py`（在文件末尾追加以下类）

```python
# backend/config/handlers.py（末尾追加）

import atexit
import logging
import queue
import socket
import threading
from typing import Optional

from kafka import KafkaProducer
from kafka.errors import KafkaError

from backend.kafka.log_schemas import LogEntrySchema, LogLevel

class KafkaLogHandler(logging.Handler):
    """
    异步 Kafka 日志处理器

    将日志记录序列化为 JSON 后异步发送至 Kafka。
    - 内部使用 queue.Queue + 后台线程，emit() 立即返回，不阻塞业务线程
    - Kafka 不可用时静默降级（本地文件 handler 作为兜底）
    - 进程退出时通过 atexit 钩子刷尽队列剩余消息
    """

    # 后台线程单次批量发送上限
    _BATCH_SIZE = 100
    # 队列满时丢弃（不阻塞 emit）
    _MAX_QUEUE_SIZE = 10_000
    # 后台线程从队列 get 的超时（秒）
    _QUEUE_TIMEOUT = 0.5

    def __init__(
        self,
        bootstrap_servers: str,
        topic: str = "application-logs",
        error_topic: str = "application-logs-error",
        service_name: str = "unknown",
        level: int = logging.INFO,
        flush_timeout: float = 5.0,
    ):
        super().__init__(level)
        self.topic = topic
        self.error_topic = error_topic
        self.service_name = service_name
        self.flush_timeout = flush_timeout
        self._bootstrap_servers = bootstrap_servers
        self._host = socket.gethostname()

        self._queue: queue.Queue = queue.Queue(maxsize=self._MAX_QUEUE_SIZE)
        self._producer: Optional[KafkaProducer] = None
        self._running = True

        self._init_producer()

        self._thread = threading.Thread(
            target=self._sender_loop,
            name="kafka-log-sender",
            daemon=True,
        )
        self._thread.start()

        # 进程正常退出时刷尽剩余日志
        atexit.register(self.close)

    # ── 初始化 ──────────────────────────────────────────────────────

    def _init_producer(self) -> None:
        """初始化同步 KafkaProducer（失败则降级为 None）"""
        try:
            self._producer = KafkaProducer(
                bootstrap_servers=self._bootstrap_servers,
                value_serializer=lambda v: v.encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
                acks="all",
                retries=3,
                linger_ms=50,           # 聚合 50ms 内的消息批量发送
                batch_size=32_768,      # 32KB 批次
                compression_type="gzip",
                max_block_ms=2000,      # send() 最大阻塞 2s（含元数据刷新）
                request_timeout_ms=5000,
            )
        except KafkaError as exc:
            self._producer = None
            # 使用 print 避免触发递归 logging
            print(f"[KafkaLogHandler] Kafka 连接失败，降级为丢弃模式: {exc}")

    # ── logging.Handler 接口 ────────────────────────────────────────

    def emit(self, record: logging.LogRecord) -> None:
        """将 LogRecord 放入队列（非阻塞，队列满则丢弃）"""
        try:
            entry = self._record_to_entry(record)
            self._queue.put_nowait(entry)
        except queue.Full:
            pass   # 静默丢弃，文件 handler 作为兜底
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        """关闭：停止后台线程，刷尽剩余消息，关闭 producer"""
        self._running = False
        self._thread.join(timeout=self.flush_timeout)
        if self._producer:
            try:
                self._producer.flush(timeout=self.flush_timeout)
                self._producer.close(timeout=self.flush_timeout)
            except Exception:
                pass
        super().close()

    # ── 内部实现 ────────────────────────────────────────────────────

    def _record_to_entry(self, record: logging.LogRecord) -> LogEntrySchema:
        """将 LogRecord 转换为结构化 LogEntrySchema"""
        exc_text: str | None = None
        if record.exc_info:
            exc_text = self.formatException(record.exc_info)

        # 提取业务扩展字段（由 LoggerAdapter.extra 注入）
        _known = {
            "trace_id", "user_id", "request_id",
            "task_id", "document_id", "kb_id",
        }
        extra = {
            k: v for k, v in record.__dict__.items()
            if k in (_known - {"trace_id", "user_id", "request_id"})
            and v is not None
        }

        return LogEntrySchema(
            timestamp=__import__("datetime").datetime
                .utcfromtimestamp(record.created).isoformat() + "Z",
            timestamp_ms=int(record.created * 1000),
            level=LogLevel(record.levelname),
            level_no=record.levelno,
            message=record.getMessage(),
            logger_name=record.name,
            service_name=self.service_name,
            host=self._host,
            process_id=record.process or 0,
            thread_name=record.threadName or "",
            filename=record.filename,
            lineno=record.lineno,
            func_name=record.funcName,
            trace_id=getattr(record, "trace_id", None),
            user_id=getattr(record, "user_id", None),
            request_id=getattr(record, "request_id", None),
            exc_text=exc_text,
            extra=extra,
        )

    def _sender_loop(self) -> None:
        """后台线程主循环：批量拉取队列消息并发送至 Kafka"""
        while self._running:
            batch: list[LogEntrySchema] = []
            try:
                # 阻塞等待第一条消息
                batch.append(self._queue.get(timeout=self._QUEUE_TIMEOUT))
                # 非阻塞追加更多，凑批次
                while len(batch) < self._BATCH_SIZE:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                continue

            self._flush(batch)

        # 退出后清空剩余队列
        remaining: list[LogEntrySchema] = []
        while not self._queue.empty():
            try:
                remaining.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if remaining:
            self._flush(remaining)

    def _flush(self, batch: list[LogEntrySchema]) -> None:
        """发送一批日志到 Kafka；失败时尝试重连一次"""
        if not self._producer:
            self._init_producer()
            if not self._producer:
                return

        try:
            for entry in batch:
                dest_topic = (
                    self.error_topic
                    if entry.level_no >= logging.ERROR
                    else self.topic
                )
                self._producer.send(
                    topic=dest_topic,
                    value=entry.model_dump_json(),
                    key=entry.service_name,
                )
            self._producer.flush(timeout=2.0)
        except KafkaError as exc:
            print(f"[KafkaLogHandler] 发送失败，重置 producer: {exc}")
            self._producer = None
```

---

## 6.1 KafkaBusinessFilter（配套过滤器）

**修改文件**：`backend/config/filters.py`（末尾追加以下类）

```python
# backend/config/filters.py（末尾追加）

class KafkaBusinessFilter(logging.Filter):
    """
    Kafka 业务日志过滤器

    规则：
    - backend.* 模块：INFO 及以上全部发往 Kafka（用户提问、API 调用、业务流程）
    - uvicorn / fastapi / starlette 框架层：只发 WARNING+（访问日志噪音多）
    - 其余第三方库：只发 WARNING+（_configure_third_party_loggers 已压制，双重保险）
    """

    # 业务模块前缀：INFO+ 全量发送
    _BUSINESS_PREFIXES = (
        "backend.",
        "__main__",
    )

    # 框架 / 第三方：只发 WARNING+
    _FRAMEWORK_PREFIXES = (
        "uvicorn",
        "fastapi",
        "starlette",
        "aiokafka",
        "kafka",
        "httpx",
        "httpcore",
        "sqlalchemy",
        "aiomysql",
        "pymysql",
        "nebula3",
        "watchfiles",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name

        # 业务模块：放行 INFO+
        for prefix in self._BUSINESS_PREFIXES:
            if name.startswith(prefix):
                return True

        # 框架/第三方：只放行 WARNING+
        for prefix in self._FRAMEWORK_PREFIXES:
            if name.startswith(prefix):
                return record.levelno >= logging.WARNING

        # 其余未知模块：只放行 WARNING+（保守策略）
        return record.levelno >= logging.WARNING
```

---

## 7. 集成到现有 setup_logging()

**修改文件**：`backend/config/logging.py`

在 `setup_logging()` 函数末尾（`_configure_third_party_loggers()` 调用之后）追加以下代码块：

```python
# backend/config/logging.py

# ── 在文件顶部 import 区域追加 ──
from backend.config.handlers import KafkaLogHandler   # 新增

# ── 在 setup_logging() 函数末尾追加（替换原来的 logging.info 输出区域之前）──

    # Kafka 日志 Handler（可选，通过 LOG_KAFKA_ENABLED 控制）
    if getattr(settings, "log_kafka_enabled", False):
        _service_name = settings.log_service_name
        # Worker 进程可通过环境变量覆盖服务名
        import os
        _service_name = os.getenv("LOG_SERVICE_NAME", _service_name)
        if guest_mode:
            _service_name = "guest-api"

        kafka_handler = KafkaLogHandler(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            topic=settings.log_kafka_topic,
            error_topic=settings.log_kafka_error_topic,
            service_name=_service_name,
            level=logging.getLevelName(settings.log_kafka_level.upper()),
        )
        kafka_handler.addFilter(SensitiveDataFilter())
        kafka_handler.addFilter(KafkaBusinessFilter())   # 新增：智能模块过滤
        root_logger.addHandler(kafka_handler)

    # 现有的 logging.info 输出（不变）
    logging.info("%s日志系统初始化完成", service_name)
    logging.info("日志目录: %s", log_dir)
    logging.info("日志文件: %s", app_log_path)
    logging.info("日志级别: %s", settings.log_level)
    logging.info("日志格式: %s", settings.log_format)
    if getattr(settings, "log_kafka_enabled", False):
        logging.info("Kafka 日志已启用: topic=%s, service=%s",
                     settings.log_kafka_topic, _service_name)
```

完整的 `setup_logging()` 执行路径如下：

```
setup_logging()
  ├─ 创建日志目录
  ├─ 获取 formatter
  ├─ 配置 root logger 级别
  ├─ 添加 ColoredConsoleHandler   （原有）
  ├─ 添加 RotatingFileHandler     （原有）
  ├─ 添加 RotatingFileHandler(ERROR) （原有）
  ├─ _configure_third_party_loggers()（原有）
  └─ 添加 KafkaLogHandler         （新增，LOG_KAFKA_ENABLED=true 时生效）
```

---

## 8. 配置变更

### 8.1 `.env` 新增配置

在 `.env` 的 Kafka 配置区（第 90 行附近）末尾追加：

```dotenv
# ==================== Log Kafka Configuration ====================
# 是否启用 Kafka 日志输出（false = 不输出到 Kafka，保持原有行为）
LOG_KAFKA_ENABLED=false

# 日志输出 Topic
LOG_KAFKA_TOPIC=application-logs
LOG_KAFKA_ERROR_TOPIC=application-logs-error

# 发送到 Kafka 的最低日志级别（低于此级别不发往 Kafka，减轻压力）
# 建议生产环境设为 WARNING，调试期设为 INFO
LOG_KAFKA_LEVEL=INFO

# 本进程的服务名标识（在 Kafka 消息中用于区分来源）
# Worker 进程启动时可通过命令行环境变量覆盖：
#   LOG_SERVICE_NAME=worker make run-worker
LOG_SERVICE_NAME=main-api
```

### 8.2 settings.py 新增字段

在 `backend/config/settings.py` 的 Kafka 配置字段区末尾追加：

```python
# backend/config/settings.py（Kafka 配置区末尾追加）

# Log Kafka
log_kafka_enabled: bool = Field(False, alias="LOG_KAFKA_ENABLED")
log_kafka_topic: str = Field("application-logs", alias="LOG_KAFKA_TOPIC")
log_kafka_error_topic: str = Field("application-logs-error", alias="LOG_KAFKA_ERROR_TOPIC")
log_kafka_level: str = Field("INFO", alias="LOG_KAFKA_LEVEL")
log_service_name: str = Field("main-api", alias="LOG_SERVICE_NAME")
```

### 8.3 各进程服务名配置

不同进程启动时通过 `LOG_SERVICE_NAME` 环境变量区分来源，Makefile 或脚本中设置：

```makefile
# Makefile（示例）

run-api:
    LOG_SERVICE_NAME=main-api python -m backend.api.http_server

run-guest-api:
    LOG_SERVICE_NAME=guest-api GUEST_API_SERVICE=1 python -m backend.api.guest_http_server

run-worker:
    LOG_SERVICE_NAME=worker python -m backend.worker.task_worker

run-ingest-worker:
    LOG_SERVICE_NAME=ingest-worker python -m backend.worker.ingest_worker
```

---

## 9. 降级与容错

### 9.1 三层保障

```
KafkaLogHandler.emit()
  │
  ├─ 正常：放入 queue.Queue（非阻塞，立即返回）
  │         后台线程批量发往 Kafka
  │
  ├─ 队列满（10000条积压）：
  │    → 静默丢弃该条日志（queue.Full）
  │    → 不影响业务线程
  │
  └─ Kafka 连接失败：
       → _flush() 中捕获 KafkaError
       → self._producer = None，下批自动重连
       → 重连失败继续丢弃，直到 Kafka 恢复
```

**关键保障**：`RotatingFileHandler` 始终保留，任何情况下日志都会写入本地文件，Kafka 输出是额外的增量能力。

### 9.2 `kafka-python` vs `aiokafka`

`KafkaLogHandler` 使用同步的 `kafka-python`（非 `aiokafka`）：

- `logging.Handler.emit()` 运行在任意线程（包括非 asyncio 线程）
- `aiokafka` 需要在 asyncio 事件循环中使用，在普通线程中调用会报错
- `kafka-python` 同步 API 在后台线程中使用更简洁稳定

项目中 `aiokafka` 已用于业务消息（`document-processing-*`），两者并存无冲突。

### 9.3 敏感数据过滤

`KafkaLogHandler` 注册了 `SensitiveDataFilter`（与文件 handler 一致），API Key、密码等不会发往 Kafka。

---

## 10. 实施步骤

### Step 1：新增 log_schemas.py

创建 `backend/kafka/log_schemas.py`，内容见[第 5 节](#5-日志消息-schema)。

### Step 2：修改 handlers.py

在 `backend/config/handlers.py` 末尾追加 `KafkaLogHandler` 类，内容见[第 6 节](#6-kafkaloghandler-实现)。

需确认 `kafka-python` 已安装（项目 `requirements.txt` 中已有 `kafka-python>=2.3.0`）：

```bash
pip show kafka-python
```

### Step 3：修改 settings.py

在 `backend/config/settings.py` 的 Kafka 配置区末尾追加 5 个字段，见[第 8.2 节](#82-settingspy-新增字段)。

### Step 4：修改 .env

在 `.env` 末尾追加 Log Kafka 配置，见[第 8.1 节](#81-env-新增配置)。

### Step 5：修改 logging.py

在 `backend/config/logging.py` 的 `setup_logging()` 中追加 Kafka handler 注册，见[第 7 节](#7-集成到现有-setup_logging)。

### Step 6：创建 Kafka Topic

```bash
venv314/bin/python -m backend.kafka.admin create-topics
venv314/bin/python -m backend.kafka.admin list-topics
venv314/bin/python -m backend.kafka.admin print-config
```

### Step 7：测试验证

```bash
# 开发环境先关闭 Kafka 输出，确保无副作用
LOG_KAFKA_ENABLED=false make run-api

# 开启后验证消息是否到达 Kafka
LOG_KAFKA_ENABLED=true LOG_KAFKA_LEVEL=DEBUG make run-api

# 用 kafka-python 消费验证
python - <<'EOF'
from kafka import KafkaConsumer
import json
import os

c = KafkaConsumer(
    "application-logs",
    bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9093"),
    auto_offset_reset="latest",
    value_deserializer=lambda v: json.loads(v.decode()),
)
for msg in c:
    print(msg.value)
EOF
```

### Step 8：生产环境开启

```dotenv
# .env 修改
LOG_KAFKA_ENABLED=true
LOG_KAFKA_LEVEL=INFO        # 发送 INFO 及以上，保留用户问题、业务流程、API 调用日志
```

**日志级别策略说明**：

| 来源 | 发往 Kafka 的级别 | 原因 |
|------|-----------------|------|
| `backend.*`（业务模块） | **INFO+** | 用户提问、API 调用、Agent 推理、RAG 检索等核心业务日志 |
| `aiokafka`、`httpx`、`sqlalchemy` 等第三方库 | **WARNING+** | 已由 `_configure_third_party_loggers()` 在 logger 级别压制，实际不会产生 INFO 日志 |
| `uvicorn`、`fastapi` | **WARNING+** | 框架自身 INFO 日志（请求访问记录等）噪音多，只保留异常 |

> **实际效果**：`LOG_KAFKA_LEVEL=INFO` 下，Kafka 中收到的几乎全是 `backend.*` 的业务日志，
> 第三方库已在 `_configure_third_party_loggers()` 中被设置为 WARNING 级别，INFO 根本不会发出，
> 无需额外担心噪音。

#### 需要哪些业务日志进入 Kafka

以下是用户关心的典型日志来源，均属于 `backend.*` 模块，INFO 级别即可覆盖：

```
backend.custom.api.v2.agent      ← 用户提问内容、Agent 思考步骤
backend.api.routers.*            ← HTTP API 请求参数、响应状态
backend.rag.*                    ← RAG 检索过程、命中文档、相关性得分
backend.kafka.client             ← 文档处理任务提交、状态变更
backend.worker.*                 ← 任务处理进度、OCR/VLM 调用
backend.integrations.*           ← 外部 LLM/VLM API 调用参数、耗时
backend.db.*                     ← 关键数据库操作
```

如需进一步降低 Kafka 消息量，可将 `LOG_KAFKA_LEVEL` 设为 `INFO`，
并在 `setup_logging()` 中追加 `KafkaBusinessFilter`（见下节）让框架层日志只发 WARNING+。

---

## 附：改动文件汇总

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/kafka/log_schemas.py` | **新建** | `LogEntrySchema` Pydantic 模型 |
| `backend/config/handlers.py` | **追加** | `KafkaLogHandler` 类（约 120 行） |
| `backend/config/filters.py` | **追加** | `KafkaBusinessFilter` 类（约 35 行） |
| `backend/config/logging.py` | **追加** | `setup_logging()` 末尾注册 Kafka handler（约 15 行） |
| `backend/config/settings.py` | **追加** | 5 个 `log_kafka_*` 配置字段 |
| `backend/.env` | **追加** | `LOG_KAFKA_ENABLED` 等 5 行配置 |

**总改动量极小，现有代码逻辑零影响。**

---

*文档结束*
