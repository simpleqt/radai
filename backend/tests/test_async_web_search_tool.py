import asyncio
import json

import pytest

from backend.application.tools.providers import async_web_search_tool as search_module
from backend.application.agent_executors.stream_handler import NEAgentStreamHandler
from backend.application.agent_executors.stream_utils import (
    extract_web_search_metadata,
    make_process_event,
)
from backend.core.tools.request_limits import (
    reset_tool_budgets,
    reset_web_search_max_results,
    set_tool_budgets,
    set_web_search_max_results,
)


@pytest.mark.asyncio
async def test_execute_web_search_preserves_response_contract(monkeypatch) -> None:
    async def fake_search(*args, **kwargs):
        assert kwargs["isolated"] is True
        return [
            {
                "title": "x-ray logging result",
                "url": "https://example.com",
                "content": "x-ray logging body",
                "provider": "firecrawl",
            }
        ]

    monkeypatch.setattr(search_module, "_search_web_async", fake_search)

    payload = json.loads(
        await search_module.execute_web_search(
            '搜索词： "x-ray logging"',
            max_results=50,
            isolated=True,
        )
    )

    assert payload["engine"] == "firecrawl"
    assert payload["query"] == "x-ray logging"
    assert payload["total_results"] == 1


@pytest.mark.asyncio
async def test_agent_web_search_enforces_runtime_page_limit_and_marks_last_call(
    monkeypatch,
) -> None:
    captured = {}

    async def fake_execute(query, *, max_results, timelimit=None, isolated=False):
        captured.update(
            query=query,
            max_results=max_results,
            timelimit=timelimit,
            isolated=isolated,
        )
        return json.dumps(
            {
                "query": query,
                "engine": "firecrawl",
                "results": [
                    {"title": "Result", "url": "https://example.com"}
                ],
            }
        )

    monkeypatch.setattr(search_module, "execute_web_search", fake_execute)
    budget_token = set_tool_budgets({"web_search": 1})
    result_limit_token = set_web_search_max_results(3)
    try:
        payload = json.loads(
            await search_module.async_web_search_tool.ainvoke(
                {"query": "focused query", "max_results": 5}
            )
        )
    finally:
        reset_web_search_max_results(result_limit_token)
        reset_tool_budgets(budget_token)

    assert captured["max_results"] == 3
    assert payload["retrieval_budget"] == {
        "remaining": 0,
        "exhausted": True,
        "instruction": "停止继续检索，立即基于现有网页证据整理结论。",
    }


def test_firecrawl_engine_is_preserved_in_frontend_event_metadata() -> None:
    metadata = extract_web_search_metadata(
        json.dumps(
            {
                "engine": "firecrawl",
                "query": "focused query",
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.com",
                        "content": "A retrieved page excerpt supporting the result.",
                    }
                ],
            }
        )
    )

    assert metadata["engine"] == "firecrawl"
    assert metadata["query"] == "focused query"
    assert metadata["web_results"] == [
        {
            "title": "Result",
            "url": "https://example.com",
            "domain": "example.com",
            "content_excerpt": "A retrieved page excerpt supporting the result.",
        }
    ]


def test_process_event_identity_distinguishes_search_queries() -> None:
    first = make_process_event(
        kind="tool_result",
        title="结果摘要",
        detail="共 5 条结果，5 个网页来源",
        tool="web_search",
        metadata={"query": "first query"},
    )
    second = make_process_event(
        kind="tool_result",
        title="结果摘要",
        detail="共 5 条结果，5 个网页来源",
        tool="web_search",
        metadata={"query": "second query"},
    )
    retried_first = make_process_event(
        kind="tool_result",
        title="结果摘要",
        detail="共 5 条结果，5 个网页来源",
        tool="web_search",
        metadata={"query": "first query"},
    )

    assert first["id"] != second["id"]
    assert first["id"] == retried_first["id"]


@pytest.mark.asyncio
async def test_concurrent_web_search_results_keep_per_invocation_ids() -> None:
    handler = NEAgentStreamHandler()
    starts = [
        {
            "name": "web_search",
            "run_id": "web-run-1",
            "data": {"input": {"query": "first query"}},
        },
        {
            "name": "web_search",
            "run_id": "web-run-2",
            "data": {"input": {"query": "second query"}},
        },
    ]
    for event in starts:
        _ = [item async for item in handler._on_tool_start(event)]

    async def finish(run_id: str, query: str):
        payload = json.dumps(
            {
                "engine": "firecrawl",
                "query": query,
                "total_results": 1,
                "results": [
                    {
                        "title": f"Page for {query}",
                        "url": f"https://example.com/{run_id}",
                    }
                ],
            }
        )
        events = [
            item
            async for item in handler._on_tool_end(
                {
                    "name": "web_search",
                    "run_id": run_id,
                    "data": {"output": payload},
                }
            )
        ]
        return next(
            item["event"]
            for item in events
            if item.get("event", {}).get("kind") == "tool_result"
        )

    first, second = await asyncio.gather(
        finish("web-run-1", "first query"),
        finish("web-run-2", "second query"),
    )

    assert first["id"] != second["id"]
    assert first["metadata"]["query"] == "first query"
    assert second["metadata"]["query"] == "second query"
    assert first["metadata"]["tool_run_id"] == "web-run-1"
    assert second["metadata"]["tool_run_id"] == "web-run-2"
    assert handler._tool_started_at == {}


@pytest.mark.asyncio
async def test_firecrawl_search_returns_bounded_page_evidence(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Relevant result",
                            "url": "https://example.com/paper",
                            "description": "short description",
                            "markdown": "full page evidence",
                        }
                    ]
                },
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json, headers):
            captured.update(url=url, json=json, headers=headers)
            return FakeResponse()

    monkeypatch.setattr(search_module.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(search_module, "FIRECRAWL_API_URL", "http://firecrawl:3002")
    monkeypatch.setattr(search_module, "FIRECRAWL_API_KEY", "secret")

    results = await search_module._search_firecrawl_async(
        "focused query",
        max_results=3,
        timeout=20,
        timelimit="m",
    )

    assert captured["url"] == "http://firecrawl:3002/v2/search"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["tbs"] == "qdr:m"
    assert captured["json"]["scrapeOptions"]["formats"] == ["markdown"]
    assert results == [
        {
            "title": "Relevant result",
            "url": "https://example.com/paper",
            "content": "full page evidence",
            "provider": "firecrawl",
        }
    ]


@pytest.mark.asyncio
async def test_firecrawl_failure_does_not_fall_back_to_another_provider(
    monkeypatch,
) -> None:
    calls = []

    async def no_firecrawl(*args, **kwargs):
        calls.append("firecrawl")
        return []

    monkeypatch.setattr(search_module, "_search_firecrawl_async", no_firecrawl)

    results = await search_module._search_web_async("focused query")

    assert calls == ["firecrawl", "firecrawl"]
    assert results == []
