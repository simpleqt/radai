import asyncio
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_, select

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db import async_session_context
from backend.db.models import Task, TaskStatus
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
from backend.worker.components.result_persister import (
    RESULT_OUTBOX_KEY,
    RESULT_OUTBOX_KIND,
    ResultPersister,
    TaskCompletionRejected,
)
from backend.worker.components.result_publisher import ResultPublisher

logger = get_logger(__name__)

_RESULT_OUTBOX_SCAN_MIN_PAGE_SIZE = 100
_RESULT_OUTBOX_SCAN_MAX_PAGE_SIZE = 1000
_RESULT_OUTBOX_SCAN_MAX_ROWS = 1000


class ResultPublishPending(RuntimeError):
    """A committed DB result still needs its durable Kafka event published."""


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
        *,
        result_outbox_poll_interval_seconds: float = 2.0,
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
        self._processing_idle = asyncio.Event()
        self._processing_idle.set()
        self._result_outbox_poll_interval_seconds = max(
            0.1,
            float(result_outbox_poll_interval_seconds),
        )
        self._result_outbox_stop_event = asyncio.Event()
        self._result_outbox_task: asyncio.Task[None] | None = None
        self._result_outbox_scan_cursor: tuple[datetime, str] | None = None

    async def start(self) -> None:
        """启动消费者"""
        if self._running:
            logger.warning("TaskConsumer is already running")
            return

        self._running = True
        self._status_updater = TaskStatusUpdater()
        self._retry_manager = RetryManager(
            retry_scheduler=self._persister.schedule_retry_dispatch,
        )
        self._consumer_manager = get_consumer_manager()
        task_dispatch_outbox = None

        try:
            from backend.services.task_dispatch_outbox import task_dispatch_outbox

            await task_dispatch_outbox.start()
            await kafka_client.start()
            await self._start_kafka_consumer()
            self._start_result_outbox_reconciler()
        except Exception:
            self._running = False
            await self._stop_result_outbox_reconciler()
            if task_dispatch_outbox is not None:
                await task_dispatch_outbox.stop()
            await kafka_client.stop()
            raise

        logger.info("TaskConsumer started")

    async def wait(self) -> None:
        """Wait until the Kafka loop stops and propagate unexpected failures."""
        if not self._running or self._consumer_manager is None:
            raise RuntimeError("TaskConsumer is not running")
        await self._consumer_manager.wait()

    async def stop(self) -> None:
        """停止消费者"""
        self._running = False
        if self._consumer_manager:
            self._consumer_manager.stop_accepting()
        await self._stop_result_outbox_reconciler()
        from backend.services.task_dispatch_outbox import task_dispatch_outbox

        await task_dispatch_outbox.stop()

        drain_timeout = max(
            0.0,
            float(os.getenv("WORKER_SHUTDOWN_TIMEOUT_SECONDS", "30") or "30"),
        )
        if self._processing_tasks:
            try:
                await asyncio.wait_for(
                    self._processing_idle.wait(),
                    timeout=drain_timeout,
                )
            except TimeoutError:
                remaining = list(self._processing_tasks.values())
                logger.warning(
                    "Worker drain timed out after %.1fs; cancelling %d task(s)",
                    drain_timeout,
                    len(remaining),
                )
                for task in remaining:
                    task.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)

        if self._consumer_manager:
            await self._consumer_manager.stop()

        await kafka_client.stop()
        logger.info("TaskConsumer stopped")

    def _start_result_outbox_reconciler(self) -> None:
        """Start the independent recovery path for committed result events."""
        if self._result_outbox_task is not None and not self._result_outbox_task.done():
            return
        self._result_outbox_stop_event = asyncio.Event()
        self._result_outbox_task = asyncio.create_task(
            self._run_result_outbox_reconciler(),
            name="result-outbox-reconciler",
        )

    async def _stop_result_outbox_reconciler(self) -> None:
        task = self._result_outbox_task
        if task is None:
            return
        self._result_outbox_stop_event.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._result_outbox_task = None

    async def _run_result_outbox_reconciler(self) -> None:
        """Continuously recover durable events even if their source offset was acked."""
        while self._running and not self._result_outbox_stop_event.is_set():
            try:
                await self._reconcile_pending_results()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient database failure must not permanently stop the
                # only recovery path for an already committed result event.
                logger.exception("Result outbox reconciliation scan failed")

            timeout = self._result_outbox_poll_interval_seconds * random.uniform(
                0.9,
                1.1,
            )
            try:
                await asyncio.wait_for(
                    self._result_outbox_stop_event.wait(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:  # noqa: UP041 -- Python 3.10 compatibility
                pass

    async def _reconcile_pending_results(self, *, limit: int = 100) -> int:
        """Publish up to ``limit`` valid result events found by the DB scanner."""
        candidates = await self._list_pending_result_candidates(limit=limit)
        published = 0
        for task_id, document_id, user_id, attempt_version in candidates:
            try:
                if await self._publish_pending_result(
                    task_id=task_id,
                    document_id=document_id,
                    user_id=user_id,
                    attempt_version=attempt_version,
                ):
                    published += 1
            except asyncio.CancelledError:
                raise
            except ResultPublishPending as exc:
                # Keep the event pending and continue so one Kafka failure or
                # malformed row cannot starve unrelated completed tasks.
                logger.warning("Result outbox event remains pending: %s", exc)
            except Exception:
                logger.exception(
                    "Unexpected result outbox reconciliation failure: task=%s",
                    task_id,
                )
        return published

    async def _list_pending_result_candidates(
        self,
        *,
        limit: int = 100,
    ) -> list[tuple[str, str, str, int]]:
        """List valid pending events with bounded, starvation-free keyset scans."""
        dispatch_limit = max(1, int(limit))
        page_size = min(
            _RESULT_OUTBOX_SCAN_MAX_PAGE_SIZE,
            max(_RESULT_OUTBOX_SCAN_MIN_PAGE_SIZE, dispatch_limit),
        )
        async with async_session_context() as session:
            filters = (
                Task.status == TaskStatus.COMPLETED.value,
                Task.outbox_kind == RESULT_OUTBOX_KIND,
                Task.updated_at.is_not(None),
            )
            candidates: list[tuple[str, str, str, int]] = []
            scanned = 0
            cursor = self._result_outbox_scan_cursor

            while (
                scanned < _RESULT_OUTBOX_SCAN_MAX_ROWS
                and len(candidates) < dispatch_limit
            ):
                query = select(
                    Task,
                    Task.updated_at,
                ).where(*filters)
                if cursor is not None:
                    cursor_updated_at, cursor_task_id = cursor
                    query = query.where(
                        or_(
                            Task.updated_at > cursor_updated_at,
                            and_(
                                Task.updated_at == cursor_updated_at,
                                Task.task_id > cursor_task_id,
                            ),
                        )
                    )
                rows = (
                    await asyncio.wait_for(
                        session.execute(
                            query.order_by(
                                Task.updated_at.asc(),
                                Task.task_id.asc(),
                            ).limit(
                                min(
                                    page_size,
                                    _RESULT_OUTBOX_SCAN_MAX_ROWS - scanned,
                                )
                            )
                        ),
                        timeout=30.0,
                    )
                ).all()
                if not rows:
                    # Wrap on the next poll. Wrapping inside this call could
                    # enqueue the same small set of pending rows repeatedly.
                    self._result_outbox_scan_cursor = None
                    break

                scanned += len(rows)
                for task, task_updated_at in rows:
                    # Advance only through rows actually inspected. Advancing
                    # to the fetched page boundary would skip the unvisited
                    # tail whenever this page fills the dispatch limit.
                    cursor = (task_updated_at, task.task_id)
                    self._result_outbox_scan_cursor = cursor
                    if not isinstance(task.result, dict):
                        continue
                    event = task.result.get(RESULT_OUTBOX_KEY)
                    if not isinstance(event, dict):
                        continue
                    try:
                        event_attempt = int(event.get("attempt", -1))
                        task_attempt = int(task.retry_count or 0)
                    except (TypeError, ValueError):
                        continue
                    if (
                        event.get("state") != "pending"
                        or event_attempt != task_attempt
                        or not event.get("event_id")
                        or not isinstance(event.get("output"), dict)
                        or not task.doc_id
                        or not task.user_id
                    ):
                        continue
                    candidates.append(
                        (
                            str(task.task_id),
                            str(task.doc_id),
                            str(task.user_id),
                            task_attempt,
                        )
                    )
                    if len(candidates) >= dispatch_limit:
                        break

        return candidates

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

                current_task = asyncio.current_task()
                if current_task is not None:
                    self._processing_tasks[task_id] = current_task
                    self._processing_idle.clear()

            try:
                await self._process_pipeline(task_msg)
            finally:
                async with self._task_lock:
                    self._processing_tasks.pop(task_id, None)
                    if not self._processing_tasks:
                        self._processing_idle.set()

        except Exception:
            logger.exception("Failed to handle task message")
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
            scheduled_at = getattr(retry_msg.body, "scheduled_at", None)
            if scheduled_at:
                try:
                    run_at = datetime.fromisoformat(str(scheduled_at))
                    now = (
                        datetime.now(tz=run_at.tzinfo)
                        if run_at.tzinfo is not None
                        else datetime.now()
                    )
                    delay = (run_at - now).total_seconds()
                    if delay > 0:
                        # Migrate legacy retry-topic records into the durable
                        # scheduler before this source offset is committed.
                        await self._persister.schedule_retry_dispatch(retry_msg)
                        return
                except ValueError:
                    logger.warning(
                        "Invalid retry scheduled_at for task %s: %s",
                        task_id,
                        scheduled_at,
                    )

            if retry_msg.body.original_task:
                retry_attempt = int(retry_msg.body.retry_attempt)
                retry_prepared = await self._persister.prepare_retry(
                    task_id,
                    retry_attempt,
                )
                if not retry_prepared:
                    logger.info(
                        "Ignoring stale or terminal retry %s for task %s",
                        retry_attempt,
                        task_id,
                    )
                    return

                original_task_dict = retry_msg.body.original_task
                if isinstance(original_task_dict, dict):
                    body = dict(original_task_dict.get("body") or {})
                    body["retry_count"] = max(
                        int(body.get("retry_count") or 0),
                        retry_attempt,
                    )
                    original_task_dict = {
                        **original_task_dict,
                        "body": body,
                    }
                    await self._handle_task(original_task_dict)
                else:
                    raise ValueError(
                        f"Invalid original_task type in retry message for {task_id}"
                    )
            else:
                raise ValueError(f"No original task in retry message for {task_id}")

        except Exception:
            logger.exception("Failed to handle retry message")
            raise

    async def _process_pipeline(self, task: TaskMessage) -> None:
        """执行完整的处理流水线"""
        task_id = task.body.task_id
        document_id = task.body.document_id
        user_id = task.header.user_id
        file_path = task.body.file_path
        file_type = task.body.file_type
        attempt_version = int(task.body.retry_count or 0)
        processing_claimed = False

        try:
            # A previous delivery may have committed the DB transaction but
            # crashed before publishing/marking its result event. Drain that
            # embedded outbox before trying to claim processing again.
            if await self._publish_pending_result(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                attempt_version=attempt_version,
            ):
                logger.info("Recovered pending result event for task %s", task_id)
                return

            if await self._is_cancelled(task_id, attempt_version):
                # The terminal state and its result event are committed in the
                # same transaction.  Re-read the outbox after observing the
                # terminal state so a completion racing the first probe cannot
                # be acknowledged without publishing its event.
                await self._publish_pending_result(
                    task_id=task_id,
                    document_id=document_id,
                    user_id=user_id,
                    attempt_version=attempt_version,
                )
                logger.info(
                    "Task %s was terminal or superseded before processing",
                    task_id,
                )
                return

            # 1. 更新状态：处理中
            processing_claimed = await self._persister.update_task_status(
                task_id,
                TaskStatusEnum.PROCESSING.value,
                attempt_version,
            )
            if not processing_claimed:
                # A competing worker may have committed completion between the
                # first outbox probe and this compare-and-set.  Drain again
                # before allowing the source record to be acknowledged.
                await self._publish_pending_result(
                    task_id=task_id,
                    document_id=document_id,
                    user_id=user_id,
                    attempt_version=attempt_version,
                )
                logger.info(
                    "Task %s reached a terminal state before processing", task_id
                )
                return

            await self._status_updater.update_status(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                status=TaskStatusEnum.PROCESSING,
                stage="initializing",
                progress=0.05,
                message="正在初始化处理器",
            )

            if not await asyncio.to_thread(Path(file_path).exists):
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

            if await self._is_cancelled(task_id, attempt_version):
                # A duplicate worker can win completion while this worker is
                # processing.  Its durable event must be drained before this
                # delivery returns successfully.
                await self._publish_pending_result(
                    task_id=task_id,
                    document_id=document_id,
                    user_id=user_id,
                    attempt_version=attempt_version,
                )
                logger.info(
                    "Task %s became terminal or superseded during processing; "
                    "result discarded",
                    task_id,
                )
                return

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

            try:
                await self._persister.save_result(
                    task_id=task_id,
                    document_id=document_id,
                    user_id=user_id,
                    file_type=file_type,
                    output=output,
                    attempt_version=attempt_version,
                )
            except TaskCompletionRejected:
                # Cancellation or another worker won the terminal compare-and-
                # set.  Acknowledge this Kafka record without publishing a
                # contradictory success/failure event or scheduling a retry.
                logger.info(
                    "Task %s result discarded because its terminal state changed",
                    task_id,
                )
                await self._publish_pending_result(
                    task_id=task_id,
                    document_id=document_id,
                    user_id=user_id,
                    attempt_version=attempt_version,
                )
                return

            # 4. Publish the result from the durable event stored in the same
            # transaction as Task/ExtractionResult completion.
            published = await self._publish_pending_result(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                attempt_version=attempt_version,
            )
            if not published:
                # The independent reconciler may have already published and
                # compacted this event after save_result committed.
                if not await self._persister.result_attempt_published(
                    task_id,
                    attempt_version,
                ):
                    raise ResultPublishPending(
                        f"任务结果已提交但缺少可发布事件: {task_id}"
                    )

            # 5. 更新最终状态
            try:
                await self._status_updater.update_status(
                    task_id=task_id,
                    document_id=document_id,
                    user_id=user_id,
                    status=TaskStatusEnum.COMPLETED,
                    stage="completed",
                    progress=1.0,
                    message="处理完成",
                )
            except Exception:
                # The authoritative DB state and result event are complete.
                # A secondary progress notification must not turn success into
                # a failed retry that can overwrite the terminal result.
                logger.exception(
                    "Task %s completed but its final status event failed",
                    task_id,
                )

            logger.info(f"Task {task_id} completed successfully")
            if self._retry_manager:
                self._retry_manager.mark_succeeded(task_id)

        except ResultPublishPending:
            # Leave the source Kafka offset uncommitted. On redelivery the
            # embedded outbox path above republishes the same stable event ID.
            raise
        except Exception as e:
            if not processing_claimed:
                # Failure to establish the DB claim is infrastructure failure,
                # not a document-processing failure.  Propagate it so Kafka
                # leaves the source offset uncommitted instead of emitting a
                # retry/DLQ record for work this consumer never owned.
                logger.exception(
                    "Task %s could not establish its processing claim",
                    task_id,
                )
                raise
            logger.error(f"Task {task_id} failed: {e}")
            # A database commit may have succeeded even if its acknowledgement
            # was lost. Recover that exact attempt's outbox before classifying
            # the exception as a processing failure.
            if await self._publish_pending_result(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                attempt_version=attempt_version,
            ):
                logger.info(
                    "Recovered committed result after ambiguous failure: task=%s",
                    task_id,
                )
                return
            if await self._is_cancelled(task_id, attempt_version):
                # Completion may have committed after the preceding outbox
                # probe but before this terminal-state read.  Re-read the
                # exact attempt before acknowledging the failed delivery.
                await self._publish_pending_result(
                    task_id=task_id,
                    document_id=document_id,
                    user_id=user_id,
                    attempt_version=attempt_version,
                )
                logger.info(
                    "Ignoring failure from terminal or superseded task attempt %s/%s",
                    task_id,
                    attempt_version,
                )
                return
            await self._handle_error(task, e)

    @staticmethod
    async def _is_cancelled(
        task_id: str,
        attempt_version: int | None = None,
    ) -> bool:
        async with async_session_context() as session:
            record = await asyncio.wait_for(
                session.get(Task, task_id),
                timeout=30.0,
            )
            return bool(
                record
                and (
                    record.status
                    in {
                        TaskStatus.COMPLETED.value,
                        TaskStatus.FAILED.value,
                        TaskStatus.CANCELLED.value,
                    }
                    or (
                        attempt_version is not None
                        and int(record.retry_count or 0) != attempt_version
                    )
                )
            )

    async def _publish_pending_result(
        self,
        *,
        task_id: str,
        document_id: str,
        user_id: str,
        attempt_version: int,
    ) -> bool:
        """Publish and acknowledge the exact attempt's embedded outbox event."""
        event = await self._persister.get_pending_result_event(
            task_id,
            attempt_version,
        )
        if event is None:
            return False

        event_id = str(event["event_id"])
        raw_timestamp = event.get("timestamp")
        try:
            event_timestamp = int(raw_timestamp) if raw_timestamp is not None else None
        except (TypeError, ValueError):
            # A malformed legacy timestamp must not create a permanent poison
            # event. Zero keeps duplicate deliveries deterministic.
            logger.warning(
                "Invalid result outbox timestamp; using stable fallback: task=%s",
                task_id,
            )
            event_timestamp = 0
        try:
            await self._publisher.publish_success(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                output=event["output"],
                event_id=event_id,
                event_timestamp=event_timestamp,
            )
            marked = await self._persister.mark_result_published(
                task_id,
                attempt_version,
                event_id,
            )
        except Exception as exc:
            raise ResultPublishPending(
                f"任务结果事件仍待发布: {task_id}/{event_id}"
            ) from exc

        if not marked:
            raise ResultPublishPending(
                f"任务结果事件发布后未能确认: {task_id}/{event_id}"
            )
        return True

    async def _handle_error(self, task: TaskMessage, error: Exception) -> None:
        """处理错误和重试"""
        task_id = task.body.task_id
        document_id = task.body.document_id
        user_id = task.header.user_id
        attempt_version = int(task.body.retry_count or 0)

        if await self._is_cancelled(task_id, attempt_version):
            # The terminal transition can race the outer caller's final outbox
            # probe and state check.  Drain once more before returning without
            # publishing a business failure or retry.
            await self._publish_pending_result(
                task_id=task_id,
                document_id=document_id,
                user_id=user_id,
                attempt_version=attempt_version,
            )
            logger.info(
                "Skipping error publication for terminal/superseded attempt %s/%s",
                task_id,
                attempt_version,
            )
            return

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

        if not self._retry_manager:
            raise RuntimeError("Retry manager is not initialized")

        # Publishing either the retry or DLQ record is part of handling this
        # source record. If publishing fails, let the exception reach the Kafka
        # loop so its offset remains uncommitted.
        retry_scheduled = await self._retry_manager.handle_failure(error_task, error)
        if not retry_scheduled:
            await self._persister.mark_attempt_failed(
                task_id,
                attempt_version,
                error,
            )
