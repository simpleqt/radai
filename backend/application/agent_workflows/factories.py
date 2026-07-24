"""Workflow provider factories used by the YAML workflow registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.application.agent_workflows.standard_agent_workflow import (
    StandardAgentWorkflow,
)
from backend.domain.agents.runtime_agent import RuntimeAgentSpec
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
from backend.domain.agents.workflows import get_workflow_spec

if TYPE_CHECKING:
    from backend.application.agents.definition import ResolvedAgentDefinition


def create_standard_workflow(
    *,
    name: str,
    checkpointer: BaseCheckpointSaver | None = None,
    runnable_config: RunnableConfig | None = None,
    runtime_budget: AgentRuntimeBudget | None = None,
    agent_specs: dict[str, RuntimeAgentSpec] | None = None,
    context_policy: object | None = None,
    resolved_definition: ResolvedAgentDefinition | None = None,
):
    spec = (
        next(
            (item for item in resolved_definition.workflows if item.name == name),
            None,
        )
        if resolved_definition is not None
        else get_workflow_spec(name)
    )
    return StandardAgentWorkflow(
        name=name,
        execution_strategy=(
            getattr(spec, "execution_strategy", "graph")
            if spec is not None
            else "graph"
        ),
        checkpointer=checkpointer,
        runnable_config=runnable_config,
        runtime_budget=runtime_budget,
        agent_specs=agent_specs,
        context_policy=context_policy,
        resolved_definition=resolved_definition,
    )


def create_coordinated_workflow(
    *,
    name: str,
    checkpointer: BaseCheckpointSaver | None = None,
    runnable_config: RunnableConfig | None = None,
    runtime_budget: AgentRuntimeBudget | None = None,
    agent_specs: dict[str, RuntimeAgentSpec] | None = None,
    context_policy: object | None = None,
    resolved_definition: ResolvedAgentDefinition | None = None,
):
    return CoordinatedAgentWorkflow(
        name=name,
        checkpointer=checkpointer,
        runnable_config=runnable_config,
        runtime_budget=runtime_budget,
        agent_specs=agent_specs,
        context_policy=context_policy,
        resolved_definition=resolved_definition,
    )
