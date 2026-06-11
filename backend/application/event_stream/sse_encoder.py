"""SSE encoder for AgentEvent streams."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import AsyncIterator, Literal

from backend.domain.agents.events import AgentEvent


class SSEEncoder:
    def __init__(self, *, heartbeat_interval_seconds: float = 15.0):
        self._heartbeat_interval_seconds = max(0.001, float(heartbeat_interval_seconds))

    def encode(self, event: AgentEvent) -> str:
        payload = event.to_sse_payload()
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def encode_stream(
        self,
        events: AsyncIterator[AgentEvent],
    ) -> AsyncIterator[str]:
        queue: asyncio.Queue[tuple[Literal["event", "done", "error"], object]] = asyncio.Queue()

        async def produce_events() -> None:
            try:
                async for event in events:
                    await queue.put(("event", event))
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await queue.put(("error", exc))
            finally:
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
                except asyncio.TimeoutError:
                    # SSE comments are ignored by clients but keep proxies and browsers
                    # from treating a long model/tool call as an idle connection.
                    yield ": keep-alive\n\n"
                    continue

                if item_type == "done":
                    return
                if item_type == "error":
                    assert isinstance(payload, BaseException)
                    raise payload

                event = payload
                assert isinstance(event, AgentEvent)
                yield self.encode(event)
                if event.type == "done":
                    yield "data: [DONE]\n\n"
                    return
        finally:
            if not producer_task.done():
                producer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await producer_task
