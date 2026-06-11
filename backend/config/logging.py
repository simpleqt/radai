"""
统一日志配置
"""

import logging
import os
import sys
from pathlib import Path

from backend.config.filters import (
    ErrorFilter,
    KafkaBusinessFilter,
    SensitiveDataFilter,
    UserContextFilter,
)
from backend.config.handlers import (
    ColoredConsoleHandler,
    KafkaLogHandler,
    ModuleFileHandler,
    RotatingFileHandlerWithCompression,
)
from backend.config.settings import settings


def get_log_formatter(format_type: str = "detailed") -> logging.Formatter:
    """
    获取日志格式化器

    Args:
        format_type: 格式类型，simple 或 detailed

    Returns:
        Formatter 实例
    """
    if format_type == "simple":
        fmt = "%(asctime)s | %(levelname)s | user=%(user_id)s | %(message)s"
    else:
        fmt = "%(asctime)s | %(levelname)s | %(name)s | user=%(user_id)s(%(username)s) | req=%(request_id)s | %(threadName)s | %(filename)s:%(lineno)d | %(message)s"

    return logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")


def _is_guest_api_service() -> bool:
    return os.getenv("GUEST_API_SERVICE", "").lower() in ("1", "true", "yes")


def setup_guest_logging():
    """访客 API 服务专用日志：仅写入 guest-api.log，不写入 app.log。"""
    os.environ["GUEST_API_SERVICE"] = "1"
    setup_logging()


