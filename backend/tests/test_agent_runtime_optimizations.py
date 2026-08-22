from __future__ import annotations

import asyncio
import json
import threading

import pytest

from backend.api.dependencies.auth import ApiKeyAuth
from backend.api.routers.agent_runtime import cancel_live_agent_run
from backend.application.agent_service import AgentService
from backend.application.checkpointing.checkpointer import init_app_checkpointer
from backend.application.agent_trace import AgentTraceRecorder, AgentTraceStore
from backend.application.agent_workflows.standard_agent_workflow import (
    StandardAgentWorkflow,
)
from backend.application.agents.runtime_agent_loader import RuntimeAgentLoader
from backend.application.policies import ProfileRuntimePolicy
from backend.application.policies.knowledge_base_access import KnowledgeBaseAccessPolicy
from backend.config.settings import settings
from backend.core.agents.runtime_manager import (
    create_agent_runtime_manager,
    get_live_agent_runtime_manager,
    unregister_agent_runtime_manager,
)
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_agent import RuntimeAgentSpec
from backend.domain.agents.runtime_context import AgentRuntimeContext


@pytest.mark.asyncio
async def test_resolve_workflow_offloads_sync_policy_lookup_from_event_loop():
    """A blocking policy/DB lookup must not prevent the event loop from ticking."""
    started = threading.Event()
    release = threading.Event()
    event_loop_thread_id = threading.get_ident()
    worker_thread_ids: list[int] = []
    service = object.__new__(AgentService)

    def blocking_resolve(_request):
        worker_thread_ids.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2)
        return "workflow", "context"

    service._resolve_workflow = blocking_resolve
    resolve_task = asyncio.create_task(service.resolve_workflow(object()))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)

        assert started.is_set()
        assert not resolve_task.done()
        assert worker_thread_ids != [event_loop_thread_id]

        # This scheduling point is the regression assertion: it is reachable
        # while the synchronous lookup is still waiting in the worker thread.
        await asyncio.sleep(0)
    finally:
        release.set()

    assert await resolve_task == ("workflow", "context")


def test_service_captures_one_request_runtime_contract():
    profile = AgentProfile(
        code="test",
        name="Test agent",
        domain="general",
        default_model="test-model",
        default_workflow="lightweight",
    )
    spec = RuntimeAgentSpec(
        name="test-child",
        description="test child",
        system_prompt="You are a test child.",
    )

    class ProfileLoader:
        def load(self, agent_code: str):
            assert agent_code == "test"
            return profile

    class RuntimeLoader:
        def load_all(self):
            return {spec.name: spec}

    service = AgentService(
        profile_loader=ProfileLoader(),
        runtime_agent_loader=RuntimeLoader(),
        runtime_policy=ProfileRuntimePolicy(
            KnowledgeBaseAccessPolicy(
                lookup=lambda _ids: {},
                binding_lookup=lambda _code: [],
                default_lookup=lambda: [],
            )
        ),
    )
    workflow, context = service._resolve_workflow(
        AgentRequest(
            question="hello",
            agent_code="test",
            user_id="user-1",
        )
    )

    assert context.runtime_budget is not None
    assert context.context_policy is not None
    assert context.agent_specs == {spec.name: spec}
    assert workflow._runtime_budget is context.runtime_budget
    assert workflow._agent_specs == context.agent_specs
    assert workflow._context_policy is context.context_policy


