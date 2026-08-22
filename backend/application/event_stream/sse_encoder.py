"""SSE encoder for AgentEvent streams."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Literal

from backend.application.agent_errors import public_agent_error
from backend.application.agent_executors.image_url_registry import (
    StreamingTokenResolver,
    get_registry,
    resolve_image_tokens,
)
from backend.application.agent_trace import (
    AgentTraceRecorder,
    create_agent_trace_store,
)
from backend.application.image_pipeline import sanitize_answer_image_urls
from backend.core.utils.async_tasks import (
    consume_background_task_result,
    task_is_cancelling,
)
from backend.domain.agents.events import AgentEvent

logger = logging.getLogger(__name__)


def _producer_cleanup_timeout_seconds() -> float:
    try:
        return max(
            0.1,
            float(os.getenv("SSE_PRODUCER_CLEANUP_TIMEOUT_SECONDS", "5") or "5"),
        )
    except (TypeError, ValueError):
        return 5.0


class SSEEncoder:
    def __init__(
        self,
        *,
        heartbeat_interval_seconds: float = 15.0,
        queue_maxsize: int = 64,
        trace_recorder: AgentTraceRecorder | None = None,
        trace_enabled: bool | None = None,
        trace_root: str | None = None,
    ):
        self._heartbeat_interval_seconds = max(0.001, float(heartbeat_interval_seconds))
        self._queue_maxsize = max(1, int(queue_maxsize))
        self._trace_recorder = trace_recorder
        self._trace_enabled = (
            _trace_env_enabled()
            if trace_enabled is None
            else bool(trace_enabled)
        )
        self._trace_root = trace_root or os.getenv("AGENT_TRACE_ROOT") or None
        self._trace_recorders: dict[str, AgentTraceRecorder] = {}

    def encode(self, event: AgentEvent) -> str:
        event = self._normalize_event_images(event, stream_resolver=None)
        payload = event.to_sse_payload()
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def encode_stream(
        self,
        events: AsyncIterator[AgentEvent],
    ) -> AsyncIterator[str]:
        queue: asyncio.Queue[tuple[Literal["event", "done", "error"], object]] = asyncio.Queue(
            maxsize=self._queue_maxsize
        )
        last_event: AgentEvent | None = None
        terminal_marker_emitted = False
        # Seed the ContextVar before spawning the producer. The upstream runtime
        # resets and populates the same registry object, while this consumer uses
        # it to resolve message.replace payloads before they reach the browser.
        get_registry()
        chunk_image_resolver = StreamingTokenResolver()

        async def produce_events() -> None:
            try:
                async for event in events:
                    await queue.put(("event", event))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put(("error", exc))
            finally:
                current = asyncio.current_task()
                if not task_is_cancelling(current):
                    await queue.put(("done", None))

        # A single producer task owns the upstream iterator for its whole
        # lifetime. This keeps ContextVar set/reset operations in one context.
        producer_task = asyncio.create_task(produce_events())
        try:
            while True:
                try:
                    item_type, payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=self._heartbeat_interval_seconds,
                    )
                except asyncio.TimeoutError:  # noqa: UP041 -- Python 3.10 compatibility
                    # SSE comments are ignored by clients but keep proxies and browsers
                    # from treating a long model/tool call as an idle connection.
                    yield ": keep-alive\n\n"
                    continue

                if item_type == "done":
                    flushed = chunk_image_resolver.flush()
                    if flushed:
                        anchor = last_event or AgentEvent(
                            type="message.delta",
                            request_id="",
                            thread_id="",
                            sequence=0,
                            payload={},
                        )
                        flush_event = replace(
                            anchor,
                            type="message.delta",
                            payload={"content": flushed},
                        )
                        yield self.encode(flush_event)
                    return
                if item_type == "error":
                    assert isinstance(payload, BaseException)
                    if terminal_marker_emitted:
                        logger.error(
                            "SSE 上游在 done 事件后的收尾阶段失败",
                            exc_info=(type(payload), payload, payload.__traceback__),
                        )
                        continue
                    public_message, error_code = public_agent_error(payload)
                    error_event = AgentEvent(
                        type="error",
                        request_id=last_event.request_id if last_event else "",
                        thread_id=last_event.thread_id if last_event else "",
                        sequence=(last_event.sequence + 1) if last_event else 0,
                        payload={
                            "message": public_message,
                            "error": public_message,
                            "error_code": error_code,
                        },
                    )
                    self._record_sse_event(error_event)
                    yield self.encode(error_event)
                    done_event = AgentEvent(
                        type="done",
                        request_id=error_event.request_id,
                        thread_id=error_event.thread_id,
                        sequence=error_event.sequence + 1,
                        payload={},
                    )
                    self._record_sse_event(done_event)
                    yield self.encode(done_event)
                    yield "data: [DONE]\n\n"
                    return

                event = payload
                assert isinstance(event, AgentEvent)
                event = self._normalize_event_images(
                    event,
                    stream_resolver=chunk_image_resolver,
                )
                if event is None:
                    continue
                if event.type == "done":
                    if terminal_marker_emitted:
                        continue
                    flushed = chunk_image_resolver.flush()
                    if flushed:
                        flush_event = replace(
                            event,
                            type="message.delta",
                            payload={"content": flushed},
                        )
                        self._record_sse_event(flush_event)
                        yield self.encode(flush_event)
                    last_event = event
                    self._record_sse_event(event)
                    yield self.encode(event)
                    yield "data: [DONE]\n\n"
                    # Do not return yet. The upstream async generator performs
                    # persistence (including the conversation image registry)
                    # after yielding its terminal event. Keep consuming until
                    # it naturally finishes so its ``finally`` block is not
                    # cancelled before that state is committed.
                    terminal_marker_emitted = True
                    continue
                last_event = event
                self._record_sse_event(event)
                yield self.encode(event)
        finally:
            if last_event is not None and last_event.request_id:
                self._trace_recorders.pop(last_event.request_id, None)
            if not producer_task.done():
                producer_task.cancel()
            done, _ = await asyncio.wait(
                {producer_task},
                timeout=_producer_cleanup_timeout_seconds(),
            )
            if not done:
                producer_task.add_done_callback(consume_background_task_result)
                logger.warning("SSE 上游生产任务取消超时，停止等待回收")
            else:
                with contextlib.suppress(
                    asyncio.CancelledError,
                    StopAsyncIteration,
                ):
                    producer_task.result()

    def _record_sse_event(self, event: AgentEvent) -> None:
        recorder = self._trace_recorder
        if recorder is None and self._trace_enabled and event.request_id:
            recorder = self._trace_recorders.get(event.request_id)
            if recorder is None:
                recorder = AgentTraceRecorder(
                    run_id=event.request_id,
                    workflow_name="sse",
                    enabled=True,
                    store=create_agent_trace_store(
                        self._trace_root,
                        backend="jsonl" if self._trace_root else None,
                    ),
                )
                self._trace_recorders[event.request_id] = recorder
        if recorder is None:
            return
        recorder.record_agent_event(
            replace(
                event,
                type=f"sse.{event.type}",
                payload=event.to_sse_payload(),
            ),
            producer="agent-sse",
        )

    def _normalize_event_images(
        self,
        event: AgentEvent,
        *,
        stream_resolver: StreamingTokenResolver | None,
    ) -> AgentEvent | None:
        payload = dict(event.payload or {})
        content = payload.get("content")

        if event.type in {"message.delta", "research.draft.delta"} and isinstance(content, str):
            if stream_resolver is not None:
                content = stream_resolver.feed(content)
            else:
                content = resolve_image_tokens(content, promote_standalone=False)
            if not content:
                return None
            payload["content"] = content
            return replace(event, payload=payload)

        if event.type == "message.replace" and isinstance(content, str):
            payload["content"] = sanitize_answer_image_urls(resolve_image_tokens(content))
            return replace(event, payload=payload)

        normalized_payload = self._normalize_payload_images(payload)
        if normalized_payload is payload:
            return event
        return replace(event, payload=normalized_payload)

    def _normalize_payload_images(self, value):
        if isinstance(value, str):
            return resolve_image_tokens(value, promote_standalone=False)
        if isinstance(value, list):
            return [self._normalize_payload_images(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._normalize_payload_images(item) for item in value)
        if isinstance(value, dict):
            return {
                key: self._normalize_payload_images(item)
                for key, item in value.items()
            }
        return value


def _trace_env_enabled() -> bool:
    return os.getenv("AGENT_TRACE_SSE_ENABLED", os.getenv("AGENT_TRACE_ENABLED", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
