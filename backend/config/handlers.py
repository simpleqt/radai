"""
自定义日志处理器
"""

import atexit
import gzip
import logging
import logging.handlers
import queue
import shutil
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


class ColoredConsoleHandler(logging.StreamHandler):
    """
    彩色控制台日志处理器

    不同级别的日志使用不同颜色输出
    """

    COLORS = {
        "DEBUG": "\033[36m",  # 青色
        "INFO": "\033[32m",  # 绿色
        "WARNING": "\033[33m",  # 黄色
        "ERROR": "\033[31m",  # 红色
        "CRITICAL": "\033[35m",  # 紫色
    }
    RESET = "\033[0m"

    def __init__(self, stream=None):
        super().__init__(stream)
        self.formatter = None

    def emit(self, record):
        try:
            msg = self.format(record)
            levelname = record.levelname

            if levelname in self.COLORS:
                color = self.COLORS[levelname]
                msg = f"{color}{msg}{self.RESET}"

            stream = self.stream
            stream.write(msg + self.terminator)
            self.flush()
        except ValueError:
            return
        except Exception:
            self.handleError(record)


class RotatingFileHandlerWithCompression(logging.handlers.RotatingFileHandler):
    """
    支持压缩的轮转文件处理器

    当日志文件轮转时，自动压缩旧日志文件
    """

    def __init__(
        self,
        filename,
        mode="a",
        maxBytes=0,
        backupCount=0,
        encoding=None,
        delay=False,
        compress=True,
    ):
        super().__init__(filename, mode, maxBytes, backupCount, encoding, delay)
        self.compress = compress

    def doRollover(self):
        """
        执行日志轮转

        如果启用了压缩，则压缩旧日志文件
        """
        if self.stream:
            self.stream.close()
            self.stream = None

        if self.backupCount > 0:
            # 压缩最旧的备份
            if self.compress:
                self._compress_oldest_backup()

            # 轮转备份文件
            for i in range(self.backupCount - 1, 0, -1):
                src = (
                    f"{self.baseFilename}.{i}.gz"
                    if self.compress
                    else f"{self.baseFilename}.{i}"
                )
                dst = (
                    f"{self.baseFilename}.{i + 1}.gz"
                    if self.compress
                    else f"{self.baseFilename}.{i + 1}"
                )
                if Path(src).exists():
                    if Path(dst).exists():
                        Path(dst).unlink()
                    Path(src).rename(dst)

            # 压缩并移动当前日志文件
            if Path(self.baseFilename).exists():
                if self.compress:
                    self._compress_file(self.baseFilename, f"{self.baseFilename}.1.gz")
                else:
                    Path(self.baseFilename).rename(f"{self.baseFilename}.1")

        self.stream = self._open()

    def _compress_oldest_backup(self):
        """压缩最旧的备份文件（如果还没有压缩）"""
        oldest = f"{self.baseFilename}.{self.backupCount}"
        if Path(oldest).exists():
            self._compress_file(oldest, f"{oldest}.gz")
            Path(oldest).unlink()

    def _compress_file(self, src: str, dst: str):
        """压缩文件"""
        with open(src, "rb") as f_in:
            with gzip.open(dst, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)


