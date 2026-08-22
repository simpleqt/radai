"""Unified async execution boundary for request-scoped tools."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import queue
import threading
import weakref
from collections.abc import Awaitable, Callable, Mapping
from time import monotonic
from typing import Any

from backend.application.tools.runtime_state import tool_runtime_state
from backend.config.loggers import get_logger

logger = get_logger(__name__)

_CONCURRENCY_LOCK = threading.Lock()
_CONCURRENCY_LIMITERS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[tuple[str, int], asyncio.Semaphore],
] = weakref.WeakKeyDictionary()
_RESULT_CACHE_LOCK = threading.Lock()
_RESULT_CACHES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[tuple[int, str], tuple[weakref.ReferenceType[Any], float, Any]],
] = weakref.WeakKeyDictionary()
_CACHE_MISS = object()


def _sync_timeout_worker_limit() -> int:
    try:
        return max(1, min(256, int(os.getenv("SYNC_TOOL_TIMEOUT_MAX_THREADS", "16"))))
    except (TypeError, ValueError):
        return 16


_SYNC_TIMEOUT_SLOTS = threading.BoundedSemaphore(_sync_timeout_worker_limit())


class ToolReportedError(RuntimeError):
    """A provider returned an explicit error payload instead of raising."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "internal_error",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = _normalize_reported_error_code(error_code)
        self.details = dict(details or {})


def tool_metadata(tool_obj: Any | None) -> dict[str, Any]:
    metadata = (
        tool_obj if isinstance(tool_obj, dict) else getattr(tool_obj, "metadata", None)
    )
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def tool_timeout_seconds(tool_name: str, tool_obj: Any | None = None) -> float:
    metadata = tool_metadata(tool_obj)
    if metadata:
        try:
            configured = metadata.get("timeout_seconds")
            if configured is not None:
                value = float(configured)
                if math.isfinite(value):
                    return max(1.0, value)
        except (TypeError, ValueError):
            pass
    normalized = str(tool_name or "").upper().replace("-", "_")
    raw = os.getenv(
        f"AGENT_TOOL_TIMEOUT_{normalized}_SECONDS",
        os.getenv("AGENT_TOOL_TIMEOUT_SECONDS", "180"),
    )
    try:
        value = float(raw)
        return max(1.0, value) if math.isfinite(value) else 180.0
    except (TypeError, ValueError):
        return 180.0


async def invoke_tool_with_timeout(
    tool_obj: Any,
    args: dict[str, Any],
    *,
    tool_name: str | None = None,
    cache_scope: str | None = None,
) -> Any:
    """Invoke a service tool with timeout, bounded concurrency, and safe retries."""
    return await invoke_service_call(
        tool_obj,
        lambda: tool_obj.ainvoke(dict(args)),
        tool_name=tool_name,
        cache_key_payload=(
            {"scope": cache_scope, "args": args}
            if cache_scope is not None
            else args
        ),
    )