@pytest.mark.asyncio
async def test_standard_workflow_finishes_trace_when_consumer_is_cancelled(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "follow_up_suggestions_enabled", False)
    release = asyncio.Event()

    class FakeExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config, **kwargs):
            del question, thread_id, runnable_config, kwargs
            yield AgentEvent(
                type="message.delta",
                request_id="ignored",
                thread_id="ignored",
                sequence=1,
                payload={"content": "partial"},
            )
            await release.wait()

    class FakeRegistry:
        def build(self, profile, **kwargs):
            del profile, kwargs
            return FakeExecutor()

    profile = AgentProfile(
        code="test",
        name="Test agent",
        domain="general",
        default_model="test-model",
    )
    context = AgentRuntimeContext(
        request_id="cancel-trace-run",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="user-1:thread-1",
        agent_profile=profile,
        workflow_name="full",
        model_name="test-model",
        trace_enabled=True,
        request_metadata={"agent_trace_root": str(tmp_path)},
    )
    request = AgentRequest(
        question="cancel me",
        agent_code="test",
        user_id="user-1",
    )
    stream = StandardAgentWorkflow(
        name="full",
        executor_registry=FakeRegistry(),
    ).run(request, context)

    assert (await anext(stream)).type == "workflow.started"
    assert (await anext(stream)).type == "message.delta"
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    records = AgentTraceStore(tmp_path).read_records("cancel-trace-run")
    runs = [record.payload for record in records if record.kind == "run"]
    assert [item["status"] for item in runs] == ["running", "cancelled"]
    assert not any(
        record.kind == "event" and record.payload.get("event_type") == "done"
        for record in records
    )


def test_trace_recorder_redacts_sensitive_tool_payloads(tmp_path):
    recorder = AgentTraceRecorder(
        run_id="redaction-run",
        workflow_name="full",
        enabled=True,
        store=AgentTraceStore(tmp_path),
    )
    recorder.record_tool_call(
        tool_call_id="tool-1",
        tool_name="test",
        status="completed",
        started_at="2026-07-14T00:00:00Z",
        args_preview={
            "api_key": "do-not-store",
            "nested": {"token": "also-do-not-store"},
            "query": "Bearer bearer-secret",
        },
        result_preview="password=secret sk-1234567890123456",
        error="authorization: bearer-error",
        metadata={"client_secret": "metadata-secret"},
    )

    payload = next(
        record.payload
        for record in AgentTraceStore(tmp_path).read_records("redaction-run")
        if record.kind == "tool_call"
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    for secret in (
        "do-not-store",
        "also-do-not-store",
        "bearer-secret",
        "1234567890123456",
        "bearer-error",
        "metadata-secret",
    ):
        assert secret not in serialized
    assert payload["args_preview"]["api_key"] == "[REDACTED]"


def test_runtime_agent_loader_reloads_changed_manifest(tmp_path):
    path = tmp_path / "runtime_agents.yaml"
    path.write_text(
        """agents:\n  child:\n    description: first\n    system_prompt: prompt\n""",
        encoding="utf-8",
    )
    loader = RuntimeAgentLoader(path)
    assert loader.load_all()["child"].description == "first"

    path.write_text(
        """agents:\n  child:\n    description: changed description\n    system_prompt: prompt\n""",
        encoding="utf-8",
    )
    assert loader.load_all()["child"].description == "changed description"


@pytest.mark.asyncio
async def test_live_runtime_cancel_is_owner_scoped():
    manager = create_agent_runtime_manager(
        "live-cancel-run",
        run_metadata={"user_id": "owner-1"},
    )
    try:
        with pytest.raises(Exception) as wrong_owner:
            await cancel_live_agent_run(
                "live-cancel-run",
                ApiKeyAuth(api_key="", user_id="other", role="user"),
            )
        assert getattr(wrong_owner.value, "status_code", None) == 404

        result = await cancel_live_agent_run(
            "live-cancel-run",
            ApiKeyAuth(api_key="", user_id="owner-1", role="user"),
        )
        assert result == {
            "run_id": "live-cancel-run",
            "status": "cancelling",
            "scope": "current_worker",
        }
        assert get_live_agent_runtime_manager("live-cancel-run") is None
    finally:
        unregister_agent_runtime_manager("live-cancel-run", manager)


@pytest.mark.asyncio
async def test_checkpointer_backend_selection_fails_closed(monkeypatch):
    monkeypatch.setenv("AGENT_CHECKPOINTER_BACKEND", "unknown")
    with pytest.raises(ValueError, match="AGENT_CHECKPOINTER_BACKEND"):
        await init_app_checkpointer(object(), "checkpoints.db")
