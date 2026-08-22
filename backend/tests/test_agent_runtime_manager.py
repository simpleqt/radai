from __future__ import annotations

import pytest

from backend.core.agents.control import AgentStateError
from backend.core.agents.runtime_manager import (
    AgentRuntimeManager,
    AgentStateMachine,
    AgentStatus,
    InMemoryAgentRuntimeManager,
    RunBudget,
    get_current_agent_id,
    get_agent_runtime_manager,
    reset_agent_runtime_manager,
    set_agent_runtime_manager,
)


def test_in_memory_backend_implements_runtime_manager_contract():
    budget = RunBudget(
        max_spawned_agents=3,
        max_active_agents=2,
        max_depth=1,
    )
    manager = InMemoryAgentRuntimeManager("root", budget=budget)

    assert isinstance(manager, AgentRuntimeManager)
    assert manager.budget is budget
    assert manager.limits is budget


def test_state_machine_rejects_transition_out_of_terminal_state():
    assert AgentStateMachine.can_transition(
        AgentStatus.PENDING,
        AgentStatus.RUNNING,
    )
    with pytest.raises(AgentStateError, match="completed to running"):
        AgentStateMachine.validate(
            AgentStatus.COMPLETED,
            AgentStatus.RUNNING,
            agent_id="child",
        )


async def test_root_cancellation_token_propagates_reason_to_children():
    manager = InMemoryAgentRuntimeManager("root")
    await manager.register("child")
    child_token = await manager.token_for("child")

    await manager.interrupt("root", reason="client disconnected")

    assert manager.cancellation.cancelled is True
    assert manager.cancellation.reason == "client disconnected"
    assert child_token.cancelled is True
    assert child_token.reason == "client disconnected"
    await child_token.wait()


def test_runtime_manager_context_is_interface_typed_and_request_scoped():
    manager = InMemoryAgentRuntimeManager("root")
    token = set_agent_runtime_manager(manager)
    try:
        assert get_agent_runtime_manager() is manager
    finally:
        reset_agent_runtime_manager(token)
    assert get_agent_runtime_manager() is None


async def test_spawn_scopes_current_agent_id_to_execution_lease():
    manager = InMemoryAgentRuntimeManager("root")

    assert get_current_agent_id() is None
    async with manager.spawn("child"):
        assert get_current_agent_id() == "child"
    assert get_current_agent_id() is None