async def invoke_service_call(
    tool_obj: Any,
    call: Callable[[], Awaitable[Any]],
    *,
    tool_name: str | None = None,
    cache_key_payload: Any | None = None,
    cache_hit_transform: Callable[[Any], Any] | None = None,
    enforce_output_limit: bool = True,
) -> Any:
    """Apply the common online-service policy to an arbitrary async tool call."""

    name = tool_name or str(getattr(tool_obj, "name", "tool") or "tool")
    timeout = tool_timeout_seconds(name, tool_obj)
    metadata = tool_metadata(tool_obj)
    idempotent = bool(metadata.get("idempotent", True))
    cache_enabled = bool(metadata.get("cache_enabled", False)) and idempotent
    cache_ttl_seconds = _bounded_int(
        metadata.get("cache_ttl_seconds"),
        default=0,
        minimum=0,
        maximum=86400,
    )
    cache_key = (
        _service_cache_key(name, cache_key_payload)
        if cache_enabled and cache_ttl_seconds > 0 and cache_key_payload is not None
        else None
    )
    if cache_key is not None:
        cached = _service_cache_get(tool_obj, cache_key)
        if cached is not _CACHE_MISS:
            tool_runtime_state.record_call(
                name,
                status="success",
                attempts=0,
                elapsed_ms=0.0,
                queue_ms=0.0,
            )
            tool_runtime_state.record_health(name, status="ready")
            return cache_hit_transform(cached) if cache_hit_transform else cached
    configured_attempts = _bounded_int(
        metadata.get("max_attempts"), default=1, minimum=1, maximum=5
    )
    max_attempts = configured_attempts if idempotent else 1
    retryable_errors = {
        str(value).strip().lower()
        for value in metadata.get("retryable_errors", [])
        if str(value).strip()
    }
    parallel_safe = bool(metadata.get("parallel_safe", True))
    max_concurrency = _bounded_int(
        metadata.get("max_concurrency"),
        default=1 if not parallel_safe else 64,
        minimum=1,
        maximum=256,
    )
    if not parallel_safe:
        max_concurrency = 1
    limiter = _tool_limiter(name, max_concurrency)
    queued_at = monotonic()
    acquired = False
    attempts = 0
    try:
        try:
            await asyncio.wait_for(limiter.acquire(), timeout=timeout)
            acquired = True
        except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041 -- Python 3.10
            error = TimeoutError(
                f"Tool '{name}' timed out waiting for an execution slot after {timeout:.0f}s"
            )
            tool_runtime_state.record_call(
                name,
                status="error",
                attempts=1,
                elapsed_ms=(monotonic() - queued_at) * 1000,
                queue_ms=(monotonic() - queued_at) * 1000,
                error_code="timeout",
            )
            tool_runtime_state.record_health(
                name,
                status="degraded",
                reason="timeout",
                detail=type(error).__name__,
            )
            raise error from exc
        started_at = monotonic()
        queue_ms = (started_at - queued_at) * 1000
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                result = await asyncio.wait_for(call(), timeout=timeout)
                raise_for_tool_reported_error(result)
                if enforce_output_limit:
                    result = _bound_tool_result(
                        result,
                        _optional_positive_int(metadata.get("max_output_chars")),
                    )
                tool_runtime_state.record_call(
                    name,
                    status="success",
                    attempts=attempts,
                    elapsed_ms=(monotonic() - started_at) * 1000,
                    queue_ms=queue_ms,
                )
                tool_runtime_state.record_health(name, status="ready")
                if cache_key is not None:
                    _service_cache_put(
                        tool_obj,
                        cache_key,
                        result,
                        ttl_seconds=cache_ttl_seconds,
                    )
                return result
            except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041 -- Python 3.10
                error: Exception = TimeoutError(
                    f"Tool '{name}' timed out after {timeout:.0f}s"
                )
                error.__cause__ = exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = exc

            category = classify_tool_error(error)
            if attempt >= max_attempts or category not in retryable_errors:
                tool_runtime_state.record_call(
                    name,
                    status="error",
                    attempts=attempts,
                    elapsed_ms=(monotonic() - started_at) * 1000,
                    queue_ms=queue_ms,
                    error_code=category,
                )
                tool_runtime_state.record_health(
                    name,
                    status="degraded",
                    reason=category,
                    detail=type(error).__name__,
                )
                raise error
            await asyncio.sleep(min(2.0, 0.1 * (2 ** (attempt - 1))))
    finally:
        if acquired:
            limiter.release()

    raise RuntimeError(f"Tool '{name}' execution ended unexpectedly")


def classify_tool_error(error: BaseException) -> str:
    explicit_code = str(getattr(error, "error_code", "") or "").strip().lower()
    if explicit_code:
        return explicit_code
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "connection"
    name = type(error).__name__.lower()
    message = str(error).lower()
    if "rate" in name or "rate limit" in message or "429" in message:
        return "rate_limited"
    if "unavailable" in name or "unavailable" in message or "503" in message:
        return "unavailable"
    if isinstance(error, (TypeError, ValueError)):
        return "invalid_arguments"
    if isinstance(error, PermissionError):
        return "not_authorized"
    if isinstance(error, FileNotFoundError):
        return "resource_not_found"
    return "internal_error"


