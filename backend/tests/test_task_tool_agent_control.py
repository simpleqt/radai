from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from backend.core.agents.context import ContextPolicy
from backend.core.agents.control import (
    AgentControl,
    AgentControlLimits,
    AgentStatus,
    reset_agent_control,
    set_agent_control,
)
from backend.core.agents.middlewares.model_turn_limit import ModelTurnLimitMiddleware
from backend.domain.agents.runtime_agent import RuntimeAgentSpec

task_tool_module = importlib.import_module("backend.core.tools.task_tool")


def _install_task_fakes(monkeypatch, run_subagent):
    spec = SimpleNamespace(
        name="general-purpose",
        max_turns=4,
        timeout_seconds=30,
    )
    monkeypatch.setattr(
        task_tool_module,
        "_RUNTIME_AGENT_LOADER",
        SimpleNamespace(get=lambda _name: spec),
    )
    events: list[dict] = []
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module, "_run_subagent", run_subagent)
    return events


async def _invoke_task(task_id: str) -> str:
    runtime = SimpleNamespace(
        config={
            "configurable": {
                "thread_id": "thread-1",
                "domain": "nuclear-electronics",
                "tool_policy": "ne_default",
            },
            "metadata": {"trace_id": "trace-1"},
        }
    )
    return await task_tool_module.task_tool.coroutine(
        runtime=runtime,
        description=f"delegate {task_id}",
        prompt="do the work",
        subagent_type="general-purpose",
        tool_call_id=task_id,
        max_turns=2,
    )


async def test_task_tool_records_completion_in_shared_agent_control(monkeypatch):
    async def run_subagent(**_kwargs):
        return "delegated answer"

    events = _install_task_fakes(monkeypatch, run_subagent)
    control = AgentControl("root")
    token = set_agent_control(control)
    try:
        result = await _invoke_task("task-1")
    finally:
        reset_agent_control(token)

    info = await control.get("task-1")
    assert info.status is AgentStatus.COMPLETED
    assert info.metadata == {
        "description": "delegate task-1",
        "subagent_type": "general-purpose",
        "trace_id": "trace-1",
    }
    assert result.startswith("Task Succeeded.")
    assert [event["type"] for event in events] == [
        "task_started",
        "task_completed",
    ]


async def test_task_tool_records_timeout_and_preserves_event_contract(monkeypatch):
    async def run_subagent(**_kwargs):
        await asyncio.wait_for(asyncio.Event().wait(), timeout=0.001)

    events = _install_task_fakes(monkeypatch, run_subagent)
    control = AgentControl("root")
    token = set_agent_control(control)
    try:
        result = await _invoke_task("task-timeout")
    finally:
        reset_agent_control(token)

    info = await control.get("task-timeout")
    assert info.status is AgentStatus.TIMED_OUT
    assert result.startswith("Task timed out.")
    assert [event["type"] for event in events] == [
        "task_started",
        "task_timed_out",
    ]


async def test_parallel_task_calls_queue_on_one_request_tree(monkeypatch):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    starts: list[str] = []

    async def run_subagent(**kwargs):
        task_id = kwargs["task_id"]
        starts.append(task_id)
        if task_id == "task-1":
            first_started.set()
            await release_first.wait()
        return f"answer from {task_id}"

    _install_task_fakes(monkeypatch, run_subagent)
    control = AgentControl(
        "root",
        limits=AgentControlLimits(
            max_spawned_agents=3,
            max_active_agents=1,
        ),
    )
    token = set_agent_control(control)
    try:
        first = asyncio.create_task(_invoke_task("task-1"))
        await first_started.wait()
        second = asyncio.create_task(_invoke_task("task-2"))
        await asyncio.sleep(0)

        assert starts == ["task-1"]
        assert (await control.get("task-2")).status is AgentStatus.PENDING

        release_first.set()
        await asyncio.gather(first, second)
    finally:
        reset_agent_control(token)

    assert starts == ["task-1", "task-2"]
    assert (await control.get("task-1")).status is AgentStatus.COMPLETED
    assert (await control.get("task-2")).status is AgentStatus.COMPLETED
    assert control.active_count == 0


