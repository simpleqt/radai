"""Bounded executor for synchronous application dependencies."""

from __future__ import annotations

import asyncio
import contextvars
import os
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future
from functools import partial
from typing import Any, TypeVar
from weakref import WeakKeyDictionary

T = TypeVar("T")

_MAX_WORKERS = max(1, int(os.getenv("AGENT_BLOCKING_MAX_WORKERS", "8") or "8"))
_DEFAULT_TIMEOUT_SECONDS = float(
    os.getenv("AGENT_BLOCKING_TIMEOUT_SECONDS", "120") or "120"
)
_LIMITERS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    WeakKeyDictionary()
)
_EXECUTOR: ApplicationBlockingExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_LIMITER_RELEASE_TASKS: set[asyncio.Task[Any]] = set()


class ApplicationBlockingExecutor(Executor):
    """Small lifecycle-owned pool whose workers never block interpreter exit."""

    _STOP = object()

    def __init__(self, *, max_workers: int, thread_name_prefix: str):
        self._queue: queue.Queue[tuple[Future[Any], Callable[[], Any]] | object] = (
            queue.Queue()
        )
        self._shutdown = False
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"{thread_name_prefix}-{index}",
                daemon=True,
            )
            for index in range(max_workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, fn, /, *args, **kwargs):
        future: Future[Any] = Future()
        call = partial(fn, *args, **kwargs)
        with self._lock:
            if self._shutdown:
                raise RuntimeError("blocking executor has been shut down")
            self._queue.put((future, call))
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not self._STOP:
                        item[0].cancel()
            for _ in self._threads:
                self._queue.put(self._STOP)
        if wait:
            for thread in self._threads:
                thread.join()

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._STOP:
                return
            future, call = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = call()
            except BaseException as exc:  # noqa: BLE001
                try:
                    future.set_exception(exc)
                except BaseException:  # Future callbacks must not terminate the worker.
                    pass
            else:
                try:
                    future.set_result(result)
                except BaseException:  # Future callbacks must not terminate the worker.
                    pass


def initialize_blocking_executor(
    *,
    max_workers: int | None = None,
) -> ApplicationBlockingExecutor:
    """Create the application-owned blocking executor once."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ApplicationBlockingExecutor(
                max_workers=max_workers or _MAX_WORKERS,
                thread_name_prefix="agent-blocking",
            )
        return _EXECUTOR


def shutdown_blocking_executor() -> None:
    """Stop accepting work without waiting for running calls on the event loop."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        executor, _EXECUTOR = _EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _get_limiter(loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
    limiter = _LIMITERS.get(loop)
    if limiter is None:
        limiter = asyncio.Semaphore(_MAX_WORKERS)
        _LIMITERS[loop] = limiter
    return limiter


async def _await_concurrent_future(future: Future[T]) -> T:
    return await asyncio.shield(asyncio.wrap_future(future))


async def _release_limiter_when_done(
    future: Future[Any], limiter: asyncio.Semaphore
) -> None:
    """Keep capacity reserved while an already-running timed-out call drains."""
    try:
        await asyncio.shield(asyncio.wrap_future(future))
    except BaseException:
        # The original caller owns the result. This waiter only releases the
        # capacity slot after the underlying thread has actually stopped.
        pass
    finally:
        limiter.release()


async def run_blocking(
    func: Callable[..., T],
    /,
    *args: Any,
    timeout_seconds: float | None = None,
    **kwargs: Any,
) -> T:
    """Run blocking work in the application pool with cancellation and timeout."""
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    timeout = (
        _DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    limiter = _get_limiter(loop)
    started_at = loop.time()
    if timeout > 0:
        await asyncio.wait_for(limiter.acquire(), timeout=timeout)
    else:
        await limiter.acquire()

    concurrent_future: Future[T] | None = None
    try:
        concurrent_future = initialize_blocking_executor().submit(
            context.run,
            func,
            *args,
            **kwargs,
        )
        try:
            if timeout <= 0:
                return await _await_concurrent_future(concurrent_future)
            remaining = timeout - (loop.time() - started_at)
            if remaining <= 0:
                raise asyncio.TimeoutError  # noqa: UP041 -- Python 3.10 compatibility
            return await asyncio.wait_for(
                _await_concurrent_future(concurrent_future),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:  # noqa: UP041 -- Python 3.10 compatibility
            concurrent_future.cancel()
            raise TimeoutError(
                f"blocking call timed out after {timeout:.3f}s"
            ) from exc
        except asyncio.CancelledError:
            concurrent_future.cancel()
            raise
    finally:
        if concurrent_future is None or concurrent_future.done():
            limiter.release()
        else:
            task = asyncio.create_task(
                _release_limiter_when_done(concurrent_future, limiter)
            )
            _LIMITER_RELEASE_TASKS.add(task)
            task.add_done_callback(_LIMITER_RELEASE_TASKS.discard)
