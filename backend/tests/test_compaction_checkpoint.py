from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from backend.application.agent_executors.graph_builder import (
    ProfiledGraphBuildConfig,
    ProfiledGraphBuilder,
)
from backend.application.agent_executors.profiled_executor import ProfiledAgentExecutor
from backend.core.agents.context.compaction_checkpoint import (
    CompactionCheckpointCoordinator,
    CompactionCheckpointIntegrityError,
    CompactionCheckpointUnavailable,
)
from backend.core.agents.context.history_compactor import HistoryCompactionResult
from backend.core.agents.middlewares.context_pruning import ContextPruningMiddleware
from backend.core.agents.state import merge_compaction_history


def _compaction_result(
    *,
    window_id: str = "ctxwin_test",
    summary: str = "compacted summary",
) -> HistoryCompactionResult:
    summary_message = HumanMessage(
        content=summary,
        id=f"compaction_summary_{window_id}",
        additional_kwargs={
            "context_compaction": {
                "window_id": window_id,
                "trigger": "test",
            }
        },
    )
    return HistoryCompactionResult(
        window_id=window_id,
        previous_window_id="ctxwin_previous",
        summary=summary,
        messages=[summary_message],
        tokens_before=1200,
        tokens_after=300,
        retained_user_messages=0,
        removed_messages=8,
        summary_tokens=40,
        fixed_request_tokens=0,
        threshold_tokens=1024,
        trigger="test",
        created_at=123.0,
    )


class _FakeCheckpointer:
    def __init__(self) -> None:
        self.channel_values: dict = {}
        self.requested_configs: list[dict] = []

    async def aget_tuple(self, config: dict):
        self.requested_configs.append(config)
        if not self.channel_values:
            return None
        configurable = dict(config.get("configurable") or {})
        configurable["checkpoint_id"] = "checkpoint-physical-7"
        return SimpleNamespace(
            config={"configurable": configurable},
            checkpoint={"channel_values": self.channel_values},
            metadata={"step": 7},
        )


def test_compaction_binding_is_idempotent_and_embedded_in_checkpoint_state():
    saver = _FakeCheckpointer()
    coordinator = CompactionCheckpointCoordinator(
        checkpointer=saver,
        thread_id="user-1:thread-1",
    )
    result = _compaction_result()

    first = coordinator.bind(result)
    second = coordinator.bind(result)

    assert first.reference is not None
    assert second.reference is not None
    assert first.reference.logical_id == second.reference.logical_id
    assert first.reference.durable is True
    assert first.state_payload["checkpoint_ref"] == first.reference.as_dict()
    message_metadata = first.messages[0].additional_kwargs["context_compaction"]
    assert message_metadata["checkpoint_ref"] == first.reference.as_dict()
    # The message content already carries the summary text. Duplicating it
    # into additional_kwargs double-charged count_message_tokens (defeating
    # the compaction-benefit check) and doubled the checkpoint footprint.
    assert "summary" not in message_metadata
    assert first.state_payload["summary"] == result.summary

    history = merge_compaction_history(
        [first.state_payload],
        [deepcopy(first.state_payload)],
    )
    assert len(history) == 1


async def test_compaction_recovery_returns_logical_and_physical_checkpoint_ids():
    saver = _FakeCheckpointer()
    coordinator = CompactionCheckpointCoordinator(
        checkpointer=saver,
        thread_id="user-1:thread-1",
        checkpoint_ns="agent",
    )
    bound = coordinator.bind(_compaction_result())
    saver.channel_values = {
        "messages": bound.messages,
        "context_compaction": bound.state_payload,
        "context_checkpoint_ref": bound.reference.as_dict(),
    }

    recovered = await coordinator.arecover()

    assert recovered is not None
    assert recovered.legacy is False
    assert recovered.reference.logical_id == bound.reference.logical_id
    assert recovered.reference.checkpoint_id == "checkpoint-physical-7"
    assert recovered.reference.thread_id == "user-1:thread-1"
    assert recovered.checkpoint_metadata == {"step": 7}
    assert saver.requested_configs == [
        {
            "configurable": {
                "thread_id": "user-1:thread-1",
                "checkpoint_ns": "agent",
            }
        }
    ]
    updates = recovered.state_updates()
    assert updates["context_window_id"] == "ctxwin_test"
    assert (
        updates["context_checkpoint_ref"]["checkpoint_id"]
        == "checkpoint-physical-7"
    )


