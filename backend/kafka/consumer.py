"""
Kafka 消费者模块
处理来自各个主题的消息
"""

import asyncio
import json
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.errors import KafkaError

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.kafka.client import KafkaClient, TaskProducer, kafka_client
from backend.kafka.exceptions import (
    MessageConsumeException,
    NonRetriableException,
)
from backend.kafka.schemas import (
    DeadLetterMessage,
    EventData,
    EventMessage,
    EventMessageBody,
    EventType,
    MessageHeader,
    MessageType,
    ResultMessage,
    RetryMessage,
    RetryMessageBody,
    RetryReason,
    StatusMessage,
    StatusMessageBody,
    TaskMessage,
    TaskStatusEnum,
)

logger = get_logger(__name__)


class MessageDeferredException(MessageConsumeException):
    """Deprecated signal retained for compatibility with older handlers."""

    def __init__(self, delay_seconds: float):
        super().__init__(f"Message deferred for {delay_seconds:.3f}s")
        self.delay_seconds = max(0.0, float(delay_seconds))


class BaseMessageHandler(ABC):
    """消息处理器基类"""

    @abstractmethod
    async def handle(self, message: Any) -> bool:
        """
        处理消息

        Args:
            message: 消息内容

        Returns:
            处理是否成功
        """
        pass

    @abstractmethod
    def get_message_type(self) -> type:
        """获取处理的消息类型"""
        pass


class TaskHandler(BaseMessageHandler):
    """任务消息处理器"""

    def __init__(self, process_task_func: Callable = None):
        self._process_task = process_task_func

    def get_message_type(self) -> type:
        return TaskMessage

    async def handle(self, message: dict[str, Any]) -> bool:
        try:
            task_msg = TaskMessage(**message)
            logger.info(f"Processing task: {task_msg.body.task_id}")

            if self._process_task:
                await self._process_task(task_msg)
            else:
                await self._default_process_task(task_msg)

            return True
        except Exception as e:
            logger.error(f"Failed to process task message: {e}")
            raise MessageConsumeException(f"Task processing failed: {e}") from e

    async def _default_process_task(self, task: TaskMessage) -> None:
        """
        默认任务处理逻辑
        实际实现应该调用 LangGraph 工作流
        """
        logger.info(f"Default task processing for: {task.body.task_id}")
        logger.info(
            f"Document: {task.body.file_name}, Options: {task.body.processing_options}"
        )

        await asyncio.sleep(1)
        logger.info(f"Task {task.body.task_id} processing completed")


class StatusHandler(BaseMessageHandler):
    """状态消息处理器"""

    def __init__(self, update_status_func: Callable = None):
        self._update_status = update_status_func

    def get_message_type(self) -> type:
        return StatusMessage

    async def handle(self, message: dict[str, Any]) -> bool:
        try:
            status_msg = StatusMessage(**message)
            logger.info(
                f"Status update: {status_msg.body.task_id} -> {status_msg.body.status}"
            )

            if self._update_status:
                await self._update_status(status_msg)
            else:
                await self._default_update_status(status_msg)

            return True
        except Exception as e:
            logger.error(f"Failed to process status message: {e}")
            raise

    async def _default_update_status(self, status: StatusMessage) -> None:
        """默认状态更新逻辑"""
        logger.info(
            f"Updated status for task {status.body.task_id}: {status.body.status}"
        )


class ResultHandler(BaseMessageHandler):
    """结果消息处理器"""

    def __init__(self, handle_result_func: Callable = None):
        self._handle_result = handle_result_func

    def get_message_type(self) -> type:
        return ResultMessage

    async def handle(self, message: dict[str, Any]) -> bool:
        try:
            result_msg = ResultMessage(**message)
            logger.info(
                f"Result received: {result_msg.body.task_id} -> {result_msg.body.status}"
            )

            if self._handle_result:
                await self._handle_result(result_msg)
            else:
                await self._default_handle_result(result_msg)

            return True
        except Exception as e:
            logger.error(f"Failed to process result message: {e}")
            raise

    async def _default_handle_result(self, result: ResultMessage) -> None:
        """默认结果处理逻辑"""
        if result.body.status == TaskStatusEnum.COMPLETED:
            logger.info(f"Task {result.body.task_id} completed successfully")
            if result.body.result:
                logger.info(
                    f"Extracted {len(result.body.result.tables)} tables, "
                    f"{len(result.body.result.formulas)} formulas"
                )
        else:
            logger.warning(f"Task {result.body.task_id} failed: {result.body.error}")


