"""
日志过滤器
"""

import logging
import threading
from collections import OrderedDict
from contextvars import ContextVar
from typing import Optional


# 每个请求的用户上下文，通过中间件注入
_user_id_ctx: ContextVar[Optional[str]] = ContextVar("user_id", default=None)
_username_ctx: ContextVar[Optional[str]] = ContextVar("username", default=None)
_request_id_ctx: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def set_user_context(
    user_id: Optional[str] = None,
    username: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    """设置当前请求的用户上下文（在中间件中调用）"""
    if user_id is not None:
        _user_id_ctx.set(user_id)
    if username is not None:
        _username_ctx.set(username)
    if request_id is not None:
        _request_id_ctx.set(request_id)


def clear_user_context() -> None:
    """清除当前请求的用户上下文"""
    _user_id_ctx.set(None)
    _username_ctx.set(None)
    _request_id_ctx.set(None)


def get_user_context() -> dict:
    """获取当前请求的用户上下文"""
    return {
        "user_id": _user_id_ctx.get(),
        "username": _username_ctx.get(),
        "request_id": _request_id_ctx.get(),
    }


class LevelFilter(logging.Filter):
    """
    级别过滤器

    只允许特定级别及以上的日志通过
    """

    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.level


class ModuleFilter(logging.Filter):
    """
    模块过滤器

    只允许特定模块的日志通过
    """

    def __init__(self, module_prefix: str):
        super().__init__()
        self.module_prefix = module_prefix

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self.module_prefix)


class ErrorFilter(logging.Filter):
    """
    错误过滤器

    只允许 ERROR 和 CRITICAL 级别的日志通过
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR


class SensitiveDataFilter(logging.Filter):
    """
    敏感数据过滤器

    过滤日志中的敏感信息（密码、密钥等）
    """

    SENSITIVE_PATTERNS = [
        ("password", "***"),
        ("passwd", "***"),
        ("secret", "***"),
        ("token", "***"),
        ("api_key", "***"),
        ("apikey", "***"),
        ("access_key", "***"),
        ("private_key", "***"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()

        for pattern, _replacement in self.SENSITIVE_PATTERNS:
            if pattern.lower() in msg.lower():
                return False

        return True


class DuplicateFilter(logging.Filter):
    """
    重复日志过滤器

    过滤重复的日志消息
    """

    def __init__(self, max_duplicates: int = 3, max_tracked_messages: int = 10000):
        super().__init__()
        self.max_duplicates = max_duplicates
        self.max_tracked_messages = max(100, max_tracked_messages)
        self.message_counts: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        with self._lock:
            count = self.message_counts.get(msg, 0) + 1
            self.message_counts[msg] = count
            self.message_counts.move_to_end(msg)

            while len(self.message_counts) > self.max_tracked_messages:
                self.message_counts.popitem(last=False)

            return count <= self.max_duplicates

    def clear(self):
        """清除计数器"""
        with self._lock:
            self.message_counts.clear()


class ContextFilter(logging.Filter):
    """
    上下文过滤器

    为日志记录添加上下文信息
    """

    def __init__(self, context: dict):
        super().__init__()
        self.context = context

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in self.context.items():
            setattr(record, key, value)
        return True


class PerformanceFilter(logging.Filter):
    """
    性能过滤器

    过滤性能相关的日志
    """

    PERFORMANCE_KEYWORDS = [
        "slow",
        "timeout",
        "latency",
        "performance",
        "bottleneck",
    ]

    def __init__(self, include: bool = True):
        super().__init__()
        self.include = include

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage().lower()

        has_performance_info = any(
            keyword in msg for keyword in self.PERFORMANCE_KEYWORDS
        )

        if self.include:
            return has_performance_info
        else:
            return not has_performance_info


class KafkaBusinessFilter(logging.Filter):
    """
    Kafka 业务日志过滤器

    按日志来源模块分两档控制发往 Kafka 的最低级别：
    - backend.* / __main__：INFO+ 全量发送（用户提问、API 调用、业务流程）
    - uvicorn / fastapi / 第三方库：只发 WARNING+（避免框架噪音）
    - 其余未知模块：只发 WARNING+（保守策略）

    与 _configure_third_party_loggers() 形成双重保障：
    第三方库 logger 已被设置为 WARNING 级别，实际不会产生 INFO 日志。
    """

    # 业务模块：INFO+ 全量发往 Kafka
    _BUSINESS_PREFIXES: tuple[str, ...] = (
        "backend.",
        "__main__",
    )

    # 框架 / 第三方：只发 WARNING+
    _NOISY_PREFIXES: tuple[str, ...] = (
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
        "asyncio",
        "concurrent",
        "multiprocessing",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name

        # kafka-python 自身日志不回灌到 Kafka，避免噪音和递归
        if name == "kafka" or name.startswith("kafka."):
            return False

        for prefix in self._BUSINESS_PREFIXES:
            if name.startswith(prefix):
                return True

        for prefix in self._NOISY_PREFIXES:
            if name.startswith(prefix):
                return record.levelno >= logging.WARNING

        # 未知模块保守策略：只发 WARNING+
        return record.levelno >= logging.WARNING


class UserContextFilter(logging.Filter):
    """
    用户上下文过滤器

    从 contextvars 中读取当前请求的用户信息（user_id、username、request_id），
    并将其注入到每条日志记录中，供 Formatter 使用。
    未登录请求时字段为空字符串，不影响日志格式的稳定性。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.user_id = _user_id_ctx.get() or "-"
        record.username = _username_ctx.get() or "-"
        record.request_id = _request_id_ctx.get() or "-"
        return True
