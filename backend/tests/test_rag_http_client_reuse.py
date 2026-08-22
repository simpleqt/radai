from __future__ import annotations

import pytest

from backend.application.tools.providers import multi_kb_search_tool as tool


@pytest.mark.asyncio
async def test_rag_http_client_reused_within_same_event_loop() -> None:
    first = tool._get_rag_http_client("http://knowledge:8086", 30.0)
    second = tool._get_rag_http_client("http://knowledge:8086", 30.0)

    assert first is second

    await tool.close_rag_http_clients()
    assert first.is_closed


@pytest.mark.asyncio
async def test_kb_search_semaphore_is_loop_local_and_bounded() -> None:
    first = tool._get_kb_search_semaphore()
    second = tool._get_kb_search_semaphore()

    assert first is second
    assert first._value >= 1
