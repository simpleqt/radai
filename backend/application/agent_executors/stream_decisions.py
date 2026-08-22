"""Pure stream decision helpers extracted from the streaming runtime.

The runtime previously encoded exit reasons as bare strings and duplicated
resume/recovery conditions inside ``_aquery_stream_impl``.  These helpers keep
the same behavior but make every decision a pure, unit-testable function.
"""

from __future__ import annotations

from enum import Enum


class StreamExitReason(str, Enum):
    """Why a LangGraph event pass stopped."""

    UNKNOWN = "unknown"
    LANGGRAPH_STREAM_COMPLETED = "langgraph_stream_completed"
    GRAPH_RECURSION_LIMIT = "graph_recursion_limit"
    IDLE_TIMEOUT = "idle_timeout"
    DEEP_RESEARCH_IDLE_TIMEOUT = "deep_research_idle_timeout"
    DEEP_RESEARCH_TOTAL_TIMEOUT = "deep_research_total_timeout"
    DEEP_RESEARCH_BUDGET_EXCEEDED = "deep_research_budget_exceeded"
    EVIDENCE_CONVERGED = "evidence_converged"
    OUTPUT_TRUNCATED = "output_truncated"
    STREAM_TOTAL_TIMEOUT = "stream_total_timeout"


class StreamAction(str, Enum):
    """Primary next action after a stream pass or after the whole stream."""

    RESUME = "resume"
    STOP = "stop"
    WRITER = "writer"
    FINALIZE = "finalize"
    ARCHIVE = "archive"


def should_resume_incomplete_round(
    exit_reason: StreamExitReason | str,
    has_incomplete_model_round: bool,
) -> bool:
    """Whether the outer loop should start another pass from a checkpoint."""

    return (
        StreamExitReason(exit_reason) == StreamExitReason.LANGGRAPH_STREAM_COMPLETED
        and bool(has_incomplete_model_round)
    )


def needs_empty_answer_recovery(
    exit_reason: StreamExitReason | str,
    answer_fragments: list[str] | tuple[str, ...] | None,
    any_tool_executed: bool,
) -> bool:
    """LangGraph finished normally but produced no visible answer after tools."""

    return (
        StreamExitReason(exit_reason) == StreamExitReason.LANGGRAPH_STREAM_COMPLETED
        and not "".join(answer_fragments or ()).strip()
        and bool(any_tool_executed)
    )


def should_run_writer_stage(
    exit_reason: StreamExitReason | str,
    *,
    deep_research_mode: bool,
    has_writer_evidence: bool,
    answer_fragments: list[str] | tuple[str, ...] | None,
    any_tool_executed: bool,
) -> bool:
    """Whether the evidence writer stage should produce the final answer."""

    if deep_research_mode or not has_writer_evidence:
        return False
    reason = StreamExitReason(exit_reason)
    if reason in {
        StreamExitReason.EVIDENCE_CONVERGED,
        StreamExitReason.GRAPH_RECURSION_LIMIT,
    }:
        return True
    return needs_empty_answer_recovery(
        exit_reason,
        answer_fragments,
        any_tool_executed,
    )


def should_write_fallback_state(
    exit_reason: StreamExitReason | str,
    *,
    deep_research_mode: bool,
    has_incomplete_model_round: bool,
    timeout_reached: bool,
    answer_fragments: list[str] | tuple[str, ...] | None,
    writer_persisted: bool = False,
) -> bool:
    """Whether the synthesized fallback answer should be written to history."""

    del exit_reason
    if deep_research_mode:
        return False
    # writer 兜底阶段已把答案落库时,再写一次会追加内容相同的第二条 AIMessage
    # (递归上限路径 writer 产出 + timeout_reached 同时成立)。
    if writer_persisted:
        return False
    if not (has_incomplete_model_round or timeout_reached):
        return False
    return bool("".join(answer_fragments or ()).strip())


def should_emit_timeout_chunk(
    exit_reason: StreamExitReason | str,
    *,
    timeout_reached: bool,
    answer_fragments: list[str] | tuple[str, ...] | None,
    graph_recursion_error: bool,
) -> bool:
    """Whether the hard-coded timeout status chunk should be emitted."""

    del exit_reason
    return bool(
        timeout_reached
        and not "".join(answer_fragments or ()).strip()
        and not graph_recursion_error
    )


def decide_post_stream_action(
    exit_reason: StreamExitReason | str,
    *,
    deep_research_mode: bool,
    has_incomplete_model_round: bool,
    has_writer_evidence: bool,
    answer_fragments: list[str] | tuple[str, ...] | None,
    any_tool_executed: bool,
    report_saved: bool = False,
) -> StreamAction:
    """Primary recovery action after one stream pass, mirroring runtime order.

    RESUME must be evaluated first: the runtime decides checkpoint
    continuation inside the pass loop (bounded by
    ``incomplete_round_retry_max``) before any post-stream recovery is
    consulted, so the writer only ever runs once passes stop.  Callers that
    have exhausted their retry passes should pass
    ``has_incomplete_model_round=False`` (or ignore RESUME) to obtain the
    post-loop recovery action.
    """

    if should_resume_incomplete_round(exit_reason, has_incomplete_model_round):
        return StreamAction.RESUME
    if should_run_writer_stage(
        exit_reason,
        deep_research_mode=deep_research_mode,
        has_writer_evidence=has_writer_evidence,
        answer_fragments=answer_fragments,
        any_tool_executed=any_tool_executed,
    ):
        return StreamAction.WRITER
    if deep_research_mode and not report_saved:
        return StreamAction.ARCHIVE
    if (
        not deep_research_mode
        and (has_incomplete_model_round or timeout_reached_for(exit_reason))
        and bool("".join(answer_fragments or ()).strip())
    ):
        return StreamAction.FINALIZE
    return StreamAction.STOP


def timeout_reached_for(exit_reason: StreamExitReason | str) -> bool:
    """Map timeout-related exit reasons to the runtime ``timeout_reached`` flag."""

    return StreamExitReason(exit_reason) in {
        StreamExitReason.IDLE_TIMEOUT,
        StreamExitReason.DEEP_RESEARCH_IDLE_TIMEOUT,
        StreamExitReason.DEEP_RESEARCH_TOTAL_TIMEOUT,
        StreamExitReason.DEEP_RESEARCH_BUDGET_EXCEEDED,
        StreamExitReason.GRAPH_RECURSION_LIMIT,
        StreamExitReason.STREAM_TOTAL_TIMEOUT,
    }
