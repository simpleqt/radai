import asyncio

from backend.application.event_stream.sse_encoder import SSEEncoder
from backend.domain.agents.events import AgentEvent


async def test_sse_encoder_waits_for_upstream_finalizer_after_done() -> None:
    finalized = asyncio.Event()

    async def events():
        try:
            yield AgentEvent(
                type="done",
                request_id="req-1",
                thread_id="thread-1",
                sequence=2,
            )
        finally:
            await asyncio.sleep(0)
            finalized.set()

    chunks = [chunk async for chunk in SSEEncoder().encode_stream(events())]

    assert chunks[-1] == "data: [DONE]\n\n"
    assert finalized.is_set()
