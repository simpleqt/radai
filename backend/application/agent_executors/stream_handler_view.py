"""Read-only view protocol for stream handler state.

The streaming runtime should never reach into ``NEAgentStreamHandler`` private
fields.  This protocol declares the stable read surface used for decisions,
logging and recovery routing so the handler can evolve internally without
breaking the runtime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StreamHandlerView(Protocol):
    """State the streaming runtime is allowed to read from a handler."""

    event_count: int
    chunk_count: int
    timeout_reached: bool
    output_truncated: bool
    report_saved: bool
    project_evidence_text: str
    retrieved_evidence_text: str
    answer_fragments: Sequence[str]
    deep_research_grace_applied: bool
    deep_research_active_task_ids: set[str]
    deep_research_last_task_runtime_at: float

    any_tool_executed: bool
    tools_invoked: Sequence[str]
    model_generation_round: int
    model_round_summaries: Sequence[dict[str, Any]]
    normal_output_chars: int
    html_ppt_succeeded: bool

    def has_incomplete_model_round(self) -> bool: ...

    def set_stream_exit_reason(self, reason: Any) -> None: ...

    async def handle(self, event: Any) -> AsyncIterator[dict[str, Any]]: ...

    async def finalize(self) -> AsyncIterator[dict[str, Any]]: ...
