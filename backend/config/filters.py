"""
日志过滤器
"""

import logging
import re
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


class ErrorFilter(logging.Filter):
    """
    错误过滤器

    只允许 ERROR 和 CRITICAL 级别的日志通过
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR


class SensitiveDataFilter(logging.Filter):
    """敏感数据脱敏过滤器。

    把 ``key=value`` / ``"key": value`` 里的敏感值替换为 ``***``，而不是整条
    丢弃日志。旧实现用裸子串 ``if "token" in msg: return False``：既误杀含
    ``tokens=``/``total_tokens`` 的正常记账与性能日志（整条丢失），又只挂在
    部分 handler 上。现在按「键名 + 分隔符」精确匹配，用词边界让 ``token`` 不
    命中 ``tokens``，其余正文保留。
    """

    # 键名后必须紧跟 = 或 : 才算敏感键值对；键名两侧允许可选引号（JSON 风格）。
    # 注意：token 的前缀组合（access_token/refresh_token/session_token/id_token）
    # 必须显式列出——``_`` 是 word 字符，``\btoken\b`` 不会命中 ``access_token``。
    _SENSITIVE_KV = re.compile(
        r"([\"']?\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|"
        r"private[_-]?key|"
        r"(?:access|refresh|session|id|auth|bearer|csrf)?[_-]?token|"
        r"authorization(?:\s+bearer)?|cookie|session[_-]?id|jwt)"
        r"[\"']?\s*[=:]\s*)"
        r"(\"[^\"]*\"|'[^']*'|[^\s,;}&]+)",
        re.IGNORECASE,
    )
    # 连接串 userinfo 密码段：scheme://user:password@host → scheme://user:***@host
    # （用户名允许为空，覆盖 redis://:pass@host 形态；无 @ 的 host:port 不受影响）
    _SENSITIVE_URI = re.compile(
        r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]*:)[^@\s/]+(@)",
    )
    # Bearer 凭据本体（出现在冒号之后，键值对规则够不到）
    _SENSITIVE_BEARER = re.compile(
        r"(\bbearer\s+)([A-Za-z0-9._\-~+/=]+)",
        re.IGNORECASE,
    )
    _REDACTED = "***"

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = self._SENSITIVE_URI.sub(
            lambda match: f"{match.group(1)}{self._REDACTED}{match.group(2)}",
            message,
        )
        # bearer 先于键值对：否则 authorization 的值会先吞掉 "Bearer" 这个词
        redacted = self._SENSITIVE_BEARER.sub(
            lambda match: f"{match.group(1)}{self._REDACTED}", redacted
        )
        redacted = self._SENSITIVE_KV.sub(
            lambda match: f"{match.group(1)}{self._REDACTED}", redacted
        )
        if redacted != message:
            # 用脱敏后的静态串替换，并清空 args 防止 formatter 二次格式化。
            record.msg = redacted
            record.args = None
        # 异常栈文本由 formatter 在消息之后渲染，filter 阶段不可见；
        # 预先脱敏 record.exc_text（已渲染缓存）与 exc_info 源头。
        if record.exc_info:
            try:
                import traceback

                text = "".join(traceback.format_exception(*record.exc_info))
                cleaned = self._SENSITIVE_URI.sub(
                    lambda match: f"{match.group(1)}{self._REDACTED}{match.group(2)}",
                    text,
                )
                cleaned = self._SENSITIVE_BEARER.sub(
                    lambda match: f"{match.group(1)}{self._REDACTED}", cleaned
                )
                cleaned = self._SENSITIVE_KV.sub(
                    lambda match: f"{match.group(1)}{self._REDACTED}", cleaned
                )
                if cleaned != text:
                    record.exc_info = (record.exc_info[0], record.exc_info[1], None)
                    record.exc_text = cleaned
            except Exception:
                pass
        elif record.exc_text:
            cleaned = self._SENSITIVE_URI.sub(
                lambda match: f"{match.group(1)}{self._REDACTED}{match.group(2)}",
                record.exc_text,
            )
            cleaned = self._SENSITIVE_BEARER.sub(
                lambda match: f"{match.group(1)}{self._REDACTED}", cleaned
            )
            cleaned = self._SENSITIVE_KV.sub(
                lambda match: f"{match.group(1)}{self._REDACTED}", cleaned
            )
            if cleaned != record.exc_text:
                record.exc_text = cleaned
        return True


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
