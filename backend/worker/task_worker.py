"""
任务处理Worker
从Kafka消费任务并执行文档处理
"""

import asyncio
import logging

from backend.config.loggers import get_logger
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

    try:
        await consumer.start()

        # 保持运行直到收到停止信号
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
    except Exception as e:
        logger.error(f"Worker encountered unexpected error: {e}")
    finally:
        await consumer.stop()


def main():
    """主入口"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s:%(lineno)d - %(message)s",
    )

    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
