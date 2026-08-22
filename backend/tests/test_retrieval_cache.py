import asyncio
import json
from contextvars import ContextVar, copy_context
from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from backend.application.blocking import run_blocking
from backend.application.tools.providers.multi_kb_search_tool import (
    _build_raw_chunk_evidence_records,
    _build_request_cache_key,
    _mark_structured_cache_hit,
)
from backend.core.agents.middlewares.tool_budget_availability import (
    ToolBudgetAvailabilityMiddleware,
)
from backend.core.skills.skill_runtime_state import (
    get_missing_required_skills,
    reset_request_skill_state,
    set_request_skill_state,
)
from backend.core.tools.request_limits import (
    _KB_PROVIDER_CAPACITY,
    RetrievalRequestCache,
    consume_tool_budget,
    reserve_turn_kb_chars,
    reset_tool_budgets,
    reset_turn_kb_context_budget,
    set_tool_budgets,
)


def test_retrieval_request_cache_exact_and_similar_hits():
    cache = RetrievalRequestCache()
    key = "groundwater remediation methods|kb-a|8|5|rrf"
    cache.set_exact(key, "result-a")

    assert cache.get_exact(key) == "result-a"
    assert (
        cache.get_similar("groundwater remediation method|kb-a|8|5|rrf", threshold=0.5)
        == "result-a"
    )
    assert cache.get_similar("basketball rules|kb-a|8|5|rrf", threshold=0.8) is None


def test_exhausted_tool_is_hidden_without_disabling_other_tools():
    middleware = ToolBudgetAvailabilityMiddleware()
    messages = [HumanMessage(content="检索后继续计算")]
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=messages,
        tools=[
            {"type": "function", "function": {"name": "multi_kb_search"}},
            {"type": "function", "function": {"name": "calculator"}},
        ],
        tool_choice="auto",
        state={"messages": messages},
        runtime=None,
    )
    captured = []
    budget_token = set_tool_budgets({"multi_kb_search": 0, "calculator": 1})
    try:
        middleware.wrap_model_call(
            request,
            lambda patched: captured.append(patched)
            or ModelResponse(result=[AIMessage(content="继续回答")]),
        )
    finally:
        reset_tool_budgets(budget_token)

    assert [tool["function"]["name"] for tool in captured[0].tools] == ["calculator"]
    assert captured[0].tool_choice is None


def test_parallel_retrieval_calls_are_deduplicated_and_bounded_before_dispatch():
    middleware = ToolBudgetAvailabilityMiddleware()
    messages = [HumanMessage(content="检索公式并回答")]
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=messages,
        tools=[
            {"type": "function", "function": {"name": "multi_kb_search"}},
            {"type": "function", "function": {"name": "calculator"}},
        ],
        tool_choice="auto",
        state={"messages": messages},
        runtime=None,
    )
    response = ModelResponse(
        result=[
            AIMessage(
                content="",
                additional_kwargs={"tool_calls": [{"provider": "raw"}]},
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "multi_kb_search",
                        "args": {"query": "一维溶质运移解析解"},
                    },
                    {
                        "id": "call-2",
                        "name": "multi_kb_search",
                        "args": {"query": "式2.23 式2.25"},
                    },
                    {
                        "id": "call-3",
                        "name": "multi_kb_search",
                        "args": {"query": "式2.23 式2.25"},
                    },
                    {
                        "id": "call-4",
                        "name": "calculator",
                        "args": {"expression": "1+1"},
                    },
                ],
            )
        ]
    )
    budget_token = set_tool_budgets({"multi_kb_search": 2})
    try:
        bounded = middleware.wrap_model_call(request, lambda _: response)
    finally:
        reset_tool_budgets(budget_token)

    assert isinstance(bounded, ModelResponse)
    assert [call["id"] for call in bounded.result[0].tool_calls] == [
        "call-1",
        "call-2",
        "call-4",
    ]
    assert "tool_calls" not in bounded.result[0].additional_kwargs


