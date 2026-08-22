from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.application.agent_executors import lightweight_context as lightweight_module
from backend.application.agent_executors.lightweight_qa_agent import LightweightQAAgent
from backend.application.agent_executors.streaming_runtime import AgentStreamRuntime
from backend.application.retrieval.query_resolution import (
    compose_retrieval_query,
    effective_tool_query,
    get_retrieval_turn_context,
    latest_user_message_text,
    reset_retrieval_turn_context,
    set_retrieval_turn_context,
)
from backend.application.tools.tool_assembly_pipeline import ToolAssemblyPipeline


def test_compose_retrieval_query_preserves_literal_media_intent() -> None:
    result = compose_retrieval_query(
        current_question="请给出蒙脱石颗粒表面一价阳离子的分布图？",
        contextual_query="蒙脱石 表面 一价阳离子 分布 吸附 交换容量",
    )

    assert result.startswith("蒙脱石 表面 一价阳离子")
    assert "请给出蒙脱石颗粒表面一价阳离子的分布图？" in result
    assert "图" in result


def test_compose_retrieval_query_avoids_duplicate_faithful_rewrite() -> None:
    result = compose_retrieval_query(
        current_question="横坐标是什么意思？",
        contextual_query="蒙脱石颗粒表面一价阳离子分布图的横坐标是什么意思？",
    )

    assert result == "蒙脱石颗粒表面一价阳离子分布图的横坐标是什么意思？"


def test_compose_retrieval_query_keeps_literal_turn_when_truncated() -> None:
    result = compose_retrieval_query(
        current_question="请返回指定图片？",
        contextual_query="扩展背景" * 100,
        max_chars=40,
    )

    assert len(result) <= 40
    assert result.endswith("请返回指定图片？")


def test_latest_user_message_text_handles_langchain_and_dict_messages() -> None:
    messages = [
        HumanMessage(content="上一题"),
        AIMessage(content="上一答"),
        {"role": "user", "content": [{"type": "text", "text": "最近问题"}]},
    ]

    assert latest_user_message_text(messages) == "最近问题"


@pytest.mark.asyncio
async def test_stream_runtime_binds_and_resets_literal_turn() -> None:
    runtime = object.__new__(AgentStreamRuntime)

    async def fake_impl(self, **kwargs):
        del self, kwargs
        yield {"query": effective_tool_query("模型改写")}

    runtime._aquery_stream_impl = MethodType(fake_impl, runtime)
    events = [
        event
        async for event in runtime.aquery_stream(question="请给出原图？")
    ]

    assert events == [{"query": "模型改写\n请给出原图？"}]
    assert get_retrieval_turn_context().current_question == ""


@pytest.mark.asyncio
async def test_lightweight_followup_media_query_is_contextualized_without_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    class FakeModel:
        async def ainvoke(self, prompt, config=None):
            nonlocal called
            called = True
            assert "上轮讨论了蒙脱石阳离子分布" in prompt
            assert config == {"max_tokens": 128}
            return AIMessage(
                content="QUERY: 蒙脱石颗粒表面一价阳离子的分布图"
            )

    async def noop(*args, **kwargs):
        del args, kwargs

    async def fake_create_chat_model(**kwargs):
        del kwargs
        return FakeModel()

    monkeypatch.setattr(
        lightweight_module,
        "create_chat_model_async",
        fake_create_chat_model,
    )
    monkeypatch.setattr(lightweight_module, "check_agent_runtime_budget", noop)
    monkeypatch.setattr(lightweight_module, "charge_agent_model_response", noop)

    agent = object.__new__(LightweightQAAgent)
    agent._prompt_domain = "csh"
    agent._model_name = "test-model"
    agent._context_policy = SimpleNamespace(result_summary_output_chars=4000)

    query, skip_kb = await agent._extract_search_query_v2(
        "请给出它的分布图？",
        "上轮讨论了蒙脱石阳离子分布",
        previous_user_question="蒙脱石颗粒表面一价阳离子如何分布？",
    )

    assert called is True
    assert skip_kb is False
    assert "蒙脱石颗粒表面一价阳离子的分布图" in query
    assert "请给出它的分布图？" in query


@pytest.mark.asyncio
async def test_lightweight_followup_falls_back_to_previous_user_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingModel:
        async def ainvoke(self, prompt, config=None):
            del prompt, config
            raise RuntimeError("model unavailable")

    async def noop(*args, **kwargs):
        del args, kwargs

    async def fake_create_chat_model(**kwargs):
        del kwargs
        return FailingModel()

    monkeypatch.setattr(
        lightweight_module,
        "create_chat_model_async",
        fake_create_chat_model,
    )
    monkeypatch.setattr(lightweight_module, "check_agent_runtime_budget", noop)
    monkeypatch.setattr(lightweight_module, "charge_agent_model_response", noop)

    agent = object.__new__(LightweightQAAgent)
    agent._prompt_domain = "csh"
    agent._model_name = "test-model"
    agent._context_policy = SimpleNamespace(result_summary_output_chars=4000)

    query, skip_kb = await agent._extract_search_query_v2(
        "它的横坐标呢？",
        "存在历史",
        previous_user_question="请给出蒙脱石颗粒表面一价阳离子的分布图？",
    )

    assert skip_kb is False
    assert query.startswith("请给出蒙脱石颗粒表面一价阳离子的分布图？")
    assert query.endswith("它的横坐标呢？")


