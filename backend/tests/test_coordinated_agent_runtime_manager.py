from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
    _cancel_task_bounded,
)
from backend.application.agent_workflows.coordination_unit_runner import (
    CoordinationUnitRunner,
)
from backend.application.agent_workflows.research_branch_runner import (
    ResearchBranchRunner,
)
from backend.application.agent_workflows.research_scheduler import ResearchScheduler
from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
    ResearchTask,
)
from backend.core.agents.runtime_manager import (
    AgentStatus,
    InMemoryAgentRuntimeManager,
    get_agent_runtime_manager,
    reset_agent_runtime_manager,
    set_agent_runtime_manager,
)
from backend.core.agents.runtime_store import SQLiteAgentRuntimeStore
from backend.domain.agents.runtime_budget import AgentRuntimeBudget


def _workflow() -> CoordinatedAgentWorkflow:
    return CoordinatedAgentWorkflow(
        name="plan_solve",
        delegate=SimpleNamespace(),
    )


def _context():
    return SimpleNamespace(
        request_id="request-1",
        internal_thread_id="thread-1",
    )


def test_workflow_composes_runtime_execution_components():
    workflow = _workflow()

    assert isinstance(workflow._research_scheduler, ResearchScheduler)
    assert isinstance(workflow._research_branch_runner, ResearchBranchRunner)
    assert isinstance(workflow._unit_runner, CoordinationUnitRunner)
    assert workflow._research_scheduler._host is workflow
    assert workflow._research_branch_runner._host is workflow
    assert workflow._unit_runner._host is workflow


async def test_coordination_unit_uses_shared_runtime_manager(monkeypatch):
    workflow = _workflow()

    async def run_unmanaged(**_kwargs):
        return CoordinationUnitResult(role="planner", answer="plan")

    monkeypatch.setattr(workflow, "_run_unit_unmanaged", run_unmanaged)
    manager = InMemoryAgentRuntimeManager("root")
    token = set_agent_runtime_manager(manager)
    try:
        result = await workflow._run_unit(
            role="planner",
            question="plan",
            request=SimpleNamespace(),
            context=_context(),
            tool_access=False,
        )
    finally:
        reset_agent_runtime_manager(token)

    assert result.answer == "plan"
    info = await manager.get("request-1:plan_solve:planner")
    assert info.status is AgentStatus.COMPLETED
    assert info.metadata == {
        "workflow": "plan_solve",
        "role": "planner",
        "task_id": None,
        "thread_id": (
            "thread-1:coord:plan_solve:request-1:planner"
        ),
        "parent_thread_id": "thread-1",
        "model_name": None,
        "model_tier": "primary",
    }


async def test_encoded_coordination_failure_marks_manager_failed(monkeypatch):
    workflow = _workflow()

    async def run_unmanaged(**_kwargs):
        return CoordinationUnitResult(
            role="researcher",
            answer="partial",
            error="required evidence missing",
        )

    monkeypatch.setattr(workflow, "_run_unit_unmanaged", run_unmanaged)
    manager = InMemoryAgentRuntimeManager("root")
    token = set_agent_runtime_manager(manager)
    try:
        result = await workflow._run_unit(
            role="researcher",
            question="research",
            request=SimpleNamespace(),
            context=_context(),
            task_id="research-1",
        )
    finally:
        reset_agent_runtime_manager(token)

    assert result.error == "required evidence missing"
    info = await manager.get("research-1")
    assert info.status is AgentStatus.FAILED
    assert info.error == "required evidence missing"


async def test_encoded_coordination_timeout_marks_manager_timed_out(monkeypatch):
    workflow = _workflow()

    async def run_unmanaged(**_kwargs):
        return CoordinationUnitResult(
            role="researcher",
            answer="partial",
            error="researcher execution exceeded hard timeout after 30s",
            timed_out=True,
        )

    monkeypatch.setattr(workflow, "_run_unit_unmanaged", run_unmanaged)
    manager = InMemoryAgentRuntimeManager("root")
    token = set_agent_runtime_manager(manager)
    try:
        result = await workflow._run_unit(
            role="researcher",
            question="research",
            request=SimpleNamespace(),
            context=_context(),
            task_id="research-timeout",
        )
    finally:
        reset_agent_runtime_manager(token)

    assert result.timed_out is True
    info = await manager.get("research-timeout")
    assert info.status is AgentStatus.TIMED_OUT
    assert info.error == result.error


async def test_direct_workflow_run_owns_and_closes_one_manager(monkeypatch):
    workflow = _workflow()
    captured = {}

    async def run_impl(_request, _context):
        captured["manager"] = get_agent_runtime_manager()
        yield "event"

    monkeypatch.setattr(workflow, "_run_impl", run_impl)

    events = [
        event
        async for event in workflow.run(
            SimpleNamespace(),
            _context(),
        )
    ]

    assert events == ["event"]
    assert get_agent_runtime_manager() is None
    assert captured["manager"] is not None
    assert captured["manager"].budget.max_active_agents == 16
    assert captured["manager"].closed is True
    assert captured["manager"].cancellation.cancelled is True


async def test_deep_research_root_budget_does_not_expire_final_convergence(
    monkeypatch,
):
    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=SimpleNamespace(),
    )
    captured = {}

    async def run_impl(_request, _context):
        captured["manager"] = get_agent_runtime_manager()
        yield "final report"

    monkeypatch.setattr(workflow, "_run_impl", run_impl)

    events = [
        event
        async for event in workflow.run(
            SimpleNamespace(),
            _context(),
        )
    ]

    assert events == ["final report"]
    assert captured["manager"].budget.max_elapsed_seconds is None