class TimedRotatingFileHandlerWithArchive(logging.handlers.TimedRotatingFileHandler):
    """
    支持归档的定时轮转文件处理器

    按时间轮转日志文件，并归档到指定目录
    """

    def __init__(
        self,
        filename,
        when="midnight",
        interval=1,
        backupCount=0,
        encoding=None,
        delay=False,
        utc=False,
        atTime=None,
        archive_dir=None,
        compress=True,
    ):
        super().__init__(
            filename, when, interval, backupCount, encoding, delay, utc, atTime
        )
        self.archive_dir = archive_dir
        self.compress = compress

        if self.archive_dir:
            Path(self.archive_dir).mkdir(parents=True, exist_ok=True)

    def doRollover(self):
        """
        执行日志轮转

        将旧日志文件移动到归档目录
        """
        if self.stream:
            self.stream.close()
            self.stream = None

        currentTime = int(self.rolloverAt - self.interval)

        if self.backupCount > 0:
            # 生成归档文件名
            if self.archive_dir:
                archive_filename = self._get_archive_filename(currentTime)
                archive_path = Path(self.archive_dir) / archive_filename
            else:
                archive_path = Path(f"{self.baseFilename}.{currentTime}")

            # 移动当前日志文件到归档目录
            if Path(self.baseFilename).exists():
                if self.compress:
                    self._compress_and_archive(self.baseFilename, archive_path)
                else:
                    if archive_path.exists():
                        archive_path.unlink()
                    Path(self.baseFilename).rename(archive_path)

            # 清理过期的归档文件
            self._cleanup_old_archives()

        self.stream = self._open()

    def _get_archive_filename(self, timestamp: int) -> str:
        """生成归档文件名"""
        dt = datetime.fromtimestamp(timestamp)
        base_name = Path(self.baseFilename).stem
        ext = ".log.gz" if self.compress else ".log"
        return f"{base_name}_{dt.strftime('%Y_%m')}{ext}"

    def _compress_and_archive(self, src: str, dst: Path):
        """压缩并归档文件"""
        with open(src, "rb") as f_in:
            with gzip.open(dst, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        Path(src).unlink()

    def _cleanup_old_archives(self):
        """清理过期的归档文件"""
        if not self.archive_dir or self.backupCount <= 0:
            return

        archive_dir = Path(self.archive_dir)
        if not archive_dir.exists():
            return

        # 获取所有归档文件
        archive_files = sorted(archive_dir.glob("*.log*"), reverse=True)

        # 删除超过保留数量的文件
        for old_file in archive_files[self.backupCount :]:
            try:
                old_file.unlink()
            except Exception:
                pass


class ModuleFileHandler(logging.FileHandler):
    """
    模块文件处理器

    根据日志记录器的名称，将日志写入到对应的模块日志文件
    """

    MODULE_MAPPING = {
        "backend.api": "api.log",
        "backend.worker": "worker.log",
        "backend.kafka": "kafka.log",
        "backend.workflows": "workflow.log",
        "backend.db": "db.log",
        "backend.storage": "storage.log",
        "backend.integrations": "integration.log",
    }

    def __init__(self, logger_name: str, log_dir: str, **kwargs):
        self.logger_name = logger_name
        self.log_dir = log_dir

        # 根据日志记录器名称确定日志文件
        log_filename = self._get_log_filename(logger_name)
        log_path = Path(log_dir) / log_filename

        # 创建日志目录
        log_path.parent.mkdir(parents=True, exist_ok=True)

        super().__init__(log_path, **kwargs)

    def _get_log_filename(self, logger_name: str) -> str:
        """根据日志记录器名称获取日志文件名"""
        for module_prefix, filename in self.MODULE_MAPPING.items():
            if logger_name.startswith(module_prefix):
                return filename
        return "app.log"


def _is_backend_kafka_on_path(path: str) -> bool:
    """判断 sys.path 条目是否会将 backend/kafka 暴露为顶层 kafka 包。"""
    import os

    if not path:
        return False
    client_py = os.path.join(path, "kafka", "client.py")
    if not os.path.isfile(client_py):
        return False
    try:
        with open(client_py, encoding="utf-8", errors="ignore") as f:
            content = f.read(800)
    except OSError:
        return False
    return "AIOKafkaProducer" in content or "backend.config" in content


_KAFKA_PYTHON_CACHE: tuple[type, type] | None = None


def _import_kafka_python():
    """
    导入 kafka-python，避免 backend/kafka 遮蔽第三方 kafka 包。

    backend/__init__.py 会把 backend/ 加入 sys.path，
    导致 `import kafka` 解析到 backend/kafka 而非 kafka-python。
    """
    global _KAFKA_PYTHON_CACHE
    if _KAFKA_PYTHON_CACHE is not None:
        return _KAFKA_PYTHON_CACHE

    import importlib
    import sys

    shadow_paths = [p for p in sys.path if _is_backend_kafka_on_path(p)]
    removed_paths = []
    for path in shadow_paths:
        if path in sys.path:
            sys.path.remove(path)
            removed_paths.append(path)

    saved_modules = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "kafka" or name.startswith("kafka.")
    }

    try:
        kafka_module = importlib.import_module("kafka")
        errors_module = importlib.import_module("kafka.errors")
        _KAFKA_PYTHON_CACHE = (kafka_module.KafkaProducer, errors_module.KafkaError)
        return _KAFKA_PYTHON_CACHE
    except Exception:
        sys.modules.update(saved_modules)
        raise
    finally:
        for path in reversed(removed_paths):
            if path not in sys.path:
                sys.path.insert(0, path)


