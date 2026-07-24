"""Durable dispatch for task messages created by database-backed API actions.

The outbox is embedded in ``Task.result`` so the task state transition and the
message payload commit atomically without introducing another deployment-time
table dependency.  A background reconciler republishes pending records with a
stable message ID; delivery is therefore at-least-once and safely deduplicable.
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, func, or_, select

from backend.config.loggers import get_logger
from backend.db import async_session_context
from backend.db.models import Task, TaskStatus
from backend.kafka.client import get_task_producer
from backend.kafka.schemas import TaskMessage

logger = get_logger(__name__)

TASK_DISPATCH_OUTBOX_KEY = "_task_dispatch_outbox"
TASK_DISPATCH_OUTBOX_KIND = "task_dispatch"
_OUTBOX_SCAN_MIN_PAGE_SIZE = 100
_OUTBOX_SCAN_MAX_PAGE_SIZE = 1000
_OUTBOX_SCAN_MAX_ROWS = 1000
_OUTBOX_SCHEMA_MIGRATION = "202607100014_add_task_outbox_scan_index"


class OutboxBacklogFullError(RuntimeError):
    """Raised before adding another durable event to a saturated outbox."""


def _max_pending_events() -> int:
    try:
        return max(100, int(os.getenv("OUTBOX_MAX_PENDING_EVENTS", "10000")))
    except (TypeError, ValueError):
        return 10000


async def ensure_outbox_capacity(
    session,
    *,
    kind: str,
    excluding_task_id: str | None = None,
) -> None:
    query = select(func.count(Task.task_id)).where(Task.outbox_kind == kind)
    if excluding_task_id:
        query = query.where(Task.task_id != excluding_task_id)
    pending = int((await session.execute(query)).scalar_one() or 0)
    maximum = _max_pending_events()
    if pending >= maximum:
        raise OutboxBacklogFullError(
            f"outbox backlog for {kind!r} reached configured limit {maximum}"
        )


def _is_outbox_schema_error(exc: BaseException) -> bool:
    """Recognize a legacy ``tasks`` table without the outbox schema."""

    original = getattr(exc, "orig", exc)
    args = getattr(original, "args", ())
    error_code = args[0] if args and isinstance(args[0], int) else None
    if error_code in {1054, 1146}:
        return True
    message = str(original).lower()
    return (
        "outbox_kind" in message
        and ("unknown column" in message or "no such column" in message)
    )


def task_dispatch_event_id(task_id: str, attempt: int) -> str:
    """Return the stable ID for one administratively dispatched task attempt."""
    return str(uuid5(NAMESPACE_URL, f"task-dispatch:{task_id}:{attempt}"))


def build_task_dispatch_event(task: TaskMessage, attempt: int) -> dict:
    event_id = task_dispatch_event_id(task.body.task_id, attempt)
    if task.header.message_id != event_id:
        task = task.model_copy(
            update={"header": task.header.model_copy(update={"message_id": event_id})}
        )
    return {
        "event_id": event_id,
        "attempt": int(attempt),
        "state": "pending",
        "attempts": 0,
        "available_at": datetime.now().isoformat(),
        "payload": task.model_dump(mode="json"),
    }


def task_dispatch_result(event: dict) -> dict:
    return {TASK_DISPATCH_OUTBOX_KEY: event}


class TaskDispatchOutboxDispatcher:
    """Publish embedded task outbox records and reconcile crash windows."""

    def __init__(self, *, poll_interval_seconds: float = 2.0) -> None:
        self._poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._runner: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._scan_cursor: tuple[datetime, str] | None = None

    async def start(self) -> None:
        if self._runner is not None and not self._runner.done():
            return
        await self.ensure_schema_ready()
        # ``ensure_schema_ready`` yields to the event loop. Recheck so two
        # concurrent startup calls cannot create duplicate runners.
        if self._runner is not None and not self._runner.done():
            return
        self._stop_event = asyncio.Event()
        self._runner = asyncio.create_task(
            self._run(),
            name="task-dispatch-outbox",
        )

    async def ensure_schema_ready(self) -> None:
        """Fail once with an actionable error instead of polling a bad schema."""

        try:
            async with async_session_context() as session:
                # Select only the discriminator. Selecting the mapped Task
                # entity would obscure which newly required column is absent.
                await session.execute(select(Task.outbox_kind).limit(0))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _is_outbox_schema_error(exc):
                raise
            raise RuntimeError(
                "任务投递 outbox 数据库结构未就绪：缺少 tasks.outbox_kind；"
                f"请先执行迁移 {_OUTBOX_SCHEMA_MIGRATION} 后再启动服务"
            ) from exc

    async def stop(self) -> None:
        runner = self._runner
        if runner is None:
            return
        self._stop_event.set()
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        self._runner = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.dispatch_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("任务投递 outbox 扫描失败: %s", exc)

            timeout = self._poll_interval_seconds * random.uniform(0.9, 1.1)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
                pass

    @staticmethod
    def _pending_event(task: Task, *, event_id: str | None = None) -> dict | None:
        if not isinstance(task.result, dict):
            return None
        event = task.result.get(TASK_DISPATCH_OUTBOX_KEY)
        if not isinstance(event, dict) or event.get("state") != "pending":
            return None
        if event_id is not None and event.get("event_id") != event_id:
            return None
        try:
            event_attempt = int(event.get("attempt", -1))
            task_attempt = int(task.retry_count or 0)
        except (TypeError, ValueError):
            return None
        if (
            event_attempt != task_attempt
            or not isinstance(event.get("payload"), dict)
            or not event.get("event_id")
        ):
            return None
        return event

    @staticmethod
    def _is_due(event: dict, now: datetime) -> bool:
        raw = event.get("available_at")
        if not raw:
            return True
        try:
            available_at = datetime.fromisoformat(str(raw))
        except ValueError:
            return True
        compare_now = (
            datetime.now(tz=available_at.tzinfo)
            if available_at.tzinfo is not None
            else now
        )
        return available_at <= compare_now

    async def dispatch_pending(self, *, limit: int = 100) -> int:
        """Dispatch up to ``limit`` due records; recheck each one under lock."""
        dispatch_limit = max(1, int(limit))
        page_size = min(
            _OUTBOX_SCAN_MAX_PAGE_SIZE,
            max(_OUTBOX_SCAN_MIN_PAGE_SIZE, dispatch_limit),
        )
        async with async_session_context() as session:
            filters = (
                Task.status == TaskStatus.PENDING.value,
                Task.outbox_kind == TASK_DISPATCH_OUTBOX_KIND,
                Task.updated_at.is_not(None),
            )
            now = datetime.now()
            candidates: list[tuple[str, str]] = []
            scanned = 0
            cursor = self._scan_cursor
            while scanned < _OUTBOX_SCAN_MAX_ROWS and len(candidates) < dispatch_limit:
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
                    await session.execute(
                        query.order_by(
                            Task.updated_at.asc(),
                            Task.task_id.asc(),
                        ).limit(min(page_size, _OUTBOX_SCAN_MAX_ROWS - scanned))
                    )
                ).all()
                if not rows:
                    self._scan_cursor = None
                    break

                scanned += len(rows)
                for task, task_updated_at in rows:
                    # Persist the last row actually inspected, not the end of
                    # the fetched page, so a full candidate batch cannot skip
                    # an unvisited healthy tail forever.
                    cursor = (task_updated_at, task.task_id)
                    self._scan_cursor = cursor
                    event = self._pending_event(task)
                    if event is None or not self._is_due(event, now):
                        continue
                    candidates.append((task.task_id, str(event["event_id"])))
                    if len(candidates) >= dispatch_limit:
                        break

        dispatched = 0
        for task_id, event_id in candidates:
            try:
                if await self.publish_task(task_id, event_id):
                    dispatched += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                # One corrupt row or transient per-record DB error must not
                # prevent the rest of this independent batch from dispatching.
                logger.exception(
                    "任务投递 outbox 单条记录处理失败: task=%s event=%s",
                    task_id,
                    event_id,
                )
        return dispatched

    async def publish_task(self, task_id: str, event_id: str) -> bool:
        """Publish one event while holding its task row lock.

        A crash after Kafka acknowledgement but before the DB commit can cause
        a duplicate publish. The payload and message ID are stable, so this is
        the expected at-least-once outbox behavior.
        """
        async with async_session_context() as session:
            task = (
                await session.execute(
                    select(Task)
                    .where(Task.task_id == task_id)
                    .with_for_update()
                    .limit(1)
                )
            ).scalar_one_or_none()
            if task is None:
                return False

            if isinstance(task.result, dict):
                existing = task.result.get(TASK_DISPATCH_OUTBOX_KEY)
                if (
                    isinstance(existing, dict)
                    and existing.get("event_id") == event_id
                    and existing.get("state") == "sent"
                ):
                    task.outbox_kind = None
                    return True

            event = self._pending_event(task, event_id=event_id)
            if event is None or not self._is_due(event, datetime.now()):
                return False

            try:
                message = TaskMessage.model_validate(event["payload"])
                if message.header.message_id != event_id:
                    raise ValueError("task dispatch message_id does not match event_id")
                await get_task_producer().submit_task(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts = int(event.get("attempts") or 0) + 1
                delay = min(300, 2 ** min(attempts, 8))
                pending = {
                    **event,
                    "attempts": attempts,
                    "available_at": (
                        datetime.now() + timedelta(seconds=delay)
                    ).isoformat(),
                    "last_error": f"{type(exc).__name__}: {exc}"[:1000],
                }
                task.result = {
                    **task.result,
                    TASK_DISPATCH_OUTBOX_KEY: pending,
                }
                logger.warning(
                    "任务 outbox 投递失败，将重试: task=%s event=%s error=%s",
                    task_id,
                    event_id,
                    exc,
                )
                return False

            # The Kafka acknowledgement is the terminal outbox state. Remove
            # the private payload so sent rows cannot starve later pending rows
            # in the bounded reconciler scan or leak through another API path.
            task.result = None
            task.outbox_kind = None
            return True


task_dispatch_outbox = TaskDispatchOutboxDispatcher()
