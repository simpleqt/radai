"""
任务处理Worker
从Kafka消费任务并执行文档处理
"""

import asyncio
import logging
import signal

from backend.config.dependency_versions import validate_runtime_dependencies
from backend.config.loggers import get_logger
from backend.db.connection import close_db, close_db_async
from backend.db.redis import close_redis_pool
from backend.worker.components.doc_processing import DocumentProcessingUseCase
from backend.worker.components.result_persister import ResultPersister
from backend.worker.components.result_publisher import ResultPublisher
from backend.worker.components.task_consumer import TaskConsumer

logger = get_logger(__name__)


async def run_worker():
    """运行Worker"""
    # 初始化组件
    doc_processor = DocumentProcessingUseCase()
    persister = ResultPersister()
    publisher = ResultPublisher()

    # 初始化消费者
    consumer = TaskConsumer(
        doc_processor=doc_processor,
        persister=persister,
        publisher=publisher,
    )

    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()

    def _request_shutdown(sig: signal.Signals) -> None:
        logger.info("Received shutdown signal: %s", sig.name)
        shutdown_requested.set()

    installed_signals: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass

    consumer_wait: asyncio.Task[None] | None = None
    shutdown_wait: asyncio.Task[bool] | None = None
    try:
        await consumer.start()
        consumer_wait = asyncio.create_task(consumer.wait(), name="task-worker-consumer")
        shutdown_wait = asyncio.create_task(
            shutdown_requested.wait(),
            name="task-worker-shutdown",
        )
        done, _ = await asyncio.wait(
            {consumer_wait, shutdown_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if consumer_wait in done:
            await consumer_wait
    except asyncio.CancelledError:
        logger.info("Worker task cancelled")
        raise
    except Exception as e:
        logger.error(f"Worker encountered unexpected error: {e}")
        raise
    finally:
        for task in (consumer_wait, shutdown_wait):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (consumer_wait, shutdown_wait) if task is not None),
            return_exceptions=True,
        )
        await consumer.stop()
        await close_db_async()
        await asyncio.to_thread(close_db)
        await close_redis_pool()
        for sig in installed_signals:
            loop.remove_signal_handler(sig)


def main():
    """主入口"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s:%(lineno)d - %(message)s",
    )
    report = validate_runtime_dependencies()
    logger.info("runtime dependency contract validated: %s", report.as_dict())

    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