async def test_compaction_recovery_upgrades_legacy_checkpoint_in_memory():
    saver = _FakeCheckpointer()
    result = _compaction_result()
    saver.channel_values = {
        "messages": result.messages,
        "context_compaction": result.state_payload(),
    }
    coordinator = CompactionCheckpointCoordinator(
        checkpointer=saver,
        thread_id="legacy-thread",
    )

    recovered = await coordinator.arecover()

    assert recovered is not None
    assert recovered.legacy is True
    assert recovered.reference.thread_id == "legacy-thread"
    assert recovered.state_payload["checkpoint_ref"]["logical_id"].startswith(
        "cmp_"
    )
    assert recovered.messages[0].additional_kwargs["context_compaction"][
        "checkpoint_ref"
    ]


async def test_compaction_recovery_rejects_cross_thread_reference():
    saver = _FakeCheckpointer()
    original = CompactionCheckpointCoordinator(
        checkpointer=saver,
        thread_id="thread-a",
    ).bind(_compaction_result())
    saver.channel_values = {
        "messages": original.messages,
        "context_compaction": original.state_payload,
    }
    other_thread = CompactionCheckpointCoordinator(
        checkpointer=saver,
        thread_id="thread-b",
    )

    with pytest.raises(CompactionCheckpointIntegrityError, match="thread_id"):
        await other_thread.arecover()


async def test_compaction_recovery_requires_durable_checkpointer():
    coordinator = CompactionCheckpointCoordinator(thread_id="thread-1")

    with pytest.raises(CompactionCheckpointUnavailable):
        await coordinator.arecover()


def test_context_pruning_persists_same_binding_in_message_and_thread_state():
    saver = _FakeCheckpointer()
    middleware = ContextPruningMiddleware(
        thread_id="user-1:thread-1",
        checkpointer=saver,
    )

    updates = middleware._apply_compaction_result(
        {},
        None,
        _compaction_result(),
    )

    assert updates is not None
    reference = updates["context_checkpoint_ref"]
    assert reference["thread_id"] == "user-1:thread-1"
    assert reference["durable"] is True
    assert updates["context_compaction"]["checkpoint_ref"] == reference
    assert (
        updates["messages"][0]
        .additional_kwargs["context_compaction"]["checkpoint_ref"]
        == reference
    )


async def test_profiled_executor_exposes_common_compaction_recovery_boundary():
    saver = _FakeCheckpointer()
    coordinator = CompactionCheckpointCoordinator(
        checkpointer=saver,
        thread_id="thread-1",
    )
    bound = coordinator.bind(_compaction_result())
    saver.channel_values = {
        "messages": bound.messages,
        "context_compaction": bound.state_payload,
    }
    executor = object.__new__(ProfiledAgentExecutor)
    executor._compaction_checkpoints = coordinator

    recovered = await executor.arecover_compaction_checkpoint()

    assert recovered is not None
    assert executor.compaction_checkpoints is coordinator
    assert recovered.reference.checkpoint_id == "checkpoint-physical-7"


def test_profiled_graph_builder_connects_shared_checkpointer_to_compaction(
    monkeypatch,
):
    import backend.core.agents as agent_core
    import backend.core.models as core_models

    saver = _FakeCheckpointer()
    captured: dict = {}

    def fake_create_agent_graph(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(name="test-graph")

    monkeypatch.setattr(agent_core, "create_agent_graph", fake_create_agent_graph)
    monkeypatch.setattr(core_models, "create_chat_model", lambda **kwargs: object())
    builder = ProfiledGraphBuilder(
        ProfiledGraphBuildConfig(
            label="test",
            graph_name="test-graph",
            domain="test",
            tool_policy="test",
            workflow_name="full",
            model_name=None,
            thinking_enabled=False,
            subagent_enabled=False,
            use_search=False,
            kb_ids=[],
            selected_skills=set(),
            skills_provider=SimpleNamespace(),
            task_type=None,
            lightweight_mode=False,
            lightweight_has_kb=False,
            thread_id="user-1:thread-1",
            external_thread_id="thread-1",
            max_concurrent_subagents=1,
            checkpointer=saver,
        )
    )
    monkeypatch.setattr(builder, "_assemble_tools", lambda **kwargs: [])
    monkeypatch.setattr(builder, "_create_summarization_middleware", lambda policy: False)
    monkeypatch.setattr(builder, "_build_system_prompt", lambda: "test")

    result = builder.build()

    context_pruning = captured["extra_middleware"][0]
    assert context_pruning.compaction_checkpoints.durable is True
    assert (
        context_pruning.compaction_checkpoints.thread_id
        == "user-1:thread-1"
    )
    assert result.compaction_checkpoints is context_pruning.compaction_checkpoints
