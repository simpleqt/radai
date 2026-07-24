"""Bounded in-process lifecycle manager for reconnectable agent streams."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from backend.domain.agents.events import AgentEvent, EventEnvelope

_MAX_TASKS = max(8, int(os.getenv("AGENT_BACKGROUND_MAX_TASKS", "128") or "128"))
_MAX_EVENTS = max(128, int(os.getenv("AGENT_BACKGROUND_MAX_EVENTS", "4096") or "4096"))
_MAX_RUNNING_TASKS = max(
    1, int(os.getenv("AGENT_BACKGROUND_MAX_RUNNING_TASKS", "8") or "8")
)
_MAX_TASKS_PER_USER = max(
    1, int(os.getenv("AGENT_BACKGROUND_MAX_TASKS_PER_USER", "2") or "2")
)
_EVENT_BATCH_SIZE = max(
    1, int(os.getenv("AGENT_BACKGROUND_EVENT_BATCH_SIZE", "128") or "128")
)
_EVENT_FLUSH_INTERVAL_SECONDS = max(
    0.001,
    float(os.getenv("AGENT_BACKGROUND_EVENT_FLUSH_INTERVAL_MS", "20") or "20") / 1000,
)
_EXECUTION_TIMEOUT_SECONDS = max(
    1.0,
    float(
        os.getenv("AGENT_BACKGROUND_EXECUTION_TIMEOUT_SECONDS", "1800")
        or "1800"
    ),
)
logger = logging.getLogger(__name__)


class AgentTaskCapacityError(RuntimeError):
    pass


class AgentTaskConflictError(RuntimeError):
    """Raised when a thread already has an unfinished background run."""

    def __init__(self, record: AgentTaskRecord) -> None:
        super().__init__("该会话已有正在执行的后台任务，请等待完成或先取消现有任务")
        self.record = record


@dataclass
class AgentTaskRecord:
    task_id: str
    user_id: str
    agent_code: str
    thread_id: str
    question: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error_message: str | None = None
    events: list[AgentEvent] = field(default_factory=list)
    dropped_event_count: int = 0
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    runner: asyncio.Task[None] | None = None

    def public_info(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "question": self.question,
            "draft_id": None,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_message": self.error_message,
            "last_event_id": self.dropped_event_count + len(self.events) - 1,
        }


class AgentTaskRegistry:
    """Own background streams and persist terminal replay data across restarts."""

    def __init__(self, persistence_path: str | Path | None = None) -> None:
        self._records: OrderedDict[str, AgentTaskRecord] = OrderedDict()
        self._lock = asyncio.Lock()
        self._persistence_path = Path(persistence_path) if persistence_path else None
        self._storage_lock = asyncio.Lock()
        self._storage_ready = False
        self._db: Any | None = None
        self._run_semaphore = asyncio.Semaphore(_MAX_RUNNING_TASKS)
        self._event_batch_lock = asyncio.Lock()
        self._pending_event_writes: list[
            tuple[AgentTaskRecord, AgentEvent, int, int]
        ] = []
        self._event_flush_task: asyncio.Task[None] | None = None

    async def _ensure_storage(self) -> None:
        if self._persistence_path is None or self._storage_ready:
            return
        async with self._storage_lock:
            if self._storage_ready:
                return
            import aiosqlite

            self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
            db = await aiosqlite.connect(self._persistence_path)
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    agent_code TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    error_message TEXT,
                    dropped_event_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS agent_task_events (
                    task_id TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (task_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS ix_agent_tasks_owner
                    ON agent_tasks(user_id, agent_code, thread_id, status);
                """
            )
            now = time.time()
            await db.execute(
                """
                UPDATE agent_tasks
                SET status = 'interrupted', finished_at = ?,
                    error_message = '服务重启导致后台任务中断'
                WHERE status IN ('pending', 'running')
                """,
                (now,),
            )
            await db.commit()
            self._db = db
            self._storage_ready = True

    async def _persist_record(self, record: AgentTaskRecord) -> None:
        if self._persistence_path is None:
            return
        await self._ensure_storage()
        async with self._storage_lock:
            db = self._db
            if db is None:
                return
            await db.execute(
                """
                INSERT INTO agent_tasks (
                    task_id, user_id, agent_code, thread_id, question, status,
                    created_at, started_at, finished_at, error_message,
                    dropped_event_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status=excluded.status,
                    started_at=excluded.started_at,
                    finished_at=excluded.finished_at,
                    error_message=excluded.error_message,
                    dropped_event_count=excluded.dropped_event_count
                """,
                (
                    record.task_id,
                    record.user_id,
                    record.agent_code,
                    record.thread_id,
                    record.question,
                    record.status,
                    record.created_at,
                    record.started_at,
                    record.finished_at,
                    record.error_message,
                    record.dropped_event_count,
                ),
            )
            await db.commit()

    async def _persist_event(
        self, record: AgentTaskRecord, event: AgentEvent, event_id: int
    ) -> None:
        if self._persistence_path is None:
            return
        await self._ensure_storage()
        envelope = event.to_envelope(
            event_id=f"evt_{uuid.uuid5(uuid.NAMESPACE_URL, f'{record.task_id}:{event_id}').hex}",
            correlation_id=record.task_id,
            producer="agent-task-registry",
            unit_id=record.task_id,
            idempotency_key=f"{record.task_id}:{event_id}",
        )
        event_json = json.dumps(envelope.to_dict(), ensure_ascii=False, default=str)
        async with self._storage_lock:
            db = self._db
            if db is None:
                return
            await db.execute(
                "INSERT OR REPLACE INTO agent_task_events(task_id, event_id, event_json) VALUES (?, ?, ?)",
                (record.task_id, event_id, event_json),
            )
            if record.dropped_event_count:
                await db.execute(
                    "DELETE FROM agent_task_events WHERE task_id = ? AND event_id < ?",
                    (record.task_id, record.dropped_event_count),
                )
            await db.commit()

    async def _persist_events(
        self,
        items: list[tuple[AgentTaskRecord, AgentEvent, int, int]],
    ) -> None:
        if self._persistence_path is None or not items:
            return
        await self._ensure_storage()
        rows: list[tuple[str, int, str]] = []
        prune_before: dict[str, int] = {}
        for record, event, event_id, dropped_event_count in items:
            envelope = event.to_envelope(
                event_id=f"evt_{uuid.uuid5(uuid.NAMESPACE_URL, f'{record.task_id}:{event_id}').hex}",
                correlation_id=record.task_id,
                producer="agent-task-registry",
                unit_id=record.task_id,
                idempotency_key=f"{record.task_id}:{event_id}",
            )
            rows.append(
                (
                    record.task_id,
                    event_id,
                    json.dumps(envelope.to_dict(), ensure_ascii=False, default=str),
                )
            )
            if dropped_event_count:
                prune_before[record.task_id] = max(
                    prune_before.get(record.task_id, 0), dropped_event_count
                )

        async with self._storage_lock:
            db = self._db
            if db is None:
                return
            await db.executemany(
                "INSERT OR REPLACE INTO agent_task_events(task_id, event_id, event_json) VALUES (?, ?, ?)",
                rows,
            )
            for task_id, event_id in prune_before.items():
                await db.execute(
                    "DELETE FROM agent_task_events WHERE task_id = ? AND event_id < ?",
                    (task_id, event_id),
                )
            await db.commit()

    async def _flush_event_batches(self) -> None:
        try:
            while True:
                await asyncio.sleep(_EVENT_FLUSH_INTERVAL_SECONDS)
                async with self._event_batch_lock:
                    batch = self._pending_event_writes[:_EVENT_BATCH_SIZE]
                    del self._pending_event_writes[: len(batch)]
                if not batch:
                    return
                try:
                    await self._persist_events(batch)
                except Exception:
                    logger.exception(
                        "后台智能体任务事件批量持久化失败: count=%s", len(batch)
                    )
                async with self._event_batch_lock:
                    if not self._pending_event_writes:
                        return
        finally:
            async with self._event_batch_lock:
                if self._event_flush_task is asyncio.current_task():
                    self._event_flush_task = None
                if self._pending_event_writes and self._event_flush_task is None:
                    self._event_flush_task = asyncio.create_task(
                        self._flush_event_batches(), name="agent-task-event-flush"
                    )

    async def _queue_persist_event(
        self, record: AgentTaskRecord, event: AgentEvent, event_id: int
    ) -> None:
        if self._persistence_path is None:
            return
        async with self._event_batch_lock:
            self._pending_event_writes.append(
                (record, event, event_id, record.dropped_event_count)
            )
            if self._event_flush_task is None or self._event_flush_task.done():
                self._event_flush_task = asyncio.create_task(
                    self._flush_event_batches(), name="agent-task-event-flush"
                )

    async def _flush_pending_events(self) -> None:
        while True:
            async with self._event_batch_lock:
                task = self._event_flush_task
                has_pending = bool(self._pending_event_writes)
                if has_pending and (task is None or task.done()):
                    task = asyncio.create_task(
                        self._flush_event_batches(), name="agent-task-event-flush"
                    )
                    self._event_flush_task = task
            if task is None:
                return
            await asyncio.shield(task)
            async with self._event_batch_lock:
                if not self._pending_event_writes:
                    return

    async def _load_record(self, task_id: str) -> AgentTaskRecord | None:
        if self._persistence_path is None:
            return None
        await self._ensure_storage()
        async with self._storage_lock:
            db = self._db
            if db is None:
                return None
            cursor = await db.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            cursor = await db.execute(
                "SELECT event_json FROM agent_task_events WHERE task_id = ? ORDER BY event_id",
                (task_id,),
            )
            event_rows = await cursor.fetchall()
        events: list[AgentEvent] = []
        for event_row in event_rows:
            raw = json.loads(event_row["event_json"])
            if raw.get("schema_version") == "2.0" and raw.get("event_id"):
                events.append(EventEnvelope.from_dict(raw).to_agent_event())
            else:
                # Existing databases may still contain the legacy SSE shape.
                events.append(
                    AgentEvent(
                        type=raw["type"],
                        request_id=raw["request_id"],
                        thread_id=raw["thread_id"],
                        sequence=int(raw["sequence"]),
                        payload=dict(raw.get("payload") or {}),
                        created_at=raw["created_at"],
                    )
                )
        return AgentTaskRecord(
            task_id=row["task_id"],
            user_id=row["user_id"],
            agent_code=row["agent_code"],
            thread_id=row["thread_id"],
            question=row["question"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_message=row["error_message"],
            events=events,
            dropped_event_count=row["dropped_event_count"],
        )

    async def start(
        self,
        *,
        user_id: str,
        agent_code: str,
        thread_id: str,
        question: str,
        stream_factory: Callable[[], AsyncIterator[AgentEvent]],
    ) -> AgentTaskRecord:
        async with self._lock:
            for record in self._records.values():
                if (
                    record.user_id == user_id
                    and record.agent_code == agent_code
                    and record.thread_id == thread_id
                    and record.status in {"pending", "running"}
                ):
                    raise AgentTaskConflictError(record)
            self._evict_finished_locked()
            if len(self._records) >= _MAX_TASKS:
                raise AgentTaskCapacityError("后台智能体任务已达到容量上限")
            active_for_user = sum(
                1
                for item in self._records.values()
                if item.user_id == user_id and item.status in {"pending", "running"}
            )
            if active_for_user >= _MAX_TASKS_PER_USER:
                raise AgentTaskCapacityError(
                    f"单个用户最多同时提交 {_MAX_TASKS_PER_USER} 个后台智能体任务"
                )
            task_id = str(uuid.uuid4())
            record = AgentTaskRecord(
                task_id=task_id,
                user_id=user_id,
                agent_code=agent_code,
                thread_id=thread_id,
                question=question,
            )
            record.events.append(
                AgentEvent(
                    type="workflow.step",
                    request_id="",
                    thread_id=thread_id,
                    sequence=-1,
                    payload={
                        "event": {
                            "id": f"background-task:{task_id}",
                            "kind": "background_task_started",
                            "title": "深度研究后台任务已启动",
                            "detail": "连接中断后可重新订阅该任务。",
                            "metadata": {"task_id": task_id},
                        }
                    },
                )
            )
            self._records[task_id] = record
            await self._persist_record(record)
            await self._persist_event(record, record.events[0], 0)
            record.runner = asyncio.create_task(
                self._run(record, stream_factory),
                name=f"agent-task:{task_id}",
            )
            return record

    async def startup_ack(self, record: AgentTaskRecord) -> AsyncIterator[AgentEvent]:
        """Acknowledge task creation without holding the query request open."""
        if record.events:
            yield record.events[0]
        yield AgentEvent(
            type="done",
            request_id="",
            thread_id=record.thread_id,
            sequence=0,
            payload={"background_task_started": True},
        )

    async def _run(
        self,
        record: AgentTaskRecord,
        stream_factory: Callable[[], AsyncIterator[AgentEvent]],
    ) -> None:
        try:
            await self._run_semaphore.acquire()
        except asyncio.CancelledError:
            record.status = "cancelled"
            record.finished_at = time.time()
            await self._persist_record(record)
            await self._append(
                record,
                AgentEvent(
                    type="done",
                    request_id="",
                    thread_id=record.thread_id,
                    sequence=len(record.events),
                    payload={"cancelled": True, "status": "cancelled"},
                ),
            )
            await self._flush_pending_events()
            async with record.condition:
                record.condition.notify_all()
            raise
        record.status = "running"
        record.started_at = time.time()
        await self._persist_record(record)
        saw_error = False
        saw_done = False
        terminal_event: AgentEvent | None = None

        async def consume_stream() -> None:
            nonlocal saw_error, saw_done, terminal_event
            async for event in stream_factory():
                saw_error = saw_error or event.type == "error"
                saw_done = saw_done or event.type == "done"
                if event.type == "done":
                    # ``done`` is a commit barrier, not a progress event. Do
                    # not expose it until the upstream generator has resumed
                    # and completed its runtime/trace cleanup.
                    terminal_event = event
                    continue
                if event.type == "error" and not record.error_message:
                    record.error_message = str(
                        event.payload.get("message")
                        or event.payload.get("error")
                        or "智能体后台任务执行失败"
                    )
                await self._append(record, event)

        try:
            await asyncio.wait_for(
                consume_stream(),
                timeout=_EXECUTION_TIMEOUT_SECONDS,
            )
            if not saw_done:
                if not saw_error:
                    saw_error = True
                    record.error_message = "智能体事件流未提交终态，任务结果不可确认"
                    await self._append(
                        record,
                        AgentEvent(
                            type="error",
                            request_id="",
                            thread_id=record.thread_id,
                            sequence=len(record.events),
                            payload={
                                "message": record.error_message,
                                "error": record.error_message,
                                "error_code": "agent_terminal_event_missing",
                            },
                        ),
                    )
                terminal_event = AgentEvent(
                    type="done",
                    request_id="",
                    thread_id=record.thread_id,
                    sequence=len(record.events),
                    payload={"status": "failed"},
                )
            record.status = "failed" if saw_error else "completed"
            record.finished_at = time.time()
            await self._persist_record(record)
            assert terminal_event is not None
            await self._append(record, terminal_event)
        except asyncio.TimeoutError:  # noqa: UP041 -- Python 3.10 compatibility
            record.status = "timed_out"
            record.finished_at = time.time()
            record.error_message = (
                f"智能体后台任务超过 {_EXECUTION_TIMEOUT_SECONDS:g} 秒执行上限"
            )
            await self._append(
                record,
                AgentEvent(
                    type="error",
                    request_id="",
                    thread_id=record.thread_id,
                    sequence=len(record.events),
                    payload={
                        "message": record.error_message,
                        "error": record.error_message,
                        "error_code": "agent_background_timeout",
                    },
                ),
            )
            await self._persist_record(record)
            await self._append(
                record,
                AgentEvent(
                    type="done",
                    request_id="",
                    thread_id=record.thread_id,
                    sequence=len(record.events),
                    payload={"status": "timed_out"},
                ),
            )
        except asyncio.CancelledError:
            record.status = "cancelled"
            record.finished_at = time.time()
            if terminal_event is not None:
                terminal_event = replace(
                    terminal_event,
                    payload={
                        **terminal_event.payload,
                        "cancelled": True,
                        "status": "cancelled",
                    },
                )
            else:
                terminal_event = AgentEvent(
                    type="done",
                    request_id="",
                    thread_id=record.thread_id,
                    sequence=len(record.events),
                    payload={"cancelled": True, "status": "cancelled"},
                )
            await self._persist_record(record)
            if terminal_event is not None:
                await self._append(record, terminal_event)
            raise
        except Exception:
            record.status = "failed"
            record.finished_at = time.time()
            record.error_message = "智能体后台任务执行失败"
            await self._append(
                record,
                AgentEvent(
                    type="error",
                    request_id="",
                    thread_id=record.thread_id,
                    sequence=len(record.events),
                    payload={
                        "message": record.error_message,
                        "error": record.error_message,
                        "error_code": "agent_background_failed",
                    },
                ),
            )
            await self._persist_record(record)
            await self._append(
                record,
                AgentEvent(
                    type="done",
                    request_id="",
                    thread_id=record.thread_id,
                    sequence=len(record.events),
                    payload={},
                ),
            )
        finally:
            self._run_semaphore.release()
            if record.finished_at is None:
                record.finished_at = time.time()
            try:
                await self._persist_record(record)
            except Exception:
                logger.exception(
                    "后台智能体任务状态持久化失败: task_id=%s", record.task_id
                )
            await self._flush_pending_events()
            async with record.condition:
                record.condition.notify_all()

    async def _append(self, record: AgentTaskRecord, event: AgentEvent) -> None:
        async with record.condition:
            if len(record.events) >= _MAX_EVENTS:
                record.events.pop(0)
                record.dropped_event_count += 1
            record.events.append(event)
            event_id = record.dropped_event_count + len(record.events) - 1
            record.condition.notify_all()
        await self._queue_persist_event(record, event, event_id)

    async def get(
        self, task_id: str, *, user_id: str, agent_code: str
    ) -> AgentTaskRecord | None:
        async with self._lock:
            record = self._records.get(task_id)
            if record and record.user_id == user_id and record.agent_code == agent_code:
                self._records.move_to_end(task_id)
                return record
            if record is not None:
                return None

        # SQLite I/O must not hold the registry lock: one cold lookup should
        # not block unrelated in-memory task operations.
        loaded = await self._load_record(task_id)
        if loaded is None:
            return None
        async with self._lock:
            record = self._records.get(task_id)
            if record is None:
                record = loaded
                self._records[task_id] = record
            if record.user_id == user_id and record.agent_code == agent_code:
                self._records.move_to_end(task_id)
                return record
            return None

    async def active_for_thread(
        self, *, user_id: str, agent_code: str, thread_id: str
    ) -> AgentTaskRecord | None:
        async with self._lock:
            for record in reversed(self._records.values()):
                if (
                    record.user_id == user_id
                    and record.agent_code == agent_code
                    and record.thread_id == thread_id
                    and record.status in {"pending", "running"}
                ):
                    return record
            return None

    async def cancel(self, record: AgentTaskRecord) -> None:
        async with self._lock:
            runner = record.runner
        if runner is not None and not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass

    async def shutdown(self) -> None:
        """Cancel and persist all active tasks during graceful API shutdown."""
        async with self._lock:
            records = list(self._records.values())
        await asyncio.gather(
            *(self.cancel(record) for record in records),
            return_exceptions=True,
        )
        await self._flush_pending_events()
        if self._db is not None:
            async with self._storage_lock:
                if self._db is not None:
                    await self._db.close()
                    self._db = None
                    self._storage_ready = False

    async def subscribe(
        self,
        record: AgentTaskRecord,
        *,
        replay: bool,
        after_event_id: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if after_event_id is not None:
            index = max(0, after_event_id + 1 - record.dropped_event_count)
            index = min(index, len(record.events))
        else:
            index = 0 if replay else len(record.events)
        dropped_seen = record.dropped_event_count
        while True:
            async with record.condition:
                if record.dropped_event_count > dropped_seen:
                    index = max(0, index - (record.dropped_event_count - dropped_seen))
                    dropped_seen = record.dropped_event_count
                while index >= len(record.events) and record.status in {
                    "pending",
                    "running",
                }:
                    await record.condition.wait()
                batch_start = record.dropped_event_count + index
                batch = list(record.events[index:])
                index = len(record.events)
                terminal = record.status not in {"pending", "running"}
            for offset, event in enumerate(batch):
                # The task-local cursor is independent from workflow sequence
                # numbers and lets clients resume without duplicating chunks.
                yield replace(
                    event,
                    payload={**event.payload, "_task_event_id": batch_start + offset},
                )
            if terminal:
                if not batch:
                    yield AgentEvent(
                        type="done",
                        request_id="",
                        thread_id=record.thread_id,
                        sequence=len(record.events),
                        payload={"status": record.status},
                    )
                return

    def _evict_finished_locked(self) -> None:
        while len(self._records) >= _MAX_TASKS:
            removable = next(
                (
                    task_id
                    for task_id, record in self._records.items()
                    if record.status not in {"pending", "running"}
                ),
                None,
            )
            if removable is None:
                break
            self._records.pop(removable, None)


_DEFAULT_PERSISTENCE_PATH = Path(
    os.getenv(
        "AGENT_TASK_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "data" / "agent_tasks.db"),
    )
)
agent_task_registry = AgentTaskRegistry(_DEFAULT_PERSISTENCE_PATH)