class EventHandler(BaseMessageHandler):
    """事件消息处理器"""

    def __init__(self, handle_event_func: Callable = None):
        self._handle_event = handle_event_func

    def get_message_type(self) -> type:
        return EventMessage

    async def handle(self, message: dict[str, Any]) -> bool:
        try:
            event_msg = EventMessage(**message)
            logger.info(
                f"Event: {event_msg.body.event_type} for task {event_msg.body.task_id}"
            )

            if self._handle_event:
                await self._handle_event(event_msg)
            else:
                await self._default_handle_event(event_msg)

            return True
        except Exception as e:
            logger.error(f"Failed to process event message: {e}")
            raise

    async def _default_handle_event(self, event: EventMessage) -> None:
        """默认事件处理逻辑"""
        logger.info(f"Event {event.body.event_type}: {event.body.data.description}")


class RetryHandler(BaseMessageHandler):
    """重试消息处理器"""

    def __init__(self, process_retry_func: Callable = None):
        self._process_retry = process_retry_func

    def get_message_type(self) -> type:
        return RetryMessage

    async def handle(self, message: dict[str, Any]) -> bool:
        try:
            retry_msg = RetryMessage(**message)
            logger.info(
                f"Retry task: {retry_msg.body.task_id}, attempt: {retry_msg.body.retry_attempt}"
            )

            if self._process_retry:
                await self._process_retry(retry_msg)
            else:
                await self._default_process_retry(retry_msg)

            return True
        except Exception as e:
            logger.error(f"Failed to process retry message: {e}")
            raise

    async def _default_process_retry(self, retry: RetryMessage) -> None:
        """默认重试处理逻辑"""
        logger.info(f"Scheduling retry for task {retry.body.task_id}")


