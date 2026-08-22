"""上下文裁剪与历史压缩契约（拆自 test_agent_platform_standardization.py）。"""

import pytest

from backend.application.agent_executors import ProfiledGraphBuilder
from backend.core.agents.middlewares.context_pruning import ContextPruningMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from types import SimpleNamespace


@pytest.fixture(autouse=True)
def _pin_legacy_context_object_mode(monkeypatch):
    """本模块断言 legacy 状态提升/持久化路径的行为。

    CONTEXT_OBJECT_MODE 的 env 默认自 2026-08-13 起为 on;这里显式钉住
    off,与 on 模式测试显式传 ``context_object_mode="on"`` 对称。
    """

    monkeypatch.setenv("CONTEXT_OBJECT_MODE", "off")


def test_context_capture_runs_before_history_summarization():
    from langchain.agents.middleware import SummarizationMiddleware
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from backend.core.agents.factory import _assemble_from_features
    from backend.core.agents.features import RuntimeFeatures

    summarization = SummarizationMiddleware(
        model=FakeListChatModel(responses=["summary"]),
        trigger=("tokens", 8000),
        keep=("tokens", 2000),
    )
    middleware, _ = _assemble_from_features(
        RuntimeFeatures(
            summarization=summarization,
            clarification=False,
            vision=False,
        ),
        extra_middleware=[ContextPruningMiddleware()],
    )

    names = [type(item).__name__ for item in middleware]
    assert names.index("ContextPruningMiddleware") < names.index(
        "SummarizationMiddleware"
    )


def test_history_summarization_uses_model_token_budget(monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from backend.core.agents.context.ledger import LedgerBudget

    monkeypatch.delenv("AGENT_HISTORY_SUMMARIZATION_TRIGGER_TOKENS", raising=False)
    monkeypatch.delenv("AGENT_HISTORY_SUMMARIZATION_KEEP_TOKENS", raising=False)
    monkeypatch.setattr(
        "backend.core.models.create_chat_model",
        lambda **kwargs: FakeListChatModel(responses=["summary"]),
    )
    monkeypatch.setattr(
        LedgerBudget,
        "from_model_name",
        classmethod(lambda cls, model_name: SimpleNamespace(input_budget_tokens=10000)),
    )
    builder = object.__new__(ProfiledGraphBuilder)
    builder._config = SimpleNamespace(model_name="test-model")

    middleware = builder._create_summarization_middleware()

    assert middleware.trigger == ("tokens", 8000)
    assert middleware.keep == ("tokens", 2500)


def test_context_pruning_does_not_elevate_failed_skill_tool_message():
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "read_local_skill_file",
                        "args": {"skill_name": "html-ppt</parameter"},
                    }
                ],
            ),
            ToolMessage(
                content="Skill 'html-ppt</parameter' is not allowed in this session",
                tool_call_id="call-1",
                name="read_local_skill_file",
                status="error",
            ),
        ]
    }

    updates = ContextPruningMiddleware()._process_state_elevation(state)

    assert updates is None
    assert "loaded_skills" not in state
    assert state["messages"][1].content == "Skill 'html-ppt</parameter' is not allowed in this session"


def test_context_pruning_normalizes_skill_name_before_state_elevation():
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "read_local_skill_file",
                        "args": {"skill_name": "html-ppt</parameter"},
                    }
                ],
            ),
            ToolMessage(
                content="# html-ppt\nskill content",
                tool_call_id="call-1",
                name="read_local_skill_file",
            ),
        ]
    }

    updates = ContextPruningMiddleware()._process_state_elevation(state)

    assert updates is not None
    assert set(updates["loaded_skills"]) == {"html-ppt"}
    assert updates["loaded_skills"]["html-ppt"] == "# html-ppt\nskill content"
    assert "loaded_skills" not in state
    assert state["messages"][1].content == "# html-ppt\nskill content"


def test_context_pruning_isolates_groundwater_project_tool_results():
    large_project_payload = '{"point_code":"GW18","water_quality_measurements":[' + ("x" * 1800) + "]}"
    state = {
        "messages": [
            ToolMessage(
                content=large_project_payload,
                tool_call_id="call-project",
                name="query_project_tables",
            ),
        ]
    }

    middleware = ContextPruningMiddleware(char_threshold=100)
    updates = middleware._process_state_elevation(state)
    assert updates is not None
    jit_state = {**state, **{key: value for key, value in updates.items() if key != "messages"}}
    patched_system = middleware._assemble_jit_system_message(None, jit_state)

    assert len(updates["context_records"]) == 1
    assert updates["context_records"][0]["source_tool"] == "query_project_tables"
    assert "project_evidence" not in updates
    assert "context_items" not in updates
    assert updates["messages"][0].content.startswith("[Context Isolation: Groundwater project data")
    assert state["messages"][0].content == large_project_payload
    assert patched_system is not None
    assert 'kind="project_data"' in patched_system.content
    assert "GW18" in patched_system.content


