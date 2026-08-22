"""流式运行时生命周期:事件等待/取消、迭代器关闭与 agent 运行时收尾(拆自 streaming_runtime.py)。"""

from __future__ import annotations

import logging

import asyncio
import os

from backend.core.agents.control import AgentRuntimeManager
from backend.core.agents.runtime_manager import unregister_agent_runtime_manager
from backend.core.utils.async_tasks import close_async_iterator_safely
from collections.abc import AsyncIterator
from typing import Any


logger = logging.getLogger(__name__)


def _positive_timeout_env(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


_STREAM_ITERATOR_CLEANUP_TIMEOUT_SECONDS = _positive_timeout_env(
    "AGENT_STREAM_ITERATOR_CLEANUP_TIMEOUT_SECONDS",
    5.0,
)


_STREAM_AGENT_DRAIN_TIMEOUT_SECONDS = _positive_timeout_env(
    "AGENT_STREAM_DRAIN_TIMEOUT_SECONDS",
    15.0,
)


# Compatibility knob retained for deployments/tests that patched the former
# single cleanup timeout. New deployments should use the two scoped settings.
_STREAM_CLEANUP_TIMEOUT_SECONDS = _STREAM_AGENT_DRAIN_TIMEOUT_SECONDS


def _iterator_cleanup_timeout() -> float:
    return min(
        _STREAM_ITERATOR_CLEANUP_TIMEOUT_SECONDS,
        _STREAM_CLEANUP_TIMEOUT_SECONDS,
    )


def _agent_drain_timeout() -> float:
    return min(_STREAM_AGENT_DRAIN_TIMEOUT_SECONDS, _STREAM_CLEANUP_TIMEOUT_SECONDS)


def _optional_budget_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _optional_budget_float(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


async def wait_for_stream_event(
    event_iter: AsyncIterator[dict[str, Any]],
    pending_task: asyncio.Task[dict[str, Any]] | None,
    *,
    timeout: float,
) -> tuple[dict[str, Any] | None, asyncio.Task[dict[str, Any]] | None]:
    """Wait for one event without cancelling the upstream iterator on heartbeat."""
    task = pending_task or asyncio.create_task(event_iter.__anext__())
    created_here = pending_task is None
    try:
        event = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    # Python 3.10 keeps asyncio.TimeoutError distinct from builtin TimeoutError.
    except asyncio.TimeoutError:  # noqa: UP041
        return None, task
    except asyncio.CancelledError:
        # 请求在等待事件期间被取消:shield 让本次新建的 __anext__ 任务继续存活,
        # 而调用方拿不到它的引用(返回赋值未发生),随后迭代器关闭因 ag_running
        # =True 被跳过 → 孤儿任务继续驱动图烧配额。仅回收本调用新建的任务
        # (调用方传入的 pending_task 由调用方自行持有清理),取消它让底层生成器
        # 就地解栈收尾后再向上传播取消。
        if created_here and not task.done():
            from contextlib import suppress

            with suppress(asyncio.CancelledError):
                await cancel_pending_stream_event(task)
        raise
    return event, None


async def cancel_pending_stream_event(
    task: asyncio.Task[Any] | None,
) -> bool:
    """Request cancellation without letting an uncooperative read block cleanup."""

    if task is None:
        return True
    if not task.done():
        task.cancel()
        done, _ = await asyncio.wait(
            {task},
            timeout=_iterator_cleanup_timeout(),
        )
        if not done:
            logger.warning("取消待处理事件读取任务超时，继续执行根请求清理")
            task.add_done_callback(_consume_background_task_result)
            return False
    _consume_background_task_result(task)
    return True


def _consume_background_task_result(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    except Exception:
        logger.debug("后台流任务清理时忽略异常", exc_info=True)


async def close_stream_event_iterator(
    event_iter: Any,
    *,
    active_read_task: asyncio.Task[Any] | None = None,
) -> bool:
    """Bound ``aclose`` so root lifecycle cleanup cannot be held indefinitely."""

    return await close_async_iterator_safely(
        event_iter,
        active_read_task=active_read_task,
        timeout=_iterator_cleanup_timeout(),
    )


async def drain_agent_control(
    control: AgentRuntimeManager,
    *,
    label: str,
) -> bool:
    """Bound root teardown and report descendants that outlive the request."""

    try:
        await control.wait(timeout=_agent_drain_timeout())
        return True
    except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
        try:
            await control.interrupt(
                control.root_agent_id,
                reason="agent runtime drain timeout",
            )
        except BaseException:
            logger.exception("[%s] 二次取消未排空子智能体失败", label)
        remaining = [info for info in await control.list() if not info.status.terminal]
        logger.warning(
            "[%s] 根请求清理等待子智能体退出超时: %s",
            label,
            [
                {
                    "agent_id": info.agent_id,
                    "parent_id": info.parent_id,
                    "status": info.status.value,
                }
                for info in remaining
            ],
        )
        return False


async def close_owned_agent_runtime(
    manager: AgentRuntimeManager,
    *,
    label: str,
    status: str,
    error: str | None,
    suppress_errors: bool,
    requested_cancelled: bool | None = None,
    cancellation_reason: str | None = None,
) -> None:
    """Interrupt, drain, and durably finalize one owned request tree."""

    try:
        if requested_cancelled is None:
            requested_cancelled = bool(manager.cancellation.cancelled)
        if cancellation_reason is None:
            cancellation_reason = manager.cancellation.reason
        failures: list[BaseException] = []
        drained = True
        try:
            await manager.interrupt(
                manager.root_agent_id,
                reason="root request stream closed",
            )
        except BaseException as exc:
            failures.append(exc)
            logger.exception("[%s] 中断根 AgentRuntimeManager 失败", label)
        try:
            drained = await drain_agent_control(manager, label=label)
        except BaseException as exc:
            failures.append(exc)
            drained = False
            logger.exception("[%s] 等待 AgentRuntimeManager 排空失败", label)

        final_status = status
        final_error = error
        if status == "completed" and requested_cancelled:
            final_status = "cancelled"
            final_error = cancellation_reason or "request cancelled"
        if status == "completed" and not drained:
            final_status = "interrupted"
            final_error = "runtime manager drain timed out"
        finalize = getattr(manager, "finalize_run", None)
        if callable(finalize):
            try:
                await finalize(final_status, error=final_error)
            except BaseException as exc:
                failures.append(exc)
                logger.exception("[%s] 持久化 AgentRuntimeManager 终态失败", label)
        if failures and not suppress_errors:
            raise failures[0]
    finally:
        unregister_agent_runtime_manager(manager.root_agent_id, manager)