class KafkaConsumerManager:
    """
    Kafka 消费者管理器
    管理多个主题的消费者
    """

    def __init__(self, client: KafkaClient = None):
        self._client = client or kafka_client
        self._consumers: dict[str, AIOKafkaConsumer] = {}
        self._handlers: dict[str, list[BaseMessageHandler]] = {}
        self._running: bool = False
        self._tasks: list[asyncio.Task] = []
        self._partition_tasks: set[asyncio.Task] = set()

    def register_handler(
        self,
        topic: str,
        handler: BaseMessageHandler,
        message_type: MessageType = None,
    ) -> None:
        """
        注册消息处理器

        Args:
            topic: 主题名称
            handler: 消息处理器
            message_type: 消息类型过滤
        """
        _ = message_type
        if topic not in self._handlers:
            self._handlers[topic] = []
        self._handlers[topic].append(handler)
        logger.info(f"Registered handler for topic {topic}")

    async def start_consumer(
        self,
        group_id: str,
        topics: list[str],
        message_handler: Callable = None,
    ) -> AIOKafkaConsumer:
        """
        启动消费者

        Args:
            group_id: 消费者组 ID
            topics: 订阅主题列表
            message_handler: 通用消息处理器

        Returns:
            AIOKafkaConsumer 实例
        """
        self._running = True
        # This manager owns offset commits. Auto commit would make it possible to
        # acknowledge a record while its handler is still running or has failed.
        consumer = await self._client.get_consumer(
            group_id,
            topics,
            enable_auto_commit=False,
        )
        self._consumers[group_id] = consumer

        task = asyncio.create_task(
            self._consume_messages(consumer, message_handler),
            name=f"consumer-{group_id}",
        )
        self._tasks.append(task)

        logger.info(f"Consumer {group_id} started for topics: {topics}")
        return consumer

    async def _send_poison_message_to_dead_letter(
        self,
        *,
        msg: Any,
        raw_value: Any,
        error: Exception,
    ) -> None:
        """Persist an unreadable record before allowing its offset to advance."""
        now = datetime.now().isoformat()
        original = (
            raw_value
            if isinstance(raw_value, dict)
            else {"raw_value": str(raw_value)}
        )
        await TaskProducer(self._client).send_to_dead_letter(
            DeadLetterMessage(
                original_message=original,
                error_count=1,
                last_error=f"{type(error).__name__}: {error}",
                first_failure_at=now,
                last_failure_at=now,
                topic=msg.topic,
                partition=msg.partition,
                offset=msg.offset,
            )
        )

    async def _process_record(
        self,
        consumer: AIOKafkaConsumer,
        msg: Any,
        message_handler: Callable | None,
    ) -> bool:
        """Process one record and commit only after its handler succeeds."""
        raw_value = msg.value
        try:
            value = raw_value.decode("utf-8") if isinstance(raw_value, bytes) else raw_value
            message_data = value if isinstance(value, dict) else json.loads(value)
            topic = msg.topic

            logger.debug(
                "Received message from %s: partition=%s, offset=%s",
                topic,
                msg.partition,
                msg.offset,
            )

            if message_handler:
                await message_handler(message_data, topic)
            elif topic in self._handlers:
                for handler in self._handlers[topic]:
                    handled = await handler.handle(message_data)
                    if handled is False:
                        raise MessageConsumeException(
                            f"Handler {type(handler).__name__} rejected message"
                        )
            else:
                logger.warning("No handler for topic %s", topic)

            partition = TopicPartition(topic, msg.partition)
            await consumer.commit({partition: msg.offset + 1})
            logger.debug(
                "Committed offset %s for %s partition %s",
                msg.offset + 1,
                topic,
                msg.partition,
            )
            return True
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.exception(
                "Poison message at %s[%s] offset %s; moving to DLQ",
                msg.topic,
                msg.partition,
                msg.offset,
            )
            await self._send_poison_message_to_dead_letter(
                msg=msg,
                raw_value=raw_value,
                error=exc,
            )
            partition = TopicPartition(msg.topic, msg.partition)
            await consumer.commit({partition: msg.offset + 1})
            return True

    async def _process_partition_batch(
        self,
        consumer: AIOKafkaConsumer,
        records: list[Any],
        message_handler: Callable | None,
    ) -> None:
        for msg in records:
            if not self._running:
                return
            should_continue = await self._process_record(
                consumer, msg, message_handler
            )
            if not should_continue:
                return

    async def _consume_messages(
        self,
        consumer: AIOKafkaConsumer,
        message_handler: Callable = None,
    ) -> None:
        """
        消费消息

        Args:
            consumer: Kafka 消费者
            message_handler: 通用消息处理器
        """
        try:
            getmany = getattr(consumer, "getmany", None)
            if not callable(getmany):
                # Compatibility path for simple consumers and unit-test fakes.
                async for msg in consumer:
                    if not self._running:
                        break
                    await self._process_partition_batch(
                        consumer, [msg], message_handler
                    )
                return

            while self._running:
                batches = await getmany(
                    timeout_ms=1000,
                    max_records=settings.kafka_max_poll_records,
                )
                if not batches:
                    continue
                # Kafka ordering is retained inside each partition while all
                # assigned partitions make progress concurrently.
                partition_tasks = [
                    asyncio.create_task(
                        self._process_partition_batch(
                            consumer,
                            list(records),
                            message_handler,
                        ),
                        name=f"consume-{partition.topic}-{partition.partition}",
                    )
                    for partition, records in batches.items()
                    if records
                ]
                if partition_tasks:
                    self._partition_tasks.update(partition_tasks)
                    try:
                        await asyncio.gather(*partition_tasks)
                    finally:
                        for task in partition_tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(
                            *partition_tasks,
                            return_exceptions=True,
                        )
                        self._partition_tasks.difference_update(partition_tasks)

        except asyncio.CancelledError:
            logger.info("Consumer task cancelled")
            raise
        except KafkaError as e:
            logger.error(f"Kafka error in consumer: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in consumer: {e}")
            raise

    async def wait(self) -> None:
        """Wait for consumers and surface the first unexpected termination."""
        if not self._tasks:
            raise RuntimeError("No active Kafka consumer tasks")

        done, _ = await asyncio.wait(
            self._tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        completed = next(iter(done))
        if completed.cancelled():
            if self._running:
                raise RuntimeError(
                    f"Kafka consumer task {completed.get_name()} was cancelled"
                )
            return

        error = completed.exception()
        if error is not None:
            raise RuntimeError(
                f"Kafka consumer task {completed.get_name()} failed"
            ) from error
        if self._running:
            raise RuntimeError(
                f"Kafka consumer task {completed.get_name()} exited unexpectedly"
            )

    def stop_accepting(self) -> None:
        """Stop fetching new records while allowing the active batch to drain."""
        self._running = False
        for consumer in self._consumers.values():
            try:
                partitions = consumer.assignment()
                if partitions:
                    consumer.pause(*partitions)
            except Exception:
                logger.exception("Failed to pause Kafka consumer during shutdown")

    async def start_all(self) -> None:
        """启动所有注册的消费者"""
        self._running = True

        task_consumer_group = self._client.get_consumer_group_id(
            settings.kafka_task_group_id
        )
        await self.start_consumer(
            group_id=task_consumer_group,
            topics=[settings.kafka_task_topic, settings.kafka_retry_topic],
        )

        status_consumer_group = self._client.get_consumer_group_id(
            settings.kafka_status_group_id
        )
        await self.start_consumer(
            group_id=status_consumer_group,
            topics=[settings.kafka_status_topic],
        )

        result_consumer_group = self._client.get_consumer_group_id(
            settings.kafka_result_group_id
        )
        await self.start_consumer(
            group_id=result_consumer_group,
            topics=[settings.kafka_result_topic],
        )

        event_consumer_group = self._client.get_consumer_group_id(
            settings.kafka_event_group_id
        )
        await self.start_consumer(
            group_id=event_consumer_group,
            topics=[settings.kafka_event_topic],
        )

        dead_letter_consumer_group = self._client.get_consumer_group_id(
            settings.kafka_dead_letter_group_id
        )
        await self.start_consumer(
            group_id=dead_letter_consumer_group,
            topics=[settings.kafka_dead_letter_topic],
        )

        logger.info("All Kafka consumers started")

    async def stop(self) -> None:
        """停止所有消费者"""
        self.stop_accepting()

        for task in self._tasks:
            task.cancel()
        for task in self._partition_tasks:
            task.cancel()

        active_tasks = [*self._tasks, *self._partition_tasks]
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

        for group_id in list(self._consumers.keys()):
            await self._client.remove_consumer(group_id)

        self._tasks.clear()
        self._partition_tasks.clear()
        self._consumers.clear()

        logger.info("All Kafka consumers stopped")


class TaskStatusUpdater:
    """
    任务状态更新器
    通过 Kafka 发送状态更新
    """

    def __init__(self):
        from backend.kafka.client import get_status_producer

        self._producer = get_status_producer()
        self._pending_status_tasks: set[asyncio.Task[None]] = set()

    def _status_task_done(self, task: asyncio.Task[None]) -> None:
        self._pending_status_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug("Background status update was cancelled")
        except Exception:
            logger.exception("Background status update failed")

    async def update_status(
        self,
        task_id: str,
        document_id: str,
        user_id: str,
        status: TaskStatusEnum,
        stage: str = None,
        progress: float = 0.0,
        message: str = "",
        source: str = "worker",
    ) -> None:
        await self.send_status(
            task_id=task_id,
            document_id=document_id,
            user_id=user_id,
            status=status,
            stage=stage,
            progress=progress,
            message=message,
            source=source,
        )

    async def send_status(
        self,
        task_id: str,
        document_id: str,
        user_id: str,
        status: TaskStatusEnum,
        stage: str = None,
        progress: float = 0.0,
        message: str = "",
        source: str = "worker",
    ) -> None:
        """
        发送状态更新

        Args:
            task_id: 任务 ID
            document_id: 文档 ID
            user_id: 用户 ID
            status: 状态
            stage: 处理阶段
            progress: 进度 (0-1)
            message: 状态消息
            source: 消息来源
        """
        from backend.kafka.schemas import MessageHeader, StatusMessage

        header = MessageHeader(
            message_type=MessageType.STATUS_UPDATE,
            source=source,
            user_id=user_id,
            correlation_id=task_id,
        )

        status_msg = StatusMessage(
            header=header,
            body=StatusMessageBody(
                task_id=task_id,
                document_id=document_id,
                status=status,
                stage=stage,
                progress=progress,
                message=message,
            ),
        )

        await self._producer.send_status(status_msg)

    def update_status_sync(
        self,
        task_id: str,
        document_id: str,
        user_id: str,
        status: TaskStatusEnum,
        stage: str = None,
        progress: float = 0.0,
        message: str = "",
        source: str = "worker",
    ) -> None:
        """
        同步发送状态更新（在回调中使用）

        Args:
            task_id: 任务 ID
            document_id: 文档 ID
            user_id: 用户 ID
            status: 状态
            stage: 处理阶段
            progress: 进度 (0-1)
            message: 状态消息
            source: 消息来源
        """
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self._send_status_async(
                    task_id,
                    document_id,
                    user_id,
                    status,
                    stage,
                    progress,
                    message,
                    source,
                )
            )
            self._pending_status_tasks.add(task)
            task.add_done_callback(self._status_task_done)
        except RuntimeError:
            logger.debug(
                "update_status_sync skipped: no running event loop",
                exc_info=True,
            )

    async def _send_status_async(
        self,
        task_id: str,
        document_id: str,
        user_id: str,
        status: TaskStatusEnum,
        stage: str = None,
        progress: float = 0.0,
        message: str = "",
        source: str = "worker",
    ) -> None:
        """异步发送状态更新"""
        header = MessageHeader(
            message_type=MessageType.STATUS_UPDATE,
            source=source,
            user_id=user_id,
            correlation_id=task_id,
        )

        status_msg = StatusMessage(
            header=header,
            body=StatusMessageBody(
                task_id=task_id,
                document_id=document_id,
                status=status,
                stage=stage,
                progress=progress,
                message=message,
            ),
        )

        await self._producer.send_status(status_msg)

    async def send_event(
        self,
        task_id: str,
        document_id: str,
        user_id: str,
        event_type: EventType,
        description: str,
        extra_info: dict[str, Any] = None,
        source: str = "worker",
    ) -> None:
        """
        发送事件

        Args:
            task_id: 任务 ID
            document_id: 文档 ID
            user_id: 用户 ID
            event_type: 事件类型
            description: 事件描述
            extra_info: 额外信息
            source: 消息来源
        """
        header = MessageHeader(
            message_type=MessageType.EVENT,
            source=source,
            user_id=user_id,
            correlation_id=task_id,
        )

        event_msg = EventMessage(
            header=header,
            body=EventMessageBody(
                event_type=event_type,
                task_id=task_id,
                document_id=document_id,
                data=EventData(
                    description=description,
                    extra_info=extra_info or {},
                ),
            ),
        )

        await self._producer.send_event(event_msg)