async def test_interrupted_task_cannot_emit_completed_after_swallowing_cancel(
    monkeypatch,
):
    started = asyncio.Event()

    async def run_subagent(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "must not become a successful result"

    events = _install_task_fakes(monkeypatch, run_subagent)
    control = AgentControl("root")
    token = set_agent_control(control)
    try:
        execution = asyncio.create_task(_invoke_task("task-interrupted"))
        await started.wait()
        await control.interrupt("task-interrupted", reason="root stopped it")
        with pytest.raises(asyncio.CancelledError):
            await execution
    finally:
        reset_agent_control(token)

    assert (await control.get("task-interrupted")).status is AgentStatus.INTERRUPTED
    assert [event["type"] for event in events] == [
        "task_started",
        "task_cancelled",
    ]


async def test_task_started_is_not_emitted_before_control_admission(monkeypatch):
    async def run_subagent(**_kwargs):
        pytest.fail("closed control must reject before subagent execution")

    events = _install_task_fakes(monkeypatch, run_subagent)
    control = AgentControl("root")
    await control.interrupt("root", reason="request already closed")
    token = set_agent_control(control)
    try:
        result = await _invoke_task("late-task")
    finally:
        reset_agent_control(token)

    assert result.startswith("Task failed.")
    assert [event["type"] for event in events] == ["task_failed"]


async def test_task_tool_depends_on_runtime_manager_interface(monkeypatch):
    async def run_subagent(**kwargs):
        assert kwargs["runtime_manager_managed"] is True
        return "delegated through manager"

    events = _install_task_fakes(monkeypatch, run_subagent)
    spawn_calls: list[tuple[str, dict]] = []

    class InterfaceOnlyManager:
        async def token_for(self, _agent_id):
            return SimpleNamespace(
                cancelled=False,
                raise_if_cancelled=lambda: None,
            )

        @asynccontextmanager
        async def spawn(self, agent_id, *, metadata=None, **_kwargs):
            spawn_calls.append((agent_id, dict(metadata or {})))
            yield SimpleNamespace(agent_id=agent_id)

    monkeypatch.setattr(
        task_tool_module,
        "get_agent_runtime_manager",
        lambda: InterfaceOnlyManager(),
    )

    result = await _invoke_task("manager-task")

    assert result.startswith("Task Succeeded.")
    assert spawn_calls == [
        (
            "manager-task",
            {
                "description": "delegate manager-task",
                "subagent_type": "general-purpose",
                "trace_id": "trace-1",
            },
        )
    ]
    assert [event["type"] for event in events] == [
        "task_started",
        "task_completed",
    ]


async def test_handoff_failure_is_recorded_inside_manager_lifecycle(monkeypatch):
    async def run_subagent(**_kwargs):
        return "delegated answer"

    events = _install_task_fakes(monkeypatch, run_subagent)
    def invalid_handoff(**_kwargs):
        raise ValueError("handoff invalid")

    monkeypatch.setattr(
        task_tool_module,
        "build_subagent_result",
        invalid_handoff,
    )
    control = AgentControl("root")
    token = set_agent_control(control)
    try:
        result = await _invoke_task("bad-handoff")
    finally:
        reset_agent_control(token)

    assert result == "Task failed. Error: handoff invalid"
    info = await control.get("bad-handoff")
    assert info.status is AgentStatus.FAILED
    assert info.error == "handoff invalid"
    assert [event["type"] for event in events] == [
        "task_started",
        "task_failed",
    ]


async def test_task_tool_passes_parent_context_policy_and_checkpointer(monkeypatch):
    captured: dict[str, object] = {}

    async def run_subagent(**kwargs):
        captured.update(kwargs)
        return "delegated answer"

    _install_task_fakes(monkeypatch, run_subagent)
    context_policy = object()
    checkpointer = object()
    runtime = SimpleNamespace(
        config={
            "configurable": {
                "thread_id": "thread-1",
                "domain": "nuclear-electronics",
                "tool_policy": "ne_default",
                "context_policy": context_policy,
                "agent_checkpointer": checkpointer,
            },
            "metadata": {"trace_id": "trace-1"},
        }
    )

    result = await task_tool_module.task_tool.coroutine(
        runtime=runtime,
        description="delegate",
        prompt="do the work",
        subagent_type="general-purpose",
        tool_call_id="context-task",
        max_turns=2,
    )

    assert result.startswith("Task Succeeded.")
    assert captured["context_policy"] is context_policy
    assert captured["checkpointer"] is checkpointer


async def test_run_subagent_uses_semantic_turn_limit_and_shared_checkpointer(
    monkeypatch,
):
    captured: dict[str, object] = {}

    class FakeGraph:
        async def astream_events(self, *_args, **_kwargs):
            yield {
                "event": "on_chat_model_end",
                "data": {"output": AIMessage(content="delegated answer")},
            }

    def create_graph(**kwargs):
        captured.update(kwargs)
        return FakeGraph()

    monkeypatch.setattr(task_tool_module, "_build_subagent_tools", lambda **_kwargs: [])
    monkeypatch.setattr(task_tool_module, "create_chat_model", lambda **_kwargs: object())
    monkeypatch.setattr(task_tool_module, "create_agent_graph", create_graph)
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: lambda _event: None)
    monkeypatch.setattr(task_tool_module, "get_subagent_execution_budget", lambda: None)
    checkpointer = object()
    context_policy = ContextPolicy.from_env(None)
    spec = RuntimeAgentSpec(
        name="general-purpose",
        description="delegate",
        system_prompt="perform the task",
        allowed_tools=set(),
        max_turns=7,
        timeout_seconds=5,
    )

    answer = await task_tool_module._run_subagent(
        subagent_cfg=spec,
        description="delegate",
        prompt="do the work",
        model_name=None,
        max_turns=3,
        thread_id="thread-1",
        kb_ids=[],
        use_search=False,
        selected_skills=set(),
        domain="nuclear-electronics",
        workflow_name="full",
        tool_policy="ne_default",
        tool_runtime_context=None,
        task_id="child-1",
        trace_id="trace-1",
        context_policy=context_policy,
        checkpointer=checkpointer,
    )

    assert answer == "delegated answer"
    assert captured["checkpointer"] is checkpointer
    turn_limits = [
        middleware
        for middleware in captured["extra_middleware"]
        if isinstance(middleware, ModelTurnLimitMiddleware)
    ]
    assert len(turn_limits) == 1
    assert turn_limits[0].max_turns == 3
