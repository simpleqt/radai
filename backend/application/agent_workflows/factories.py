"""Workflow provider factories used by the YAML workflow registry."""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from backend.application.agent_workflows.coordinated_agent_workflow import CoordinatedAgentWorkflow
from backend.application.agent_workflows.standard_agent_workflow import StandardAgentWorkflow


def create_standard_workflow(
    *,
    name: str,
    checkpointer: BaseCheckpointSaver | None = None,
    runnable_config: RunnableConfig | None = None,
):
    return StandardAgentWorkflow(
        name=name,
        checkpointer=checkpointer,
        runnable_config=runnable_config,
    )


def create_coordinated_workflow(
    *,
    name: str,
    checkpointer: BaseCheckpointSaver | None = None,
    runnable_config: RunnableConfig | None = None,
):
    return CoordinatedAgentWorkflow(
        name=name,
        checkpointer=checkpointer,
        runnable_config=runnable_config,
    )
