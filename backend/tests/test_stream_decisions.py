from __future__ import annotations

from backend.application.agent_executors.stream_decisions import (
    StreamAction,
    StreamExitReason,
    decide_post_stream_action,
    needs_empty_answer_recovery,
    should_emit_timeout_chunk,
    should_resume_incomplete_round,
    should_run_writer_stage,
    should_write_fallback_state,
    timeout_reached_for,
)


def test_exit_reason_enum_values_are_backward_compatible_strings() -> None:
    assert StreamExitReason.LANGGRAPH_STREAM_COMPLETED == "langgraph_stream_completed"
    assert StreamExitReason.DEEP_RESEARCH_TOTAL_TIMEOUT == "deep_research_total_timeout"
    assert StreamExitReason.EVIDENCE_CONVERGED == "evidence_converged"


def test_resume_incomplete_round_only_for_completed_stream() -> None:
    assert should_resume_incomplete_round(
        StreamExitReason.LANGGRAPH_STREAM_COMPLETED, True
    ) is True
    assert should_resume_incomplete_round(
        StreamExitReason.LANGGRAPH_STREAM_COMPLETED, False
    ) is False
    assert should_resume_incomplete_round(StreamExitReason.IDLE_TIMEOUT, True) is False
    assert should_resume_incomplete_round("evidence_converged", True) is False


def test_empty_answer_recovery_requires_completed_with_tools_and_no_text() -> None:
    assert needs_empty_answer_recovery(
        StreamExitReason.LANGGRAPH_STREAM_COMPLETED, [], True
    ) is True
    assert needs_empty_answer_recovery(
        StreamExitReason.LANGGRAPH_STREAM_COMPLETED, ["有正文"], True
    ) is False
    assert needs_empty_answer_recovery(
        StreamExitReason.LANGGRAPH_STREAM_COMPLETED, [], False
    ) is False
    assert needs_empty_answer_recovery(StreamExitReason.IDLE_TIMEOUT, [], True) is False


def test_writer_stage_conditions() -> None:
    common = dict(
        deep_research_mode=False,
        has_writer_evidence=True,
        answer_fragments=[],
        any_tool_executed=True,
    )
    assert should_run_writer_stage(StreamExitReason.EVIDENCE_CONVERGED, **common)
    assert should_run_writer_stage(StreamExitReason.GRAPH_RECURSION_LIMIT, **common)
    assert should_run_writer_stage(
        StreamExitReason.LANGGRAPH_STREAM_COMPLETED, **common
    )
    assert not should_run_writer_stage(StreamExitReason.IDLE_TIMEOUT, **common)
    assert not should_run_writer_stage(
        StreamExitReason.EVIDENCE_CONVERGED,
        deep_research_mode=True,
        has_writer_evidence=True,
        answer_fragments=[],
        any_tool_executed=True,
    )
    assert not should_run_writer_stage(
        StreamExitReason.EVIDENCE_CONVERGED,
        deep_research_mode=False,
        has_writer_evidence=False,
        answer_fragments=[],
        any_tool_executed=True,
    )


def test_fallback_state_write_conditions() -> None:
    assert should_write_fallback_state(
        StreamExitReason.IDLE_TIMEOUT,
        deep_research_mode=False,
        has_incomplete_model_round=True,
        timeout_reached=True,
        answer_fragments=["fallback"],
    )
    assert not should_write_fallback_state(
        StreamExitReason.LANGGRAPH_STREAM_COMPLETED,
        deep_research_mode=False,
        has_incomplete_model_round=False,
        timeout_reached=False,
        answer_fragments=["ok"],
    )
    assert not should_write_fallback_state(
        StreamExitReason.IDLE_TIMEOUT,
        deep_research_mode=True,
        has_incomplete_model_round=True,
        timeout_reached=True,
        answer_fragments=["fallback"],
    )
    assert not should_write_fallback_state(
        StreamExitReason.IDLE_TIMEOUT,
        deep_research_mode=False,
        has_incomplete_model_round=True,
        timeout_reached=True,
        answer_fragments=[],
    )


