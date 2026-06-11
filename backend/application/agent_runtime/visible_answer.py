"""Helpers for collecting visible text directly from the standard agent runtime."""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from backend.application.agent_service import AgentService
from backend.application.event_stream.thinking_filter import split_thinking_segments
from backend.domain.agents.request import AgentRequest


async def collect_visible_answer_standard(
    *,
    question: str,
    agent_code: str = "ne",
    user_id: str = "system",
    thread_id: str | None = None,
    workflow: str | None = "full",
    model_name: str | None = None,
    selected_kb_ids: list[str] | None = None,
    selected_skills: list[str] | None = None,
    use_search: bool = True,
    subagent_enabled: bool = False,
    thinking_enabled: bool = False,
    checkpointer: BaseCheckpointSaver | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Collect visible answer text from the standard AgentEvent stream."""
    request = AgentRequest(
        question=question,
        agent_code=agent_code,
        user_id=user_id,
        conversation_id=thread_id or f"runtime__{uuid.uuid4().hex}",
        requested_workflow=workflow,
        selected_kb_ids=list(selected_kb_ids or []),
        selected_skills=list(selected_skills or []),
        model_name=model_name,
        use_search=use_search,
        subagent_enabled=subagent_enabled,
        thinking_enabled=thinking_enabled,
        metadata=dict(metadata or {}),
    )
    service = AgentService(checkpointer=checkpointer)

    full_answer = ""
    in_thought = False
    think_carry = ""
    async for event in service.run_stream(request):
        if event.type == "message.delta":
            visible, _, in_thought, think_carry = split_thinking_segments(
                str(event.payload.get("content") or ""),
                in_thought,
                think_carry,
            )
            full_answer += visible
        elif event.type == "error":
            error_msg = str(event.payload.get("message") or event.payload.get("error") or "")
            return f"错误: {error_msg}"
    return full_answer
