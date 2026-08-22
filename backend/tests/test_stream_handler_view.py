from __future__ import annotations

import re
from pathlib import Path

from backend.application.agent_executors.stream_handler import (
    NEAgentStreamHandler,
)
from backend.application.agent_executors.stream_handler_view import (
    StreamHandlerView,
)


def _handler() -> NEAgentStreamHandler:
    return NEAgentStreamHandler(thread_id="thread-1", selected_skills={"html-ppt"})


def test_handler_satisfies_stream_handler_view() -> None:
    assert isinstance(_handler(), StreamHandlerView)


def test_read_only_properties_reflect_private_state() -> None:
    handler = _handler()
    handler._any_tool_executed = True
    handler._normal_output_chars = 42
    handler._model_generation_round = 2
    handler._tools_invoked = ["web_search"]
    handler._model_round_summaries = [{"round": 1}]
    handler._html_ppt_succeeded = True

    assert handler.any_tool_executed is True
    assert handler.normal_output_chars == 42
    assert handler.model_generation_round == 2
    assert handler.tools_invoked == ["web_search"]
    assert handler.model_round_summaries == [{"round": 1}]
    assert handler.html_ppt_succeeded is True


def test_view_collections_are_defensive_copies() -> None:
    handler = _handler()
    handler._tools_invoked = ["a"]
    handler._model_round_summaries = [{"round": 1}]

    handler.tools_invoked.append("b")
    handler.model_round_summaries.append({"round": 2})

    assert handler._tools_invoked == ["a"]
    assert handler._model_round_summaries == [{"round": 1}]


def test_mark_model_round_closed() -> None:
    handler = _handler()
    handler._model_round_open = True
    assert handler.model_round_open is True

    handler.mark_model_round_closed()

    assert handler.model_round_open is False


def test_runtime_and_writer_stage_avoid_private_handler_access() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "backend/application/agent_executors/streaming_runtime.py",
        "backend/application/agent_executors/writer_stage.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert re.search(r"handler\._[a-zA-Z]", source) is None, relative
