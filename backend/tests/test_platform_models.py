"""推理兼容模型与流式工具调用契约（拆自 test_agent_platform_standardization.py）。"""

from backend.core.config.model_config import ModelConfig
from backend.core.models.factory import _build_model_settings, _resolve_model_use
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from types import SimpleNamespace


def test_chatopenai_models_disable_streaming_for_tool_calling():
    config = ModelConfig(
        name="gateway-model",
        use="langchain_openai:ChatOpenAI",
        model="gateway-model",
        supports_reasoning_effort=False,
    )

    settings = _build_model_settings(config, thinking_enabled=False, kwargs={})

    assert settings["disable_streaming"] == "tool_calling"


def test_chatopenai_models_use_streaming_tool_compatible_adapter():
    for model_name in ("MiniMax-M2.7", "glm-5.2", "qwen3.6-plus"):
        config = ModelConfig(
            name=model_name,
            use="langchain_openai:ChatOpenAI",
            model=model_name,
            supports_reasoning_effort=model_name == "MiniMax-M2.7",
        )

        assert _resolve_model_use(config) == (
            "backend.core.models.patched_deepseek_openai:PatchedDeepSeekOpenAI"
        )
        assert _build_model_settings(config, thinking_enabled=False, kwargs={})[
            "disable_streaming"
        ] == "tool_calling"


async def test_reasoning_compatible_model_streams_text_with_bound_tools(monkeypatch):
    from langchain_core.outputs import ChatGenerationChunk

    from backend.core.models.patched_deepseek_openai import PatchedDeepSeekOpenAI

    model = PatchedDeepSeekOpenAI(
        model="test-model",
        api_key="test-key",
        disable_streaming="tool_calling",
    )
    tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]

    assert not model._should_stream(async_api=True, tools=tools)
    assert model._should_stream(async_api=True, tools=tools, stream=True)

    class StreamingHandler:
        def tap_output_iter(self, run_id, output):
            return output

        def tap_output_aiter(self, run_id, output):
            return output

    assert model._should_stream(
        async_api=True,
        run_manager=SimpleNamespace(handlers=[StreamingHandler()]),
        tools=tools,
    )

    async def fake_provider(*args, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="第一段"))
        yield ChatGenerationChunk(message=AIMessageChunk(content="第二段"))

    monkeypatch.setattr(model, "_astream_provider", fake_provider)

    chunks = [
        chunk
        async for chunk in model.astream(
            [HumanMessage(content="问题")],
            tools=tools,
        )
    ]

    assert [chunk.content for chunk in chunks if chunk.content] == ["第一段", "第二段"]

    class FakeRunManager:
        run_id = "run-1"
        handlers = [StreamingHandler()]

        def __init__(self):
            self.tokens = []

        async def on_llm_new_token(self, token, **kwargs):
            if token:
                self.tokens.append(token)

    run_manager = FakeRunManager()
    result = await model._agenerate_with_cache(
        [HumanMessage(content="问题")],
        run_manager=run_manager,
        tools=tools,
    )

    assert result.generations[0].message.content == "第一段第二段"
    assert run_manager.tokens == ["第一段", "第二段"]


async def test_reasoning_compatible_model_retries_malformed_tool_stream(monkeypatch):
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

    from backend.core.models.patched_deepseek_openai import PatchedDeepSeekOpenAI

    model = PatchedDeepSeekOpenAI(
        model="test-model",
        api_key="test-key",
        disable_streaming="tool_calling",
    )

    async def fake_provider(*args, **kwargs):
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": "", "args": "", "id": "call-empty", "index": 0}
                ],
            )
        )
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=""),
            generation_info={"finish_reason": "tool_calls"},
        )

    async def fake_generate(self, messages, stop=None, run_manager=None, **kwargs):
        assert kwargs["stream"] is False
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "search", "args": {"query": "HPGe"}, "id": "call-1"}
                        ],
                    )
                )
            ]
        )

    monkeypatch.setattr(model, "_astream_provider", fake_provider)
    monkeypatch.setattr("langchain_openai.ChatOpenAI._agenerate", fake_generate)

    chunks = [
        chunk
        async for chunk in model.astream(
            [HumanMessage(content="问题")],
            tools=[{"type": "function", "function": {"name": "search", "parameters": {}}}],
        )
    ]

    tool_chunks = [chunk for chunk in chunks if chunk.tool_calls]
    assert tool_chunks[-1].tool_calls == [
        {"name": "search", "args": {"query": "HPGe"}, "id": "call-1", "type": "tool_call"}
    ]