def test_context_pruning_jit_context_is_bounded_and_not_persisted_between_model_calls():
    from langchain_core.messages import SystemMessage

    from backend.core.agents.middlewares import context_pruning as pruning_module

    oversized_evidence = "e" * (pruning_module.JIT_MAX_EVIDENCE_CHARS + 2000)
    state = {
        "messages": [],
        "retrieved_evidence": [oversized_evidence],
        "project_evidence": [],
        "loaded_skills": {},
    }
    middleware = ContextPruningMiddleware()
    base_system = SystemMessage(content="base system prompt")

    first = middleware._assemble_jit_system_message(base_system, state)
    second = middleware._assemble_jit_system_message(base_system, state)

    assert base_system.content == "base system prompt"
    assert first is not None
    assert second is not None
    assert first.content.count("DYNAMIC CONTEXT") == 1
    assert second.content.count("DYNAMIC CONTEXT") == 1
    assert len(first.content) == len(second.content)
    assert oversized_evidence not in first.content
    assert first.content.count("<context_record") == 1
    assert len(first.content) < pruning_module.JIT_MAX_EVIDENCE_CHARS


def test_context_pruning_returns_reducer_updates_with_stable_message_ids():
    from langgraph.graph.message import add_messages

    original = ToolMessage(
        content="retrieved evidence",
        tool_call_id="call-rag",
        name="multi_kb_search",
        id="message-rag",
    )
    state = {"messages": [original]}

    updates = ContextPruningMiddleware().before_model(state, runtime=None)

    assert updates is not None
    assert state == {"messages": [original]}
    assert updates["context_records"]
    assert "retrieved_evidence" not in updates
    assert "context_items" not in updates
    assert updates["messages"][0].id == original.id
    merged = add_messages(state["messages"], updates["messages"])
    assert len(merged) == 1
    assert merged[0].content.startswith("[Context Isolation:")


def test_context_pruning_captures_generic_tool_result_for_dynamic_jit_context():
    from langchain_core.messages import HumanMessage

    payload = "calibration_result=PASS\n" + ("detector channel 17 stable\n" * 200)
    original = ToolMessage(
        content=payload,
        tool_call_id="call-calibration",
        name="run_detector_calibration",
        id="message-calibration",
    )
    state = {
        "messages": [HumanMessage(content="汇总探测器校准结果"), original],
        "loaded_skills": {},
        "retrieved_evidence": [],
        "project_evidence": [],
        "context_items": [],
    }
    middleware = ContextPruningMiddleware(char_threshold=100)

    updates = middleware._process_state_elevation(state)

    assert updates is not None
    assert updates["messages"][0].content.startswith("[Result Capture:")
    assert updates["messages"][0].id == original.id
    assert updates["context_records"][0]["source_tool"] == "run_detector_calibration"
    assert updates["context_records"][0]["raw_ref"].startswith("artifact://context/")
    assert "context_items" not in updates
    jit_state = {**state, "context_records": updates["context_records"]}
    system_message = middleware._assemble_jit_system_message(None, jit_state)
    assert system_message is not None
    assert 'category="tool_result"' in system_message.content
    assert "calibration_result=PASS" in system_message.content


def test_context_pruning_does_not_capture_result_marker_twice():
    state = {
        "messages": [
            ToolMessage(
                content="[Result Capture: Tool 'x' returned 2000 characters. item='ctx_1'.]",
                tool_call_id="call-x",
                name="x",
            )
        ]
    }

    assert ContextPruningMiddleware(char_threshold=10)._process_state_elevation(state) is None


def test_context_pruning_wraps_request_with_one_system_message_and_keeps_messages_unchanged():
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.messages import HumanMessage, SystemMessage

    middleware = ContextPruningMiddleware()
    user_message = HumanMessage(content="question")
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=[user_message],
        system_message=SystemMessage(content="base system prompt"),
        state={
            "messages": [user_message, HumanMessage(content="state-only message")],
            "loaded_skills": {},
            "retrieved_evidence": ["evidence"],
            "project_evidence": [],
        },
        runtime=None,
    )
    captured = []

    middleware.wrap_model_call(
        request,
        lambda patched_request: captured.append(patched_request) or ModelResponse(result=[]),
    )

    assert len(captured) == 1
    assert captured[0].messages == [user_message]
    assert all(not isinstance(message, SystemMessage) for message in captured[0].messages)
    assert captured[0].system_message.content.startswith("base system prompt")
    assert captured[0].system_message.content.count("DYNAMIC CONTEXT") == 1

    clean_request = ModelRequest(
        model=request.model,
        messages=request.messages,
        system_message=request.system_message,
        state={"messages": [user_message], "loaded_skills": {}, "retrieved_evidence": [], "project_evidence": []},
        runtime=None,
    )
    captured.clear()
    middleware.wrap_model_call(
        clean_request,
        lambda patched_request: captured.append(patched_request) or ModelResponse(result=[]),
    )
    assert captured[0].system_message.content == "base system prompt"


async def test_context_pruning_async_wrap_uses_request_local_state():
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.messages import HumanMessage, SystemMessage

    middleware = ContextPruningMiddleware()
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=[HumanMessage(content="question")],
        system_message=SystemMessage(content="base"),
        state={
            "messages": [],
            "loaded_skills": {},
            "retrieved_evidence": ["async evidence"],
            "project_evidence": [],
        },
        runtime=None,
    )
    captured = []

    async def handler(patched_request):
        captured.append(patched_request)
        return ModelResponse(result=[])

    await middleware.awrap_model_call(request, handler)

    assert captured[0].messages == request.messages
    assert captured[0].system_message.content.count("DYNAMIC CONTEXT") == 1
    assert "async evidence" in captured[0].system_message.content