async def test_deep_research_tree_capacity_covers_all_bounded_retries(monkeypatch):
    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=SimpleNamespace(),
        runtime_budget=AgentRuntimeBudget(
            max_concurrent_research=5,
            deep_research_reflection_rounds=2,
            deep_research_followup_tasks=5,
            deep_research_structured_output_attempts=3,
        ),
    )
    captured = {}

    async def run_impl(_request, _context):
        captured["manager"] = get_agent_runtime_manager()
        yield "final report"

    monkeypatch.setattr(workflow, "_run_impl", run_impl)

    assert [
        event async for event in workflow.run(SimpleNamespace(), _context())
    ] == ["final report"]
    assert captured["manager"].budget.max_spawned_agents == 25


async def test_direct_workflow_persists_root_outcome_when_enabled(
    tmp_path,
    monkeypatch,
):
    workflow = _workflow()
    path = tmp_path / "coordinated-runtime.sqlite3"
    monkeypatch.setenv("AGENT_RUNTIME_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_RUNTIME_STORE_PATH", str(path))

    async def run_impl(_request, _context):
        yield "event"

    monkeypatch.setattr(workflow, "_run_impl", run_impl)

    assert [
        event
        async for event in workflow.run(SimpleNamespace(), _context())
    ] == ["event"]

    store = SQLiteAgentRuntimeStore(path)
    run = await store.load_run("request-1")
    events = await store.load_lifecycle_events("request-1")
    assert run is not None
    assert run.status == "completed"
    assert run.metadata["degraded"] is False
    assert run.metadata["completion_status"] == "completed"
    assert [event.event_type for event in events] == [
        "agent_tree.started",
        "agent_tree.completed",
    ]


async def test_direct_workflow_persists_degraded_business_outcome(
    tmp_path,
    monkeypatch,
):
    workflow = _workflow()
    path = tmp_path / "coordinated-runtime-degraded.sqlite3"
    monkeypatch.setenv("AGENT_RUNTIME_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_RUNTIME_STORE_PATH", str(path))

    async def run_impl(_request, _context):
        yield SimpleNamespace(
            type="coordination.completed",
            payload={"status": "degraded", "degraded": True},
        )

    monkeypatch.setattr(workflow, "_run_impl", run_impl)

    events = [
        event async for event in workflow.run(SimpleNamespace(), _context())
    ]

    assert events[0].payload["degraded"] is True
    run = await SQLiteAgentRuntimeStore(path).load_run("request-1")
    assert run is not None
    # Operationally the tree completed; the answer quality is recorded
    # separately so existing terminal-status consumers remain compatible.
    assert run.status == "completed"
    assert run.metadata["degraded"] is True
    assert run.metadata["completion_status"] == "degraded"


async def test_research_branch_uses_one_lease_for_prefetch_unit_and_validation(
    monkeypatch,
):
    workflow = _workflow()

    async def run_unit_unmanaged(**_kwargs):
        return CoordinationUnitResult(role="researcher", answer="evidence")

    async def run_research_unmanaged(**kwargs):
        return await workflow._run_unit(
            role=kwargs["role"],
            question="analyze evidence",
            request=kwargs["request"],
            context=kwargs["context"],
            task_id="request-1:research:0",
        )

    monkeypatch.setattr(workflow, "_run_unit_unmanaged", run_unit_unmanaged)
    monkeypatch.setattr(
        workflow,
        "_run_research_task_unmanaged",
        run_research_unmanaged,
    )
    manager = InMemoryAgentRuntimeManager("root")
    token = set_agent_runtime_manager(manager)
    try:
        result = await workflow._run_research_task(
            task=ResearchTask(key="r1", title="Research", question="question"),
            index=0,
            role="researcher",
            kb_circuit_open=asyncio.Event(),
            request=SimpleNamespace(),
            context=_context(),
        )
    finally:
        reset_agent_runtime_manager(token)

    assert result.answer == "evidence"
    assert manager.spawned_count == 1
    assert (
        await manager.get("request-1:research:0")
    ).status is AgentStatus.COMPLETED


async def test_coordination_cleanup_is_bounded_for_cancel_resistant_task():
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    async def resist_one_cancel():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()

    task = asyncio.create_task(resist_one_cancel())
    await asyncio.sleep(0)

    assert await _cancel_task_bounded(task, timeout=0.01) is False
    assert cancellation_seen.is_set() is True

    release.set()
    await asyncio.wait_for(task, timeout=0.5)


async def test_research_final_contract_failure_marks_whole_branch_failed(
    monkeypatch,
):
    workflow = _workflow()

    async def run_research_unmanaged(**_kwargs):
        return CoordinationUnitResult(
            role="researcher",
            answer="partial handoff",
            error="required tool missing",
        )

    monkeypatch.setattr(
        workflow,
        "_run_research_task_unmanaged",
        run_research_unmanaged,
    )
    manager = InMemoryAgentRuntimeManager("root")
    token = set_agent_runtime_manager(manager)
    try:
        result = await workflow._run_research_task(
            task=ResearchTask(key="r1", title="Research", question="question"),
            index=0,
            role="researcher",
            kb_circuit_open=asyncio.Event(),
            request=SimpleNamespace(),
            context=_context(),
        )
    finally:
        reset_agent_runtime_manager(token)

    assert result.error == "required tool missing"
    branch = await manager.get("request-1:research:0")
    assert branch.status is AgentStatus.FAILED
    assert branch.error == "required tool missing"