def setup_logging():
    """
    配置日志系统

    创建日志目录，配置根日志记录器，添加各种处理器。
    当环境变量 GUEST_API_SERVICE=1 时，日志写入 guest-api.log。
    """
    guest_mode = _is_guest_api_service()

    # 创建日志目录
    log_dir = Path(settings.log_dir_path)
    log_dir.mkdir(parents=True, exist_ok=True)

    # 创建归档目录
    archive_dir = Path(settings.log_archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    # 创建临时目录
    temp_dir = Path(settings.log_temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 获取日志格式
    formatter = get_log_formatter(settings.log_format)

    # 配置根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    # 清除现有的处理器
    root_logger.handlers.clear()

    if guest_mode:
        app_log_path = log_dir / "guest-api.log"
        error_log_path = log_dir / "guest-api.error.log"
        service_name = "访客 API"
    else:
        app_log_path = Path(settings.log_file)
        error_log_path = log_dir / "app.error.log"
        service_name = "主应用"

    # 用户上下文过滤器（全局共享同一实例）
    user_ctx_filter = UserContextFilter()

    # 控制台处理器（访客服务由 nohup 重定向 stdout，避免与文件 handler 重复）
    if not guest_mode:
        console_handler = ColoredConsoleHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(settings.log_level)
        console_handler.addFilter(user_ctx_filter)
        root_logger.addHandler(console_handler)

    # 主日志文件处理器（支持轮转）
    app_handler = RotatingFileHandlerWithCompression(
        filename=str(app_log_path),
        maxBytes=settings.log_max_size,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
        compress=settings.log_compression,
    )
    app_handler.setFormatter(formatter)
    app_handler.setLevel(settings.log_level)
    app_handler.addFilter(user_ctx_filter)
    app_handler.addFilter(SensitiveDataFilter())
    root_logger.addHandler(app_handler)

    # 错误日志文件处理器
    error_handler = RotatingFileHandlerWithCompression(
        filename=str(error_log_path),
        maxBytes=settings.log_max_size,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
        compress=settings.log_compression,
    )
    error_handler.setFormatter(formatter)
    error_handler.setLevel(logging.ERROR)
    error_handler.addFilter(user_ctx_filter)
    error_handler.addFilter(ErrorFilter())
    root_logger.addHandler(error_handler)

    # 4. 定时轮转处理器（归档）
    # 注意：不能同时使用 RotatingFileHandler 和 TimedRotatingFileHandler 写入同一个文件，否则会导致日志重复
    # 如果需要按时间轮转，请注释掉上面的 RotatingFileHandler 配置
    # timed_handler = TimedRotatingFileHandlerWithArchive(
    #     filename=str(app_log_path),
    #     when="midnight",
    #     interval=1,
    #     backupCount=settings.log_backup_count,
    #     encoding="utf-8",
    #     archive_dir=str(archive_dir),
    #     compress=settings.log_compression,
    # )
    # timed_handler.setFormatter(formatter)
    # timed_handler.setLevel(settings.log_level)
    # root_logger.addHandler(timed_handler)

    # 配置第三方库的日志级别
    _configure_third_party_loggers()

    # Kafka 日志 Handler（通过 LOG_KAFKA_ENABLED 控制，默认关闭）
    if getattr(settings, "log_kafka_enabled", False):
        import os

        _service_name = os.getenv("LOG_SERVICE_NAME", settings.log_service_name)
        if guest_mode:
            _service_name = "guest-api"

        kafka_handler = KafkaLogHandler(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            topic=settings.log_kafka_topic,
            error_topic=settings.log_kafka_error_topic,
            service_name=_service_name,
            level=logging.getLevelName(settings.log_kafka_level.upper()),
        )
        kafka_handler.addFilter(user_ctx_filter)
        kafka_handler.addFilter(SensitiveDataFilter())
        kafka_handler.addFilter(KafkaBusinessFilter())
        root_logger.addHandler(kafka_handler)

    logging.info("%s日志系统初始化完成", service_name)
    logging.info("日志目录: %s", log_dir)
    logging.info("日志文件: %s", app_log_path)
    logging.info("日志级别: %s", settings.log_level)
    logging.info("日志格式: %s", settings.log_format)
    if getattr(settings, "log_kafka_enabled", False):
        logging.info(
            "Kafka 日志已启用: topic=%s, service=%s, level=%s",
            settings.log_kafka_topic,
            _service_name,
            settings.log_kafka_level,
        )


def _configure_third_party_loggers():
    """
    配置第三方库的日志级别，避免第三方库产生过多日志。

    kafka.conn / kafka.client：kafka-python 底层连接层在 Broker 不可达时会以
    指数退避不断重试并输出 ERROR/WARNING，而 KafkaLogHandler 已实现降级丢弃模式，
    底层重试噪声对业务无价值，设为 CRITICAL 彻底屏蔽。
    """
    third_party_loggers = {
        # ── 通用库 ──────────────────────────────────────────
        "httpx":        "WARNING",
        "httpcore":     "WARNING",
        "urllib3":      "WARNING",
        "sqlalchemy":   "WARNING",
        "pymysql":      "WARNING",
        "aiomysql":     "WARNING",
        "watchfiles":   "WARNING",
        "nebula3":      "WARNING",
        # ── Kafka ───────────────────────────────────────────
        # 保留 kafka 顶层 WARNING（业务层 kafka 事件仍可见）
        "kafka":        "WARNING",
        "aiokafka":     "WARNING",
        # 底层连接/客户端噪声：Broker 不可达时的 ERROR/WARNING 重试日志
        # KafkaLogHandler 已降级丢弃，这些连接重试对业务无价值
        "kafka.conn":   "CRITICAL",
        "kafka.client": "CRITICAL",
    }

    for logger_name, level in third_party_loggers.items():
        logging.getLogger(logger_name).setLevel(level)


def get_module_handler(logger_name: str) -> logging.Handler:
    """
    获取模块日志处理器

    Args:
        logger_name: 日志记录器名称

    Returns:
        Handler 实例
    """
    formatter = get_log_formatter(settings.log_format)

    handler = ModuleFileHandler(
        logger_name=logger_name,
        log_dir=settings.log_dir_path,
        mode="a",
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    handler.setLevel(settings.log_level)
    handler.addFilter(UserContextFilter())
    handler.addFilter(SensitiveDataFilter())

    return handler


def setup_module_logger(logger_name: str) -> logging.Logger:
    """
    设置模块日志记录器

    Args:
        logger_name: 日志记录器名称

    Returns:
        Logger 实例
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(settings.log_level)

    # 添加模块专用处理器
    module_handler = get_module_handler(logger_name)
    logger.addHandler(module_handler)

    # 防止日志传播到根日志记录器（避免重复）
    logger.propagate = True

    return logger

