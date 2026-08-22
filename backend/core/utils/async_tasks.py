"""Small helpers for safe asyncio task lifecycle checks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, TypeVar

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


async def iterate_until_disconnected(
    iterator: AsyncIterator[_T],
    request: Any,
    *,
    poll_interval: float = 0.25,
) -> AsyncIterator[_T]:
    """Advance an upstream iterator while independently polling disconnects."""
    active_read: asyncio.Task[_T] | None = None
    try:
        while True:
            if await request.is_disconnected():
                return
            active_read = asyncio.create_task(anext(iterator))
            while True:
                done, _ = await asyncio.wait(
                    {active_read},
                    timeout=max(0.01, poll_interval),
                )
                if done:
                    break
                if await request.is_disconnected():
                    active_read.cancel()
                    await asyncio.gather(active_read, return_exceptions=True)
                    active_read = None
                    return
            try:
                item = active_read.result()
            except StopAsyncIteration:
                return
            active_read = None
            yield item
    finally:
        if active_read is not None and not active_read.done():
            active_read.cancel()
            await asyncio.gather(active_read, return_exceptions=True)
        await close_async_iterator_safely(
            iterator,
            active_read_task=active_read,
        )


def task_is_cancelling(task: asyncio.Task | None) -> bool:
    """Return whether cancellation has been requested or completed for a task."""
    if task is None:
        return False
    cancelling = getattr(task, "cancelling", None)
    if callable(cancelling):
        return bool(cancelling())
    return task.cancelled()


def consume_background_task_result(task: asyncio.Future[Any]) -> None:
    """Observe a detached cleanup task so asyncio never reports it as lost."""

    try:
        task.result()
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    except Exception:
        logger.warning("后台异步清理任务失败", exc_info=True)


async def reap_task_bounded(
    task: asyncio.Future[Any] | None,
    *,
    timeout: float = 1.0,
) -> bool:
    """Cancel a still-running task and observe it within a bounded window.

    这是流式管线里反复手写的「cancel + 有界 wait + done 回调观察」模式的
    共享实现:清理路径绝不无限等待一个可能阻塞在满队列 put/未响应取消的
    任务。已完成的任务原样返回(其结果/异常由调用方业务逻辑消费,这里
    不重复读取以免产生虚假的清理告警)。

    Returns True when the task finished (or was ``None``); False when the
    cancellation did not land within *timeout* — a done-callback will observe
    the eventual result so asyncio never reports it as lost.
    """

    if task is None or task.done():
        return True
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
    if not done:
        task.add_done_callback(consume_background_task_result)
        return False
    consume_background_task_result(task)
    return True


async def close_async_iterator_safely(
    iterator: Any,
    *,
    active_read_task: asyncio.Task[Any] | None = None,
    timeout: float = 1.5,
) -> bool:
    """Close an async iterator without racing an in-flight ``__anext__``.

    Async generators reject ``aclose`` while another task is still executing
    ``__anext__``.  If cancellation of that read is not yet complete, register
    a one-shot deferred close instead of racing it during request teardown.
    """

    if iterator is None or not hasattr(iterator, "aclose"):
        return True
    if active_read_task is not None and not active_read_task.done():
        loop = asyncio.get_running_loop()
        close_scheduled = False

        def close_after_read(_completed: asyncio.Future[Any]) -> None:
            nonlocal close_scheduled
            if close_scheduled or loop.is_closed():
                return
            close_scheduled = True
            close_task = loop.create_task(
                close_async_iterator_safely(iterator, timeout=timeout)
            )
            close_task.add_done_callback(consume_background_task_result)

        active_read_task.add_done_callback(close_after_read)
        return False

    # Native async generators expose ``ag_running``.  A true value here means
    # another owner is already advancing or closing the generator; issuing a
    # second ``aclose`` is exactly what raises
    # ``aclose(): asynchronous generator is already running``.
    if bool(getattr(iterator, "ag_running", False)):
        logger.debug("异步生成器仍由其他任务运行，跳过重复关闭")
        return False

    try:
        close_task = asyncio.ensure_future(iterator.aclose())
    except Exception:
        logger.warning("创建异步迭代器关闭任务失败", exc_info=True)
        return False
    # Register the observer immediately.  The caller itself may be cancelled
    # while awaiting cleanup; without an eager callback an implicit
    # ``async_generator_athrow`` task can later fail as an unretrieved task.
    close_task.add_done_callback(consume_background_task_result)
    done, _ = await asyncio.wait({close_task}, timeout=max(0.0, timeout))
    if not done:
        close_task.cancel()
        return False
    try:
        close_task.result()
    except (asyncio.CancelledError, StopAsyncIteration):
        return True
    except Exception:
        logger.warning("关闭异步迭代器失败", exc_info=True)
        return False
    return True