def raise_for_tool_reported_error(value: Any) -> None:
    """Turn JSON/status error results into exceptions at the execution boundary."""

    message_value = getattr(value, "content", value)
    message_status = str(getattr(value, "status", "") or "").strip().lower()
    parsed = message_value
    if isinstance(message_value, str):
        try:
            parsed = json.loads(message_value)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = message_value
    if not isinstance(parsed, dict):
        return

    payload_status = str(parsed.get("status") or "").strip().lower()
    error_value = parsed.get("error")
    if not error_value and message_status != "error" and payload_status not in {
        "error",
        "failed",
        "failure",
    }:
        return

    error_code = "internal_error"
    error_message = "tool provider reported an error"
    if isinstance(error_value, dict):
        error_code = str(error_value.get("code") or error_code)
        error_message = str(
            error_value.get("message")
            or parsed.get("message")
            or error_code
        )
    elif error_value:
        raw_error = str(error_value).strip()
        error_code = raw_error if _looks_like_error_code(raw_error) else error_code
        error_message = str(parsed.get("message") or raw_error)
    else:
        error_code = str(parsed.get("code") or error_code)
        error_message = str(
            parsed.get("message")
            or parsed.get("summary")
            or error_message
        )
    if str(parsed.get("query_kind") or "").strip().lower() == "validation_error":
        error_code = "invalid_arguments"
    details = {
        key: item
        for key, item in parsed.items()
        if key not in {"status", "error", "message", "summary"}
    }
    if isinstance(error_value, dict):
        nested_details = error_value.get("details")
        if isinstance(nested_details, Mapping):
            details.update(nested_details)
    raise ToolReportedError(
        error_message,
        error_code=error_code,
        details=details,
    )


def _normalize_reported_error_code(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "permission_denied": "not_authorized",
        "unauthorized": "not_authorized",
        "forbidden": "not_authorized",
        "not_found": "resource_not_found",
        "file_not_found": "resource_not_found",
        "unknown_project_table": "invalid_arguments",
        "validation_error": "invalid_arguments",
        "rate_limit": "rate_limited",
        "too_many_requests": "rate_limited",
    }
    return aliases.get(normalized, normalized or "internal_error")


def _looks_like_error_code(value: str) -> bool:
    return bool(value) and len(value) <= 80 and all(
        char.isalnum() or char in {"_", "-", "."} for char in value
    )


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bound_tool_result(value: Any, limit: int | None) -> Any:
    if limit is None:
        return value
    if isinstance(value, str):
        return _bound_serialized_text(value, limit)
    if isinstance(value, (dict, list, tuple)):
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        if len(serialized) <= limit:
            return value
        bounded = _bound_serialized_text(serialized, limit)
        try:
            return json.loads(bounded)
        except (TypeError, ValueError, json.JSONDecodeError):
            return bounded

    # LangGraph 工具（如 search_question_bank）返回 Command 而非裸值：上限
    # 必须作用于 update["messages"] 里各消息的 content，否则 max_output_chars
    # 对这类工具是无效声明。
    try:
        from langgraph.types import Command as _LangGraphCommand
    except ImportError:  # pragma: no cover - langgraph 是核心依赖
        _LangGraphCommand = None
    if _LangGraphCommand is not None and isinstance(value, _LangGraphCommand):
        return _bound_command_result(value, limit)

    content = getattr(value, "content", None)
    if not isinstance(content, str) or len(content) <= limit:
        return value
    bounded_content = _bound_serialized_text(content, limit)
    model_copy = getattr(value, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"content": bounded_content})
    return value


def _bound_command_result(command: Any, limit: int) -> Any:
    import dataclasses

    update = getattr(command, "update", None)
    if not isinstance(update, dict):
        return command
    messages = update.get("messages")
    if not isinstance(messages, list):
        return command
    bounded_messages = [_bound_tool_result(item, limit) for item in messages]
    if all(
        new is old
        for new, old in zip(bounded_messages, messages, strict=False)
    ):
        return command
    new_update = dict(update)
    new_update["messages"] = bounded_messages
    try:
        return dataclasses.replace(command, update=new_update)
    except TypeError:
        return command


def _bound_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "...[truncated]"
    return value[: max(0, limit - len(marker))] + marker


def _bound_serialized_text(value: str, limit: int) -> str:
    """Keep oversized JSON results valid while still enforcing the hard limit."""

    if len(value) <= limit:
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _bound_text(value, limit)
    if not isinstance(parsed, (dict, list)):
        return _bound_text(value, limit)

    status = parsed.get("status", "success") if isinstance(parsed, dict) else "success"
    envelope = {
        "status": status,
        "summary": "",
        "truncated": True,
    }
    base = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    if len(base) > limit:
        return _bound_text(value, limit)
    envelope["summary"] = _bound_text(value, max(1, limit - len(base)))
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    while len(encoded) > limit and envelope["summary"]:
        overflow = len(encoded) - limit
        envelope["summary"] = envelope["summary"][: -max(1, overflow)]
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return encoded


