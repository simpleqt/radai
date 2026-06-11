"""
日志记录器工厂
提供统一的日志记录器获取接口
"""

import logging


class LoggerContext:
    """日志上下文管理器"""

    def __init__(self, logger: logging.Logger, **context):
        self.logger = logger
        self.context = context
        self.old_adapter = None

    def __enter__(self):
        self.old_adapter = logging.LoggerAdapter
        logging.LoggerAdapter = type(
            "ContextAdapter",
            (logging.LoggerAdapter,),
            {
                "process": lambda self, msg, kwargs: super(type(self), self).process(
                    f"[{self.extra}] {msg}" if self.extra else msg, kwargs
                )
            },
        )
        return logging.LoggerAdapter(self.logger, self.context)

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        if self.old_adapter:
            logging.LoggerAdapter = self.old_adapter


class LoggerFactory:
    """
    日志记录器工厂

    负责创建和管理日志记录器
    """

    _loggers: dict = {}
    _initialized: bool = False

    @classmethod
    def initialize(cls):
        """初始化日志系统"""
        if cls._initialized:
            return

        from backend.config.logging import setup_logging

        setup_logging()
        cls._initialized = True

    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        """
        获取日志记录器

        Args:
            name: 日志记录器名称，通常使用 __name__

        Returns:
            Logger 实例
        """
        if not cls._initialized:
            cls.initialize()

        if name not in cls._loggers:
            logger = logging.getLogger(name)
            cls._loggers[name] = logger

        return cls._loggers[name]

    @classmethod
    def get_module_logger(cls, module_path: str) -> logging.Logger:
        """
        获取模块日志记录器

        Args:
            module_path: 模块路径，如 "backend.api"

        Returns:
            Logger 实例
        """
        return cls.get_logger(module_path)

    @classmethod
    def get_api_logger(cls) -> logging.Logger:
        """获取 API 模块日志记录器"""
        return cls.get_module_logger("backend.api")

    @classmethod
    def get_worker_logger(cls) -> logging.Logger:
        """获取 Worker 模块日志记录器"""
        return cls.get_module_logger("backend.worker")

    @classmethod
    def get_kafka_logger(cls) -> logging.Logger:
        """获取 Kafka 模块日志记录器"""
        return cls.get_module_logger("backend.kafka")

    @classmethod
    def get_workflow_logger(cls) -> logging.Logger:
        """获取工作流模块日志记录器"""
        return cls.get_module_logger("backend.workflows")

    @classmethod
    def get_db_logger(cls) -> logging.Logger:
        """获取数据库模块日志记录器"""
        return cls.get_module_logger("backend.db")

    @classmethod
    def get_storage_logger(cls) -> logging.Logger:
        """获取存储模块日志记录器"""
        return cls.get_module_logger("backend.storage")

    @classmethod
    def get_integration_logger(cls) -> logging.Logger:
        """获取集成模块日志记录器"""
        return cls.get_module_logger("backend.integrations")


def get_logger(name: str) -> logging.Logger:
    """
    获取日志记录器的便捷函数

    Args:
        name: 日志记录器名称

    Returns:
        Logger 实例
    """
    return LoggerFactory.get_logger(name)


def get_module_logger(module_path: str) -> logging.Logger:
    """
    获取模块日志记录器的便捷函数

    Args:
        module_path: 模块路径

    Returns:
        Logger 实例
    """
    return LoggerFactory.get_module_logger(module_path)
