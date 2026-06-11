"""
Kafka 客户端封装
提供异步 Kafka 生产者和消费者的创建和管理
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Optional

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.kafka.exceptions import (
    ConnectionException,
    ConsumerException,
    ProducerException,
)
from backend.kafka.schemas import (
    DeadLetterMessage,
    EventMessage,
    ProgressMessage,
    ResultMessage,
    RetryMessage,
    StatusMessage,
    TaskMessage,
)

logger = get_logger(__name__)


class KafkaClient:
    """
    Kafka 客户端单例
    管理 Kafka 生产者和消费者的生命周期
    """

    _instance: Optional["KafkaClient"] = None
    _producer: AIOKafkaProducer | None = None
    _consumers: dict[str, AIOKafkaConsumer] = {}
    _lock: asyncio.Lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def start(self) -> None:
        """启动 Kafka 客户端"""
        async with self._lock:
            if self._producer is None:
                try:
                    self._producer = AIOKafkaProducer(
                        bootstrap_servers=settings.kafka_bootstrap_servers,
                        value_serializer=lambda v: (
                            v.encode("utf-8") if isinstance(v, str) else v
                        ),
                        key_serializer=lambda k: (
                            k.encode("utf-8") if isinstance(k, str) else k
                        ),
                        acks=settings.kafka_producer_acks,
                        enable_idempotence=settings.kafka_producer_enable_idempotence,
                    )
                    await self._producer.start()
                    logger.info(
                        f"Kafka producer started, connected to {settings.kafka_bootstrap_servers}"
                    )
                except KafkaError as e:
                    raise ConnectionException(f"Failed to start Kafka producer: {e}")

    async def stop(self) -> None:
        """停止 Kafka 客户端"""
        async with self._lock:
            if self._producer:
                try:
                    await self._producer.stop()
                    logger.info("Kafka producer stopped")
                except KafkaError as e:
                    logger.error(f"Error stopping Kafka producer: {e}")
                finally:
                    self._producer = None

            for group_id, consumer in list(self._consumers.items()):
                try:
                    await consumer.stop()
                    logger.info(f"Kafka consumer {group_id} stopped")
                except KafkaError as e:
                    logger.error(f"Error stopping Kafka consumer {group_id}: {e}")
                finally:
                    if group_id in self._consumers:
                        del self._consumers[group_id]

    @property
    def producer(self) -> AIOKafkaProducer:
        """获取 Kafka 生产者"""
        if self._producer is None:
            raise ProducerException("Kafka producer not started")
        return self._producer

    async def get_consumer(
        self,
        group_id: str,
        topics: list[str],
        auto_offset_reset: str = None,
        enable_auto_commit: bool = None,
    ) -> AIOKafkaConsumer:
        """
        获取或创建 Kafka 消费者

        Args:
            group_id: 消费者组 ID
            topics: 订阅主题列表
            auto_offset_reset: 起始位置
            enable_auto_commit: 是否自动提交 offset

        Returns:
            AIOKafkaConsumer 实例
        """
        if group_id in self._consumers:
            return self._consumers[group_id]

        auto_offset_reset = auto_offset_reset or settings.kafka_auto_offset_reset
        enable_auto_commit = (
            enable_auto_commit
            if enable_auto_commit is not None
            else settings.kafka_enable_auto_commit
        )

        consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            group_id=group_id,
            auto_offset_reset=auto_offset_reset,
            enable_auto_commit=enable_auto_commit,
            value_deserializer=lambda v: v.decode("utf-8"),
            key_deserializer=lambda k: k.decode("utf-8") if k else None,
            max_poll_records=settings.kafka_max_poll_records,
            session_timeout_ms=settings.kafka_session_timeout_ms,
            heartbeat_interval_ms=settings.kafka_heartbeat_interval_ms,
        )

        try:
            await consumer.start()
            self._consumers[group_id] = consumer
            logger.info(
                f"Kafka consumer {group_id} started, subscribed to topics: {topics}"
            )
        except KafkaError as e:
            raise ConsumerException(f"Failed to start Kafka consumer {group_id}: {e}")

        return consumer

    async def remove_consumer(self, group_id: str) -> None:
        """移除并停止 Kafka 消费者"""
        if group_id in self._consumers:
            try:
                await self._consumers[group_id].stop()
                logger.info(f"Kafka consumer {group_id} stopped")
            except KafkaError as e:
                logger.error(f"Error stopping Kafka consumer {group_id}: {e}")
            finally:
                del self._consumers[group_id]

    def get_consumer_group_id(self, group_name: str) -> str:
        """获取消费者组 ID"""
        prefix = settings.kafka_consumer_group_prefix
        return f"{prefix}{group_name}" if prefix else group_name


kafka_client = KafkaClient()


@asynccontextmanager
async def get_kafka_client() -> AsyncGenerator[KafkaClient, None]:
    """
    获取 Kafka 客户端的上下文管理器

    Usage:
        async with get_kafka_client() as client:
            await client.producer.send(...)
    """
    try:
        await kafka_client.start()
        yield kafka_client
    finally:
        await kafka_client.stop()


async def get_producer() -> AIOKafkaProducer:
    """获取 Kafka 生产者"""
    await kafka_client.start()
    return kafka_client.producer


async def get_consumer(
    group_id: str,
    topics: list[str],
) -> AIOKafkaConsumer:
    """获取 Kafka 消费者"""
    await kafka_client.start()
    return await kafka_client.get_consumer(group_id, topics)


class TaskProducer:
    """
    任务消息生产者
    封装任务相关的消息发送操作
    """

    def __init__(self, client: KafkaClient = None):
        self._client = client or kafka_client

    async def _ensure_started(self) -> None:
        """确保 Kafka 客户端已启动"""
        await self._client.start()

    async def submit_task(self, task: TaskMessage) -> None:
        """
        提交任务消息

        Args:
            task: 任务消息
        """
        await self._ensure_started()
        try:
            await self._client.producer.send_and_wait(
                topic=settings.kafka_task_topic,
                value=task.model_dump_json(),
                key=task.body.task_id,
            )
            logger.info(f"Task submitted: {task.body.task_id}")
        except KafkaError as e:
            logger.error(f"Failed to submit task {task.body.task_id}: {e}")
            raise

    async def submit_batch(self, tasks: list[TaskMessage]) -> None:
        """
        批量提交任务消息

        Args:
            tasks: 任务消息列表
        """
        if not tasks:
            return

        async with self._client.producer:
            for task in tasks:
                try:
                    await self._client.producer.send(
                        topic=settings.kafka_task_topic,
                        value=task.model_dump_json(),
                        key=task.body.task_id,
                    )
                except KafkaError as e:
                    logger.error(f"Failed to submit task {task.body.task_id}: {e}")

            logger.info(f"Batch submitted: {len(tasks)} tasks")

    async def retry_task(self, retry: RetryMessage) -> None:
        """
        发送任务重试消息

        Args:
            retry: 重试消息
        """
        try:
            await self._client.producer.send_and_wait(
                topic=settings.kafka_retry_topic,
                value=retry.model_dump_json(),
                key=retry.body.task_id,
            )
            logger.info(
                f"Task retry scheduled: {retry.body.task_id}, attempt: {retry.body.retry_attempt}"
            )
        except KafkaError as e:
            logger.error(f"Failed to schedule retry for task {retry.body.task_id}: {e}")
            raise

    async def send_to_dead_letter(self, message: DeadLetterMessage) -> None:
        """
        发送死信消息

        Args:
            message: 死信消息
        """
        try:
            await self._client.producer.send_and_wait(
                topic=settings.kafka_dead_letter_topic,
                value=message.model_dump_json(),
                key=message.original_message.get("body", {}).get("task_id", "unknown"),
            )
            logger.warning(f"Message sent to dead letter: {message.original_message}")
        except KafkaError as e:
            logger.error(f"Failed to send to dead letter: {e}")
            raise


class StatusProducer:
    """
    状态消息生产者
    封装状态更新相关的消息发送操作
    """

    def __init__(self, client: KafkaClient = None):
        self._client = client or kafka_client

    async def _ensure_started(self) -> None:
        """确保 Kafka 客户端已启动"""
        await self._client.start()

    async def send_status(self, status: StatusMessage) -> None:
        """
        发送状态更新消息

        Args:
            status: 状态消息
        """
        await self._ensure_started()
        try:
            await self._client.producer.send_and_wait(
                topic=settings.kafka_status_topic,
                value=status.model_dump_json(),
                key=status.body.task_id,
            )
            logger.debug(
                f"Status update sent: {status.body.task_id} -> {status.body.status}"
            )
        except KafkaError as e:
            logger.error(f"Failed to send status update: {e}")
            raise

    async def send_progress(self, progress: ProgressMessage) -> None:
        """
        发送进度更新消息

        Args:
            progress: 进度消息
        """
        try:
            await self._client.producer.send_and_wait(
                topic=settings.kafka_status_topic,
                value=progress.model_dump_json(),
                key=progress.body.task_id,
            )
            logger.debug(f"Progress update sent: {progress.body.task_id}")
        except KafkaError as e:
            logger.error(f"Failed to send progress update: {e}")
            raise


class ResultProducer:
    """
    结果消息生产者
    封装结果相关的消息发送操作
    """

    def __init__(self, client: KafkaClient = None):
        self._client = client or kafka_client

    async def _ensure_started(self) -> None:
        """确保 Kafka 客户端已启动"""
        await self._client.start()

    async def send_result(self, result: ResultMessage) -> None:
        """
        发送处理结果消息

        Args:
            result: 结果消息
        """
        await self._ensure_started()
        try:
            await self._client.producer.send_and_wait(
                topic=settings.kafka_result_topic,
                value=result.model_dump_json(),
                key=result.body.task_id,
            )
            logger.info(f"Result sent: {result.body.task_id} -> {result.body.status}")
        except KafkaError as e:
            logger.error(f"Failed to send result: {e}")
            raise


class EventProducer:
    """
    事件消息生产者
    封装事件相关的消息发送操作
    """

    def __init__(self, client: KafkaClient = None):
        self._client = client or kafka_client

    async def _ensure_started(self) -> None:
        """确保 Kafka 客户端已启动"""
        await self._client.start()

    async def send_event(self, event: EventMessage) -> None:
        """
        发送事件消息

        Args:
            event: 事件消息
        """
        await self._ensure_started()
        try:
            await self._client.producer.send_and_wait(
                topic=settings.kafka_event_topic,
                value=event.model_dump_json(),
                key=event.body.task_id,
            )
            logger.debug(f"Event sent: {event.body.event_type} -> {event.body.task_id}")
        except KafkaError as e:
            logger.error(f"Failed to send event: {e}")
            raise


def get_task_producer() -> TaskProducer:
    """获取任务生产者"""
    return TaskProducer(kafka_client)


def get_status_producer() -> StatusProducer:
    """获取状态生产者"""
    return StatusProducer(kafka_client)


def get_result_producer() -> ResultProducer:
    """获取结果生产者"""
    return ResultProducer(kafka_client)


def get_event_producer() -> EventProducer:
    """获取事件生产者"""
    return EventProducer(kafka_client)