class RetryManager:
    """
    重试管理器
    管理任务重试逻辑
    """

    RETRY_DELAYS = [60, 300, 1800, 7200]
    MAX_RETRIES = 4
    RETRY_COUNTER_TTL_SECONDS = 24 * 60 * 60
    MAX_TRACKED_RETRY_COUNTERS = 10_000

    def __init__(
        self,
        *,
        retry_scheduler: Callable[[RetryMessage], Awaitable[None]] | None = None,
    ):
        from backend.kafka.client import get_task_producer

        self._producer = get_task_producer()
        self._retry_counts: dict[str, int] = {}
        self._retry_touched_at: dict[str, float] = {}
        self._retry_scheduler = retry_scheduler

    def _prune_retry_counts(self) -> None:
        cutoff = time.monotonic() - self.RETRY_COUNTER_TTL_SECONDS
        expired = [
            task_id
            for task_id, touched_at in self._retry_touched_at.items()
            if touched_at < cutoff
        ]
        for task_id in expired:
            self._retry_counts.pop(task_id, None)
            self._retry_touched_at.pop(task_id, None)
        overflow = len(self._retry_counts) - self.MAX_TRACKED_RETRY_COUNTERS
        if overflow > 0:
            oldest = sorted(
                self._retry_touched_at,
                key=self._retry_touched_at.__getitem__,
            )[:overflow]
            for task_id in oldest:
                self._retry_counts.pop(task_id, None)
                self._retry_touched_at.pop(task_id, None)

    def should_retry(self, task_id: str, error: Exception) -> bool:
        """
        判断是否应该重试

        Args:
            task_id: 任务 ID
            error: 错误信息

        Returns:
            是否应该重试
        """
        if isinstance(error, NonRetriableException):
            return False

        self._prune_retry_counts()
        retry_count = self._retry_counts.get(task_id, 0)
        return retry_count < self.MAX_RETRIES

    def get_retry_delay(self, retry_count: int) -> int:
        """
        获取重试延迟时间

        Args:
            retry_count: 重试次数

        Returns:
            延迟时间（秒）
        """
        attempt_index = max(0, retry_count - 1)
        if attempt_index < len(self.RETRY_DELAYS):
            return self.RETRY_DELAYS[attempt_index]
        return self.RETRY_DELAYS[-1]

    def get_jittered_retry_delay(self, retry_count: int) -> float:
        """Broaden jitter after the first retry to avoid synchronized storms."""
        base_delay = self.get_retry_delay(retry_count)
        minimum_ratio = 0.93 if retry_count <= 1 else 0.5
        return random.uniform(base_delay * minimum_ratio, float(base_delay))

    def _current_retry_count(self, task: TaskMessage) -> int:
        self._prune_retry_counts()
        task_id = task.body.task_id
        return max(
            self._retry_counts.get(task_id, 0),
            int(task.body.retry_count or 0),
        )

    def mark_succeeded(self, task_id: str) -> None:
        """Release the in-process counter after a terminal success."""
        self._retry_counts.pop(task_id, None)
        self._retry_touched_at.pop(task_id, None)

    async def handle_failure(
        self,
        task: TaskMessage,
        error: Exception,
        reason: str = "transient_failure",
    ) -> bool:
        """Schedule the next retry or durably route an exhausted task to DLQ."""
        task_id = task.body.task_id
        retry_count = self._current_retry_count(task)
        max_retries = min(
            self.MAX_RETRIES,
            max(0, int(task.body.max_retries)),
        )

        if isinstance(error, NonRetriableException) or retry_count >= max_retries:
            failure_count = retry_count + 1
            logger.warning(
                "Task %s is non-retriable or exhausted %s retries; sending to dead letter",
                task_id,
                max_retries,
            )
            await self._send_to_dead_letter(
                task,
                error,
                error_count=failure_count,
            )
            return False

        await self.schedule_retry(task, error, reason=reason)
        return True

    async def schedule_retry(
        self,
        task: TaskMessage,
        error: Exception,
        reason: str = "transient_failure",
    ) -> None:
        """
        安排重试

        Args:
            task: 原始任务消息
            error: 错误信息
            reason: 重试原因
        """
        task_id = task.body.task_id
        current_retry_count = self._current_retry_count(task)
        retry_count = current_retry_count + 1

        max_retries = min(
            self.MAX_RETRIES,
            max(0, int(task.body.max_retries)),
        )
        if retry_count > max_retries:
            logger.warning(
                f"Task {task_id} exceeded max retries, sending to dead letter"
            )
            await self._send_to_dead_letter(
                task,
                error,
                error_count=retry_count,
            )
            return

        delay = self.get_jittered_retry_delay(retry_count)
        scheduled_at = datetime.fromtimestamp(
            datetime.now().timestamp() + delay
        ).isoformat()

        header = MessageHeader(
            message_type=MessageType.TASK_RETRY,
            source="retry-manager",
            user_id=task.header.user_id,
            correlation_id=task_id,
        )

        original_task = task.model_dump()
        original_task["body"]["retry_count"] = retry_count

        retry_msg = RetryMessage(
            header=header,
            body=RetryMessageBody(
                task_id=task_id,
                document_id=task.body.document_id,
                retry_attempt=retry_count,
                previous_error=str(error),
                reason=RetryReason(reason),
                scheduled_at=scheduled_at,
                original_task=original_task,
            ),
        )

        if self._retry_scheduler is not None:
            await self._retry_scheduler(retry_msg)
        else:
            await self._producer.retry_task(retry_msg)
        # Advance the in-memory counter only after the retry record is durable.
        # The same attempt is also embedded in original_task for process restarts.
        self._retry_counts[task_id] = retry_count
        self._retry_touched_at[task_id] = time.monotonic()
        self._prune_retry_counts()
        logger.info(
            f"Scheduled retry for task {task_id}, attempt {retry_count}, delay {delay}s"
        )

    async def _send_to_dead_letter(
        self,
        task: TaskMessage,
        error: Exception,
        *,
        error_count: int | None = None,
    ) -> None:
        """
        发送死信

        Args:
            task: 原始任务消息
            error: 错误信息
        """
        from backend.kafka.schemas import DeadLetterMessage

        dead_letter = DeadLetterMessage(
            original_message=task.model_dump(),
            error_count=(
                error_count
                if error_count is not None
                else self._retry_counts.get(task.body.task_id, 0)
            ),
            last_error=str(error),
            first_failure_at=datetime.now().isoformat(),
            last_failure_at=datetime.now().isoformat(),
            topic=settings.kafka_task_topic,
            partition=0,
            offset=0,
        )

        await self._producer.send_to_dead_letter(dead_letter)
        self._retry_counts.pop(task.body.task_id, None)
        self._retry_touched_at.pop(task.body.task_id, None)
        logger.warning(f"Task {task.body.task_id} sent to dead letter")


def get_consumer_manager() -> KafkaConsumerManager:
    """获取消费者管理器"""
    return KafkaConsumerManager()


def get_status_updater() -> TaskStatusUpdater:
    """获取状态更新器"""
    return TaskStatusUpdater()


def get_retry_manager() -> RetryManager:
    """获取重试管理器"""
    return RetryManager()