def _service_cache_key(tool_name: str, payload: Any) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError):
        encoded = repr(payload)
    return f"{tool_name}:{encoded}"


def _service_cache_get(tool_obj: Any, cache_key: str) -> Any:
    loop = asyncio.get_running_loop()
    key = (id(tool_obj), cache_key)
    now = monotonic()
    with _RESULT_CACHE_LOCK:
        loop_cache = _RESULT_CACHES.setdefault(loop, {})
        cached = loop_cache.get(key)
        if cached is None:
            return _CACHE_MISS
        object_ref, expires_at, value = cached
        if object_ref() is not tool_obj or expires_at <= now:
            loop_cache.pop(key, None)
            return _CACHE_MISS
    try:
        return copy.deepcopy(value)
    except Exception:
        logger.debug("工具结果缓存深拷贝失败，丢弃缓存项", exc_info=True)
        with _RESULT_CACHE_LOCK:
            loop_cache = _RESULT_CACHES.get(loop)
            if loop_cache is not None and loop_cache.get(key) is cached:
                loop_cache.pop(key, None)
        return _CACHE_MISS


def _service_cache_put(
    tool_obj: Any,
    cache_key: str,
    value: Any,
    *,
    ttl_seconds: int,
) -> None:
    try:
        object_ref = weakref.ref(tool_obj)
    except TypeError:
        return
    try:
        cached_value = copy.deepcopy(value)
    except (TypeError, ValueError):
        logger.debug("工具结果不可深拷贝，跳过缓存", exc_info=True)
        return
    loop = asyncio.get_running_loop()
    key = (id(tool_obj), cache_key)
    with _RESULT_CACHE_LOCK:
        loop_cache = _RESULT_CACHES.setdefault(loop, {})
        loop_cache[key] = (
            object_ref,
            monotonic() + max(1, ttl_seconds),
            cached_value,
        )


def _tool_limiter(tool_name: str, limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    key = (str(tool_name), int(limit))
    with _CONCURRENCY_LOCK:
        loop_limiters = _CONCURRENCY_LIMITERS.setdefault(loop, {})
        limiter = loop_limiters.get(key)
        if limiter is None:
            limiter = asyncio.Semaphore(limit)
            loop_limiters[key] = limiter
        return limiter


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def invoke_sync_with_timeout(call, *, timeout: float, label: str) -> Any:
    """Run a synchronous tool handler with a bounded wait.

    LangGraph can execute middleware on its synchronous path as well.  A
    daemon thread lets that path return a deterministic timeout without
    blocking the request thread indefinitely.  The underlying operation is
    best-effort cancelled; providers should still honour their own I/O
    cancellation APIs for resource cleanup.
    """
    bounded_timeout = max(1.0, float(timeout))
    if not _SYNC_TIMEOUT_SLOTS.acquire(timeout=min(1.0, bounded_timeout)):
        raise TimeoutError(
            f"Tool '{label}' could not start because timed-out workers are saturated"
        )

    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def runner() -> None:
        try:
            result_queue.put((True, call()))
        except BaseException as exc:  # preserve provider/GraphBubbleUp errors
            result_queue.put((False, exc))
        finally:
            _SYNC_TIMEOUT_SLOTS.release()

    thread = threading.Thread(
        target=runner,
        name=f"tool-timeout-{str(label or 'tool')[:48]}",
        daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        _SYNC_TIMEOUT_SLOTS.release()
        raise
    thread.join(timeout=bounded_timeout)
    if thread.is_alive():
        logger.warning(
            "同步工具调用超时，后台守护线程将继续运行直至调用自行结束: tool=%s",
            label,
        )
        raise TimeoutError(f"Tool '{label}' timed out after {timeout:.0f}s")
    try:
        ok, value = result_queue.get_nowait()
    except queue.Empty as exc:  # pragma: no cover - defensive provider bug
        raise RuntimeError(f"Tool '{label}' completed without a result") from exc
    if ok:
        return value
    raise value
