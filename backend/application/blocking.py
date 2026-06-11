"""Bounded executor for synchronous application dependencies."""

from __future__ import annotations

import asyncio
import os
import queue
import threading
from concurrent.futures import Executor, Future
from functools import partial
from typing import Any, Callable, TypeVar
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
        limiter = asyncio.Semaphore(8)
        _LIMITERS[loop] = limiter
    return limiter


async def _await_concurrent_future(future: Future[T]) -> T:
    while not future.done():
        await asyncio.sleep(0.005)
    return future.result()


async def run_blocking(
    func: Callable[..., T],
    /,
    *args: Any,
    timeout_seconds: float | None = None,
    **kwargs: Any,
) -> T:
    """Run blocking work in the application pool with cancellation and timeout."""
    loop = asyncio.get_running_loop()
    async with _get_limiter(loop):
        concurrent_future = initialize_blocking_executor().submit(
            func,
            *args,
            **kwargs,
        )
        try:
            timeout = (
                _DEFAULT_TIMEOUT_SECONDS
                if timeout_seconds is None
                else timeout_seconds
            )
            if timeout <= 0:
                return await _await_concurrent_future(concurrent_future)
            return await asyncio.wait_for(
                _await_concurrent_future(concurrent_future),
                timeout=timeout,
            )
        except (asyncio.CancelledError, TimeoutError):
            concurrent_future.cancel()
            raise