async def test_reasoning_compatible_model_recovers_invalid_parameter_tool_call(monkeypatch):
    from langchain_core.outputs import ChatGenerationChunk

    from backend.core.models.patched_deepseek_openai import PatchedDeepSeekOpenAI

    model = PatchedDeepSeekOpenAI(
        model="test-model",
        api_key="test-key",
        disable_streaming="tool_calling",
    )

    async def fake_provider(*args, **kwargs):
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                invalid_tool_calls=[
                    {
                        "name": "query_project_tables",
                        "args": (
                            '{"sql":"SELECT * FROM monitoring_points</parameter",'
                            '"max_rows":20</parameter}'
                        ),
                        "id": "call-query-1",
                        "error": "invalid JSON",
                    }
                ],
            )
        )
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=""),
            generation_info={"finish_reason": "tool_calls"},
        )

    async def unexpected_generate(*args, **kwargs):
        raise AssertionError("losslessly repairable tool calls must not trigger a second request")

    monkeypatch.setattr(model, "_astream_provider", fake_provider)
    monkeypatch.setattr("langchain_openai.ChatOpenAI._agenerate", unexpected_generate)

    chunks = [
        chunk
        async for chunk in model.astream(
            [HumanMessage(content="查询项目表")],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "query_project_tables", "parameters": {}},
                }
            ],
        )
    ]

    tool_chunks = [chunk for chunk in chunks if chunk.tool_calls]
    assert tool_chunks[-1].tool_calls == [
        {
            "name": "query_project_tables",
            "args": {"sql": "SELECT * FROM monitoring_points", "max_rows": 20},
            "id": "call-query-1",
            "type": "tool_call",
        }
    ]
    assert tool_chunks[-1].invalid_tool_calls == []


async def test_reasoning_compatible_model_recovers_tool_call_after_visible_text(monkeypatch):
    from langchain_core.outputs import ChatGenerationChunk

    from backend.core.models.patched_deepseek_openai import PatchedDeepSeekOpenAI

    model = PatchedDeepSeekOpenAI(
        model="test-model",
        api_key="test-key",
        disable_streaming="tool_calling",
    )

    async def fake_provider(*args, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="现在查询项目数据。"))
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                invalid_tool_calls=[
                    {
                        "name": "query_project_tables",
                        "args": '{"sql":"SELECT * FROM wells</parameter"}',
                        "id": "call-after-text",
                        "error": "invalid JSON",
                    }
                ],
            )
        )
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=""),
            generation_info={"finish_reason": "tool_calls"},
        )

    async def unexpected_generate(*args, **kwargs):
        raise AssertionError("late repairable tool calls must not trigger a second request")

    monkeypatch.setattr(model, "_astream_provider", fake_provider)
    monkeypatch.setattr("langchain_openai.ChatOpenAI._agenerate", unexpected_generate)

    chunks = [
        chunk
        async for chunk in model.astream(
            [HumanMessage(content="查询项目表")],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "query_project_tables", "parameters": {}},
                }
            ],
        )
    ]

    assert "".join(str(chunk.content or "") for chunk in chunks) == "现在查询项目数据。"
    assert [chunk.tool_calls for chunk in chunks if chunk.tool_calls][-1] == [
        {
            "name": "query_project_tables",
            "args": {"sql": "SELECT * FROM wells"},
            "id": "call-after-text",
            "type": "tool_call",
        }
    ]


async def test_reasoning_compatible_model_retries_malformed_tool_stream_with_whitespace_and_reasoning(monkeypatch):
    from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

    from backend.core.models.patched_deepseek_openai import PatchedDeepSeekOpenAI

    model = PatchedDeepSeekOpenAI(
        model="test-model",
        api_key="test-key",
        disable_streaming="tool_calling",
    )

    async def fake_provider(*args, **kwargs):
        # 1. Yield reasoning chunk (should stream immediately)
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_content": "I need to call search."}
            )
        )
        # 2. Yield whitespace chunk (should be buffered)
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="\n\n\n")
        )
        # 3. Finish with finish_reason "tool_calls" but no tool call chunks
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=""),
            generation_info={"finish_reason": "tool_calls"},
        )

    async def fake_generate(self, messages, stop=None, run_manager=None, **kwargs):
        assert kwargs["stream"] is False
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "search", "args": {"query": "HPGe"}, "id": "call-1"}
                        ],
                    )
                )
            ]
        )

    monkeypatch.setattr(model, "_astream_provider", fake_provider)
    monkeypatch.setattr("langchain_openai.ChatOpenAI._agenerate", fake_generate)

    chunks = []
    async for chunk in model.astream(
        [HumanMessage(content="问题")],
        tools=[{"type": "function", "function": {"name": "search", "parameters": {}}}],
    ):
        chunks.append(chunk)

    # Verify reasoning was streamed first
    reasoning_chunks = [c for c in chunks if c.additional_kwargs.get("reasoning_content")]
    assert len(reasoning_chunks) == 1
    assert reasoning_chunks[0].additional_kwargs["reasoning_content"] == "I need to call search."

    # Verify the retried tool calls are in the output chunks
    tool_chunks = [chunk for chunk in chunks if chunk.tool_calls]
    assert tool_chunks[-1].tool_calls == [
        {"name": "search", "args": {"query": "HPGe"}, "id": "call-1", "type": "tool_call"}
    ]