def test_parallel_distinct_calls_cannot_exceed_remaining_tool_budget():
    middleware = ToolBudgetAvailabilityMiddleware()
    response = AIMessage(
        content="",
        tool_calls=[
            {"id": "call-1", "name": "multi_kb_search", "args": {"query": "a"}},
            {"id": "call-2", "name": "multi_kb_search", "args": {"query": "b"}},
        ],
    )
    budget_token = set_tool_budgets({"multi_kb_search": 1})
    try:
        bounded = middleware._bounded_response(response)
    finally:
        reset_tool_budgets(budget_token)

    assert isinstance(bounded, AIMessage)
    assert [call["id"] for call in bounded.tool_calls] == ["call-1"]


async def test_agent_graph_does_not_dispatch_third_parallel_retrieval_call():
    from typing import Any

    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import BaseMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool

    from backend.core.agents.factory import create_agent_graph

    dispatched_queries = []

    @tool
    def multi_kb_search(query: str) -> str:
        """Return knowledge-base evidence for a query."""

        allowed, _ = consume_tool_budget("multi_kb_search")
        assert allowed
        dispatched_queries.append(query)
        return f"evidence:{query}"

    class ParallelRetrievalModel(BaseChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: Any | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            if any(isinstance(message, ToolMessage) for message in messages):
                answer = AIMessage(content="已根据两次检索结果回答")
            else:
                answer = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "multi_kb_search",
                            "args": {"query": "一维溶质运移解析解"},
                        },
                        {
                            "id": "call-2",
                            "name": "multi_kb_search",
                            "args": {"query": "式2.23 式2.25"},
                        },
                        {
                            "id": "call-3",
                            "name": "multi_kb_search",
                            "args": {"query": "式2.23 式2.25"},
                        },
                    ],
                )
            return ChatResult(generations=[ChatGeneration(message=answer)])

        @property
        def _llm_type(self) -> str:
            return "parallel-retrieval-test"

    graph = create_agent_graph(
        model=ParallelRetrievalModel(),
        tools=[multi_kb_search],
    )
    budget_token = set_tool_budgets({"multi_kb_search": 2})
    try:
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="检索公式并回答")]}
        )
    finally:
        reset_tool_budgets(budget_token)

    # Tool calls are dispatched concurrently, so completion order is not a
    # contract. The budget/dedup contract is that these two unique calls run
    # exactly once and the duplicate third call never reaches the provider.
    assert sorted(dispatched_queries) == sorted(["一维溶质运移解析解", "式2.23 式2.25"])
    assert result["messages"][-1].content == "已根据两次检索结果回答"


def test_failed_artifact_read_is_hidden_on_the_next_model_round():
    from backend.application.tools.providers.context_artifact_tool import (
        get_scoped_read_context_artifact_tool,
    )

    budget_token = set_tool_budgets({"read_context_artifact": 1})
    try:
        failed = json.loads(
            get_scoped_read_context_artifact_tool(None).invoke(
                {"raw_ref": "artifact://context/raw_1d54fab81c89"}
            )
        )
        messages = [HumanMessage(content="继续使用已有证据计算")]
        request = ModelRequest(
            model=SimpleNamespace(),
            messages=messages,
            tools=[
                {
                    "type": "function",
                    "function": {"name": "read_context_artifact"},
                },
                {"type": "function", "function": {"name": "calculator"}},
            ],
            tool_choice="auto",
            state={"messages": messages},
            runtime=None,
        )
        captured = []
        ToolBudgetAvailabilityMiddleware().wrap_model_call(
            request,
            lambda patched: captured.append(patched)
            or ModelResponse(result=[AIMessage(content="继续计算")]),
        )
    finally:
        reset_tool_budgets(budget_token)

    assert failed["error"] == "invalid_artifact_ref"
    assert [tool["function"]["name"] for tool in captured[0].tools] == ["calculator"]


def test_similar_cache_compares_query_separately_from_kb_parameters():
    def key(query: str, *, kb_id: str = "kb-a") -> str:
        return _build_request_cache_key(
            query=query,
            kb_ids=[kb_id],
            top_k=10,
            chunk_top_k=6,
            merge_strategy="rrf",
            external_hl_keywords=None,
            external_ll_keywords=None,
            external_query_type=None,
            user_provided_keywords=False,
            content_filter_mode=None,
            max_result_chars=8000,
        )

    cache = RetrievalRequestCache()
    cache.set_exact(key("地下水污染修复技术"), "groundwater-result")

    assert cache.get_similar(key("核电子学探测器原理")) is None
    assert (
        cache.get_similar(key("地下水污染修复方法"), threshold=0.3)
        == "groundwater-result"
    )
    assert (
        cache.get_similar(key("地下水污染修复方法", kb_id="kb-b"), threshold=0.3)
        is None
    )