class KafkaLogHandler(logging.Handler):
    """
    异步 Kafka 日志处理器

    将日志记录序列化为 JSON 后异步发往 Kafka。

    设计要点：
    - emit() 把消息放入内存队列后立即返回，不阻塞业务线程
    - 后台守护线程批量取队列消息并发往 Kafka
    - Kafka 不可用时静默降级（本地文件 handler 永远作为兜底）
    - 进程正常退出时通过 atexit 钩子刷尽队列剩余消息
    - 使用 kafka-python 同步 Producer（非 aiokafka），
      因为 logging.Handler.emit() 可在任意线程调用，
      aiokafka 只能在 asyncio 协程中使用
    - 重连使用指数退避（最大 5 分钟），避免 Broker 不可用时频繁
      创建 KafkaProducer 触发 kafka.conn/kafka.client 日志噪声
    """

    _BATCH_SIZE = 100         # 后台线程单次批量发送上限
    _MAX_QUEUE_SIZE = 10_000  # 队列满时丢弃（不阻塞 emit）
    _QUEUE_TIMEOUT = 0.5      # 后台线程从队列 get 的超时（秒）

    # 重连退避：初始 30s，每次翻倍，上限 300s（5 分钟）
    _RECONNECT_INIT_DELAY = 30.0
    _RECONNECT_MAX_DELAY = 300.0

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
        self._producer: Optional[object] = None  # kafka.KafkaProducer
        self._running = True

        # 重连退避状态
        import time as _time
        self._reconnect_delay = self._RECONNECT_INIT_DELAY
        self._next_reconnect_at: float = 0.0  # 允许立即初次连接

        self._init_producer()

        self._thread = threading.Thread(
            target=self._sender_loop,
            name="kafka-log-sender",
            daemon=True,
        )
        self._thread.start()

        atexit.register(self.close)

    # ── 初始化 ──────────────────────────────────────────────────────

    def _init_producer(self) -> None:
        """初始化同步 KafkaProducer；失败则置为 None 并降级。

        带指数退避：连续失败时延迟重试，避免频繁创建 KafkaProducer
        触发 kafka.conn/kafka.client 大量 ERROR/WARNING 噪声日志。
        """
        import time as _time

        now = _time.monotonic()
        if now < self._next_reconnect_at:
            return  # 退避期内，不尝试重连

        try:
            KafkaProducer, _ = _import_kafka_python()

            self._producer = KafkaProducer(
                bootstrap_servers=self._bootstrap_servers,
                value_serializer=lambda v: v.encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
                acks="all",
                retries=1,              # 降低内部重试次数，减少连接噪声
                linger_ms=50,
                batch_size=32_768,
                compression_type="gzip",
                max_block_ms=2000,
                request_timeout_ms=5000,
                reconnect_backoff_ms=1000,       # 底层重连初始间隔 1s
                reconnect_backoff_max_ms=60000,  # 底层重连最大间隔 60s
            )
            # 连接成功，重置退避计时
            self._reconnect_delay = self._RECONNECT_INIT_DELAY
            self._next_reconnect_at = 0.0
        except Exception as exc:
            self._producer = None
            # 计算下次允许重连时间（指数退避）
            self._next_reconnect_at = now + self._reconnect_delay
            self._reconnect_delay = min(
                self._reconnect_delay * 2, self._RECONNECT_MAX_DELAY
            )
            print(
                f"[KafkaLogHandler] Kafka 连接失败，降级为丢弃模式: {exc}"
                f"（下次重试: {self._reconnect_delay:.0f}s 后）"
            )

    # ── logging.Handler 接口 ────────────────────────────────────────

    def emit(self, record: logging.LogRecord) -> None:
        """将 LogRecord 放入队列（非阻塞；队列满则静默丢弃）"""
        try:
            entry = self._record_to_entry(record)
            self._queue.put_nowait(entry)
        except queue.Full:
            pass
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        """关闭：停止后台线程 → 刷尽剩余 → 关闭 producer"""
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

    def _record_to_entry(self, record: logging.LogRecord):
        """将 LogRecord 转换为 LogEntrySchema"""
        from backend.kafka.log_schemas import LogEntrySchema, LogLevel

        exc_text: str | None = None
        if record.exc_info:
            exc_text = self.formatException(record.exc_info)

        # 提取业务扩展字段（由 LoggerAdapter.extra 注入）
        _extra_keys = {"task_id", "document_id", "kb_id", "conversation_id"}
        extra = {
            k: v
            for k, v in record.__dict__.items()
            if k in _extra_keys and v is not None
        }

        return LogEntrySchema(
            timestamp=datetime.utcfromtimestamp(record.created).isoformat() + "Z",
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
        """后台线程：批量拉取队列消息并发送至 Kafka"""
        while self._running:
            batch = []
            try:
                batch.append(self._queue.get(timeout=self._QUEUE_TIMEOUT))
                while len(batch) < self._BATCH_SIZE:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                continue

            self._flush_batch(batch)

        # 退出后清空剩余队列
        remaining = []
        while not self._queue.empty():
            try:
                remaining.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if remaining:
            self._flush_batch(remaining)

    def _flush_batch(self, batch: list) -> None:
        """发送一批日志到 Kafka；失败时尝试重连一次"""
        if not self._producer:
            self._init_producer()
            if not self._producer:
                return

        try:
            _, KafkaError = _import_kafka_python()

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
        except Exception as exc:
            print(f"[KafkaLogHandler] 发送失败，重置 producer: {exc}")
            # 关闭旧 producer（释放底层连接线程，停止其 ERROR 日志）
            try:
                if self._producer:
                    self._producer.close(timeout=1.0)
            except Exception:
                pass
            self._producer = None
            # 设置退避：下次 _flush_batch 调用 _init_producer 时会检查 _next_reconnect_at
            import time as _time
            self._next_reconnect_at = _time.monotonic() + self._reconnect_delay
            self._reconnect_delay = min(
                self._reconnect_delay * 2, self._RECONNECT_MAX_DELAY
            )
