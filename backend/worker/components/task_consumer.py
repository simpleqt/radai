import asyncio
from pathlib import Path
from typing import Any

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.kafka.client import kafka_client
from backend.kafka.consumer import (
    KafkaConsumerManager,
    RetryManager,
    TaskStatusUpdater,
    get_consumer_manager,
)
from backend.kafka.schemas import (
    MessageHeader,
    MessageType,
    TaskMessage,
    TaskStatusEnum,
)
from backend.worker.components.doc_processing import DocumentProcessingUseCase
from backend.worker.components.result_persister import ResultPersister
from backend.worker.components.result_publisher import ResultPublisher

logger = get_logger(__name__)


class TaskConsumer:
    """
    任务消费者
    负责从Kafka消费任务，控制并发，并协调处理流程
    """

    def __init__(
        self,
        doc_processor: DocumentProcessingUseCase,
        persister: ResultPersister,
        publisher: ResultPublisher,
    ):
        self._doc_processor = doc_processor
        self._persister = persister
        self._publisher = publisher

        self._consumer_manager: KafkaConsumerManager | None = None
        self._status_updater: TaskStatusUpdater | None = None
        self._retry_manager: RetryManager | None = None

        self._running: bool = False
        self._task_lock: asyncio.Lock = asyncio.Lock()
        self._processing_tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        """启动消费者"""
        if self._running:
            logger.warning("TaskConsumer is already running")
            return

        self._running = True
        self._status_updater = TaskStatusUpdater()
        self._retry_manager = RetryManager()
        self._consumer_manager = get_consumer_manager()

        await kafka_client.start()
        await self._start_kafka_consumer()

        logger.info("TaskConsumer started")

    async def stop(self) -> None:
        """停止消费者"""
        self._running = False

        # 取消所有正在进行的任务
        for task in list(self._processing_tasks.values()):
            task.cancel()

        if self._processing_tasks:
            await asyncio.gather(
                *self._processing_tasks.values(), return_exceptions=True
            )

        if self._consumer_manager:
            await self._consumer_manager.stop()

        await kafka_client.stop()
        logger.info("TaskConsumer stopped")

    async def _start_kafka_consumer(self) -> None:
        """启动Kafka消费循环"""

        async def handle_message(message_data: dict[str, Any], topic: str) -> None:
            if topic == settings.kafka_retry_topic:
                await self._handle_retry(message_data)
            else:
                await self._handle_task(message_data)

        await self._consumer_manager.start_consumer(
            group_id=settings.kafka_task_group_id,
            topics=[settings.kafka_task_topic, settings.kafka_retry_topic],
            message_handler=handle_message,
        )

    async def _handle_task(self, message_data: dict[str, Any]) -> None:
        """处理任务消息"""
        try:
            task_msg = TaskMessage(**message_data)
            task_id = task_msg.body.task_id

            async with self._task_lock:
                if task_id in self._processing_tasks:
                    logger.warning(f"Task {task_id} is already being processed")
                    return

                # 创建后台任务处理文档
                task = asyncio.create_task(
                    self._process_pipeline(task_msg),
                    name=f"process-{task_id}",
                )
                self._processing_tasks[task_id] = task
                task.add_done_callback(
                    lambda t, tid=task_id: self._processing_tasks.pop(tid, None)
                )

        except Exception as e:
            logger.error(f"Failed to handle task message: {e}")
            raise

    async def _handle_retry(self, message_data: dict[str, Any]) -> None:
        """处理重试消息"""
        try:
            from backend.kafka.schemas import RetryMessage

            retry_msg = RetryMessage(**message_data)
            task_id = retry_msg.body.task_id

            logger.info(
                f"Retrying task: {task_id}, attempt: {retry_msg.body.retry_attempt}"
            )

            if retry_msg.body.original_task:
                original_task_dict = retry_msg.body.original_task
                if isinstance(original_task_dict, dict):
                    await self._handle_task(original_task_dict)
                else:
                    logger.warning(
                        f"Invalid original_task type in retry message for {task_id}"
                    )
            else:
                logger.warning(f"No original task in retry message for {task_id}")

        except Exception as e:
            logger.error(f"Failed to handle retry message: {e}")
            raise

    async def _process_pipeline(self, task: TaskMessage) -> None:
        """执行完整的处理流水线"""
        task_id = task.body.task_id
        document_id = task.body.document_id
        user_id = task.header.user_id
        file_path = task.body.file_path
        file_type = task.body.file_type

        try:
            # 1. 更新状态：处理中
            await self._persister.update_task_status(
                task_id, TaskStatusEnum.PROCESSING.value
            )

            await self._status_updater.update_status(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                status=TaskStatusEnum.PROCESSING,
                stage="initializing",
                progress=0.05,
                message="正在初始化处理器",
            )

            if not Path(file_path).exists():
                raise FileNotFoundError(f"File not found: {file_path}")

            # 2. 执行文档处理用例
            await self._status_updater.update_status(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                status=TaskStatusEnum.PROCESSING,
                stage="document_processing",
                progress=0.1,
                message="正在处理文档",
            )

            output = await self._doc_processor.execute(task)

            # 3. 持久化结果
            await self._status_updater.update_status(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                status=TaskStatusEnum.PROCESSING,
                stage="saving_results",
                progress=0.95,
                message="正在保存结果",
            )

            await self._persister.save_result(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                file_type=file_type,
                output=output,
            )

            # 4. 发布成功消息
            await self._publisher.publish_success(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                output=output,
            )

            # 5. 更新最终状态
            await self._status_updater.update_status(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                status=TaskStatusEnum.COMPLETED,
                stage="completed",
                progress=1.0,
                message="处理完成",
            )

            logger.info(f"Task {task_id} completed successfully")

        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}")
            await self._handle_error(task, e)

    async def _handle_error(self, task: TaskMessage, error: Exception) -> None:
        """处理错误和重试"""
        task_id = task.body.task_id
        document_id = task.body.document_id
        user_id = task.header.user_id

        # 更新状态为失败
        await self._status_updater.update_status(
            task_id=task_id,
            document_id=document_id,
            user_id=user_id,
            status=TaskStatusEnum.FAILED,
            stage="error",
            progress=0,
            message=str(error),
        )

        # 构建错误任务消息用于重试
        error_task = TaskMessage(
            header=MessageHeader(
                message_type=MessageType.TASK_SUBMITTED,
                source="worker-error",
                user_id=user_id,
                correlation_id=task_id,
            ),
            body=task.body,
        )

        # 尝试重试
        if self._retry_manager and self._retry_manager.should_retry(task_id, error):
            await self._retry_manager.schedule_retry(error_task, error)
        else:
            logger.error(f"Task {task_id} exceeded max retries or non-retriable error")