def test_retrieval_cache_key_separates_result_size_budget():
    common = dict(
        query="地下水污染修复技术",
        kb_ids=["kb-a"],
        top_k=10,
        chunk_top_k=6,
        merge_strategy="rrf",
        external_hl_keywords=None,
        external_ll_keywords=None,
        external_query_type=None,
        user_provided_keywords=False,
        content_filter_mode=None,
    )

    small = _build_request_cache_key(**common, max_result_chars=2000)
    large = _build_request_cache_key(**common, max_result_chars=8000)

    assert small != large


def test_raw_kb_chunks_build_structured_evidence_without_markdown_reparse():
    records = _build_raw_chunk_evidence_records(
        [
            {
                "success": True,
                "kb_id": "kb-a",
                "kb_label": "地下水标准库",
                "chunks": [
                    {
                        "id": "chunk-17",
                        "content": "标准 A 的限值为 17 mg/L。",
                        "score": 0.93,
                        "file_path": "标准A.pdf",
                    }
                ],
            }
        ],
        per_kb_limit=3,
    )

    assert len(records) == 1
    assert records[0]["source_id"] == "kb-a"
    assert records[0]["chunk_id"] == "chunk-17"
    assert records[0]["score"] == 0.93
    assert "17 mg/L" in records[0]["claim"]
    assert "raw_chunk" in records[0]["tags"]


def test_structured_cache_hit_preserves_evidence_and_marks_hit_type():
    result = _mark_structured_cache_hit(
        {
            "text": "evidence",
            "evidence_records": [{"chunk_id": "c1"}],
            "stats": {"evidence_count": 1},
        },
        "in_flight",
    )

    assert result["text"] == "evidence"
    assert result["evidence_records"] == [{"chunk_id": "c1"}]
    assert result["stats"]["cache_hit"] == "in_flight"


async def test_cancelled_provider_waiter_does_not_leak_capacity():
    cache = RetrievalRequestCache()
    await cache.acquire_provider_slot()
    waiter = asyncio.create_task(cache.acquire_provider_slot())
    await asyncio.sleep(0)
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass
    cache.release_provider_slot()

    await asyncio.wait_for(cache.acquire_provider_slot(), timeout=0.2)
    cache.release_provider_slot()


async def test_provider_capacity_is_shared_across_request_caches():
    first = RetrievalRequestCache()
    second = RetrievalRequestCache()
    activity = {"active": 0, "max_active": 0}

    async def run(cache):
        await cache.acquire_provider_slot()
        try:
            activity["active"] += 1
            activity["max_active"] = max(activity["max_active"], activity["active"])
            await asyncio.sleep(0.02)
        finally:
            activity["active"] -= 1
            cache.release_provider_slot()

    await asyncio.gather(run(first), run(second))

    assert activity["max_active"] == min(2, _KB_PROVIDER_CAPACITY)


def test_reset_tool_budgets_tolerates_cross_context_cleanup():
    token = copy_context().run(lambda: set_tool_budgets({"multi_kb_search": 1}))

    reset_tool_budgets(token)

    assert consume_tool_budget("multi_kb_search") == (True, None)


def test_reset_request_skill_state_tolerates_cross_context_cleanup():
    token = copy_context().run(lambda: set_request_skill_state({"deep-research"}))

    reset_request_skill_state(token)

    assert get_missing_required_skills() == set()


async def test_run_blocking_propagates_request_contextvars():
    marker: ContextVar[str | None] = ContextVar("marker", default=None)
    marker.set("request-cache-visible")

    def read_marker() -> str | None:
        return marker.get()

    assert await run_blocking(read_marker) == "request-cache-visible"


async def test_run_blocking_concurrent_kb_budget_reservations_are_atomic():
    reset_turn_kb_context_budget(max_chars=10)

    reservations = await asyncio.wait_for(
        asyncio.gather(
            run_blocking(reserve_turn_kb_chars, 8, timeout_seconds=1),
            run_blocking(reserve_turn_kb_chars, 8, timeout_seconds=1),
        ),
        timeout=2,
    )

    assert sum(reservations) == 10
