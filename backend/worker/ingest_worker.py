"""独立入库 Worker：从 kb_pending_files 拉取 status=1 的任务进行处理。"""

import asyncio
import logging
import os
import signal

from sqlalchemy import select

from backend.config.dependency_versions import validate_runtime_dependencies
from backend.config.loggers import get_logger
from backend.db.connection import async_session_context, close_db, close_db_async
from backend.db.models import PendingFile
from backend.db.redis import close_redis_pool
from backend.rag.ingestion.concurrency import ingest_file_concurrency
from backend.worker.components.ingest_pending_processor import PendingIngestProcessor

logger = get_logger(__name__)


def file_task_succeeded(task: asyncio.Task[bool], file_id: str) -> bool:
    """Resolve one file task without allowing its cancellation to stop the worker."""
    try:
        return bool(task.result())
    except asyncio.CancelledError as exc:
        logger.warning(
            "ingest worker file task cancelled; worker will continue and the file "
            "will remain queued/retryable: file_id=%s error=%s",
            file_id,
            exc or "cancelled",
        )
        return False
    except Exception as exc:
        logger.error(
            "ingest worker task error: file_id=%s error=%s",
            file_id,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return False


def select_refill_candidates(
    file_ids: list[str],
    *,
    active_file_ids: set[str],
    cooldown_until: dict[str, float],
    now: float,
    limit: int,
) -> list[str]:
    """Choose pending files for free worker slots without waiting for a batch barrier."""
    selected: list[str] = []
    for file_id in file_ids:
        if file_id in active_file_ids:
            continue
        if cooldown_until.get(file_id, 0.0) > now:
            continue
        selected.append(file_id)
        if len(selected) >= max(int(limit), 0):
            break
    return selected


async def process_file_batch(
    processor: PendingIngestProcessor,
    file_ids: list[str],
    *,
    max_concurrent_files: int,
) -> int:
    semaphore = asyncio.Semaphore(max(int(max_concurrent_files or 1), 1))

    async def process_one(file_id: str) -> bool:
        async with semaphore:
            return await processor.process_file(file_id)

    results = await asyncio.gather(
        *(process_one(file_id) for file_id in file_ids),
        return_exceptions=True,
    )
    processed_count = 0
    for file_id, result in zip(file_ids, results, strict=True):
        # CancelledError is a BaseException on supported Python versions, so an
        # Exception-only check would incorrectly count a cancelled file as done.
        if isinstance(result, BaseException):
            logger.error(
                "ingest worker task error: file_id=%s error=%s",
                file_id,
                result,
                exc_info=(type(result), result, result.__traceback__),
            )
            continue
        if result:
            processed_count += 1
    return processed_count


async def run_worker():
    processor = PendingIngestProcessor()
    poll_interval = float(os.getenv("INGEST_WORKER_POLL_INTERVAL", "2"))
    batch_size = int(os.getenv("INGEST_WORKER_BATCH_SIZE", "5"))
    candidate_multiplier = int(os.getenv("INGEST_WORKER_CANDIDATE_MULTIPLIER", "10"))
    candidate_limit = max(batch_size, batch_size * max(1, candidate_multiplier))

    logger.info(
        "ingest worker started: poll_interval=%s, batch_size=%s, candidate_limit=%s",
        poll_interval,
        batch_size,
        candidate_limit,
    )

    active_tasks: dict[asyncio.Task[bool], str] = {}
    cooldown_until: dict[str, float] = {}
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
    try:
        while not shutdown_requested.is_set():
            try:
                batch_size = int(os.getenv("INGEST_WORKER_BATCH_SIZE", str(batch_size)))
                batch_size = max(batch_size, 1)
                candidate_multiplier = int(
                    os.getenv("INGEST_WORKER_CANDIDATE_MULTIPLIER", str(candidate_multiplier))
                )
                candidate_limit = max(batch_size, batch_size * max(1, candidate_multiplier))
                max_concurrent_files = min(batch_size, ingest_file_concurrency())
                free_slots = max(max_concurrent_files - len(active_tasks), 0)
                if free_slots:
                    async with async_session_context() as session:
                        result = await asyncio.wait_for(
                            session.execute(
                                select(PendingFile.file_id)
                                .where(PendingFile.status == 1)
                                .order_by(
                                    PendingFile.retry_count.desc(),
                                    PendingFile.updated_at.asc(),
                                    PendingFile.created_at.asc(),
                                )
                                .limit(candidate_limit)
                            ),
                            timeout=30.0,
                        )
                        file_ids = list(result.scalars().all())

                    now = asyncio.get_running_loop().time()
                    selected = select_refill_candidates(
                        file_ids,
                        active_file_ids=set(active_tasks.values()),
                        cooldown_until=cooldown_until,
                        now=now,
                        limit=free_slots,
                    )
                    for file_id in selected:
                        task = asyncio.create_task(
                            processor.process_file(file_id),
                            name=f"ingest-worker-{file_id}",
                        )
                        active_tasks[task] = file_id

                if not active_tasks:
                    try:
                        await asyncio.wait_for(
                            shutdown_requested.wait(),
                            timeout=poll_interval,
                        )
                    except TimeoutError:
                        pass
                    continue

                done, _pending = await asyncio.wait(
                    set(active_tasks),
                    timeout=poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                now = asyncio.get_running_loop().time()
                for task in done:
                    file_id = active_tasks.pop(task)
                    succeeded = file_task_succeeded(task, file_id)
                    if not succeeded:
                        # A different worker may own the Redis file lock. Avoid
                        # immediately selecting the same candidate in a hot loop.
                        cooldown_until[file_id] = now + max(poll_interval * 5, 10.0)
                cooldown_until = {
                    file_id: deadline
                    for file_id, deadline in cooldown_until.items()
                    if deadline > now
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("ingest worker loop error: %s", exc, exc_info=True)
                await asyncio.sleep(poll_interval)
    finally:
        tasks = list(active_tasks)
        if tasks:
            drain_timeout = max(
                0.0,
                float(os.getenv("WORKER_SHUTDOWN_TIMEOUT_SECONDS", "30") or "30"),
            )
            done, pending = await asyncio.wait(tasks, timeout=drain_timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            logger.info(
                "ingest worker drained: completed=%d cancelled=%d",
                len(done),
                len(pending),
            )
        await close_db_async()
        await asyncio.to_thread(close_db)
        await close_redis_pool()
        for sig in installed_signals:
            loop.remove_signal_handler(sig)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s:%(lineno)d - %(message)s",
    )
    report = validate_runtime_dependencies()
    logger.info("runtime dependency contract validated: %s", report.as_dict())
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