def test_timeout_chunk_conditions() -> None:
    assert should_emit_timeout_chunk(
        StreamExitReason.IDLE_TIMEOUT,
        timeout_reached=True,
        answer_fragments=[],
        graph_recursion_error=False,
    )
    assert not should_emit_timeout_chunk(
        StreamExitReason.GRAPH_RECURSION_LIMIT,
        timeout_reached=True,
        answer_fragments=[],
        graph_recursion_error=True,
    )
    assert not should_emit_timeout_chunk(
        StreamExitReason.IDLE_TIMEOUT,
        timeout_reached=True,
        answer_fragments=["text"],
        graph_recursion_error=False,
    )
    assert not should_emit_timeout_chunk(
        StreamExitReason.LANGGRAPH_STREAM_COMPLETED,
        timeout_reached=False,
        answer_fragments=[],
        graph_recursion_error=False,
    )


def test_timeout_reached_for_maps_timeout_reasons() -> None:
    assert timeout_reached_for(StreamExitReason.IDLE_TIMEOUT)
    assert timeout_reached_for(StreamExitReason.DEEP_RESEARCH_TOTAL_TIMEOUT)
    assert timeout_reached_for(StreamExitReason.GRAPH_RECURSION_LIMIT)
    assert not timeout_reached_for(StreamExitReason.LANGGRAPH_STREAM_COMPLETED)
    assert not timeout_reached_for(StreamExitReason.OUTPUT_TRUNCATED)


def test_decide_post_stream_action_resume_takes_precedence_over_writer() -> None:
    """与运行时顺序一致:pass 循环内先决定续跑,writer 只在续跑停止后运行。

    此前该镜像函数把 WRITER 排在 RESUME 前:流正常结束但模型轮次未收尾、
    同时满足空正文撰写兜底条件时,它给出 WRITER,而运行时会先续跑。
    """

    ambiguous_state = dict(
        deep_research_mode=False,
        has_writer_evidence=True,
        answer_fragments=[],
        any_tool_executed=True,
    )
    assert decide_post_stream_action(
        StreamExitReason.LANGGRAPH_STREAM_COMPLETED,
        has_incomplete_model_round=True,
        **ambiguous_state,
    ) == StreamAction.RESUME
    # 续跑机会耗尽(调用方传 False)后才轮到撰写兜底。
    assert decide_post_stream_action(
        StreamExitReason.LANGGRAPH_STREAM_COMPLETED,
        has_incomplete_model_round=False,
        **ambiguous_state,
    ) == StreamAction.WRITER


def test_decide_post_stream_action_prefers_writer_then_archive_then_finalize() -> None:
    assert decide_post_stream_action(
        StreamExitReason.EVIDENCE_CONVERGED,
        deep_research_mode=False,
        has_incomplete_model_round=False,
        has_writer_evidence=True,
        answer_fragments=[],
        any_tool_executed=True,
    ) == StreamAction.WRITER

    assert decide_post_stream_action(
        StreamExitReason.DEEP_RESEARCH_TOTAL_TIMEOUT,
        deep_research_mode=True,
        has_incomplete_model_round=False,
        has_writer_evidence=False,
        answer_fragments=[],
        any_tool_executed=True,
        report_saved=False,
    ) == StreamAction.ARCHIVE

    assert decide_post_stream_action(
        StreamExitReason.IDLE_TIMEOUT,
        deep_research_mode=False,
        has_incomplete_model_round=True,
        has_writer_evidence=False,
        answer_fragments=["fallback"],
        any_tool_executed=True,
    ) == StreamAction.FINALIZE

    assert decide_post_stream_action(
        StreamExitReason.LANGGRAPH_STREAM_COMPLETED,
        deep_research_mode=False,
        has_incomplete_model_round=False,
        has_writer_evidence=False,
        answer_fragments=["ok"],
        any_tool_executed=True,
    ) == StreamAction.STOP