@pytest.mark.asyncio
async def test_full_tool_uses_shared_query_and_internal_keyword_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return {"text": "evidence", "evidence_records": [], "stats": {}}

    # The provider is imported inside the factory, so patch the source module.
    from backend.application.tools.providers import multi_kb_search_tool

    monkeypatch.setattr(
        multi_kb_search_tool,
        "execute_multi_kb_search",
        fake_execute,
    )

    pipeline = ToolAssemblyPipeline(
        kb_ids=["kb-csh"],
        domain="csh-hydrogeology",
        workflow_name="full",
        enforce_skill_load=False,
    )
    search_tool = pipeline._create_multi_kb_search_tool()
    token = set_retrieval_turn_context(
        "请给出蒙脱石颗粒表面一价阳离子的分布图？"
    )
    try:
        await search_tool.ainvoke(
            {
                "query": "蒙脱石 表面 一价阳离子 分布 吸附 交换容量",
                "high_level_keywords": ["交换容量"],
                "low_level_keywords": ["Na+"],
                "query_type": "global",
            }
        )
    finally:
        reset_retrieval_turn_context(token)

    assert "分布图" in str(captured["query"])
    assert captured["external_hl_keywords"] is None
    assert captured["external_ll_keywords"] is None
    assert captured["external_query_type"] is None


@pytest.mark.asyncio
async def test_full_tool_allows_a_second_distinct_kb_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []
    attempts = 0

    async def fake_execute(**kwargs):
        nonlocal attempts
        attempts += 1
        from backend.core.tools.request_limits import consume_tool_budget

        allowed, _remaining = consume_tool_budget("multi_kb_search")
        if not allowed:
            return {
                "text": "知识库检索次数已达上限，请基于已有检索结果回答。",
                "evidence_records": [],
                "stats": {"tool_budget_exhausted": True},
            }
        queries.append(str(kwargs["query"]))
        return {"text": "evidence", "evidence_records": [], "stats": {}}

    from backend.application.tools.providers import multi_kb_search_tool

    monkeypatch.setattr(
        multi_kb_search_tool,
        "execute_multi_kb_search",
        fake_execute,
    )

    pipeline = ToolAssemblyPipeline(
        kb_ids=["kb-csh"],
        domain="csh-hydrogeology",
        workflow_name="full",
        enforce_skill_load=False,
    )
    search_tool = pipeline._create_multi_kb_search_tool()
    token = set_retrieval_turn_context("计算弥散度并核对公式来源")
    from backend.core.tools.request_limits import reset_tool_budgets, set_tool_budgets

    budget_token = set_tool_budgets({"multi_kb_search": 2})
    try:
        await search_tool.ainvoke({"query": "弥散度计算公式"})
        await search_tool.ainvoke({"query": "例题2.9参数与结果"})
        await search_tool.ainvoke({"query": "第三次同轮检索"})
    finally:
        reset_tool_budgets(budget_token)
        reset_retrieval_turn_context(token)

    assert attempts == 3
    assert len(queries) == 2
    assert "弥散度计算公式" in queries[0]
    assert "例题2.9参数与结果" in queries[1]


@pytest.mark.asyncio
async def test_deep_research_keeps_planned_branch_query_and_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return {"text": "evidence", "evidence_records": [], "stats": {}}

    from backend.application.tools.providers import multi_kb_search_tool

    monkeypatch.setattr(
        multi_kb_search_tool,
        "execute_multi_kb_search",
        fake_execute,
    )
    pipeline = ToolAssemblyPipeline(
        kb_ids=["kb-csh"],
        workflow_name="deep_research",
        enforce_skill_load=False,
    )
    search_tool = pipeline._create_multi_kb_search_tool()
    token = set_retrieval_turn_context("宽泛的用户研究任务")
    try:
        await search_tool.ainvoke(
            {
                "query": "特定研究分支",
                "high_level_keywords": ["含水层"],
                "low_level_keywords": ["TCE"],
                "query_type": "global",
            }
        )
    finally:
        reset_retrieval_turn_context(token)

    assert captured["query"] == "特定研究分支"
    assert captured["external_hl_keywords"] == ["含水层"]
    assert captured["external_ll_keywords"] == ["TCE"]
    assert captured["external_query_type"] == "global"
