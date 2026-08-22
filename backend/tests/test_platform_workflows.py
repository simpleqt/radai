"""工作流注册表与标准工作流契约（拆自 test_agent_platform_standardization.py）。"""

import inspect
import pytest

from backend.application.agent_executors import (
    ProfiledGraphBuildConfig,
    ProfiledGraphBuilder,
)
from backend.application.agent_service import AgentService
from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.application.agent_workflows.registry import WorkflowRegistry
from backend.application.agent_workflows.standard_agent_workflow import (
    StandardAgentWorkflow,
)
from backend.domain.agents import workflows as workflows_module
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext
from backend.domain.agents.workflows import (
    PLAN_SOLVE_WORKFLOW,
    SUPPORTED_WORKFLOWS,
    resolve_workflow_capabilities,
)
from types import SimpleNamespace


def _context(profile: AgentProfile) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        request_id="req-1",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="user-1:thread-1",
        agent_profile=profile,
        workflow_name="lightweight",
        model_name=None,
    )


def test_workflow_registry_resolves_full_when_skills_selected():
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    request = AgentRequest(
        question="hello",
        agent_code="ne",
        user_id="user-1",
        selected_skills=["nuclear-electronics"],
    )

    workflow_name = WorkflowRegistry().resolve_name(request, _context(profile))

    assert workflow_name == "full"


def test_workflow_registry_keeps_profile_default_for_implicitly_required_skills():
    profile = AgentProfile(
        code="ne",
        name="NE",
        domain="nuclear-electronics",
        default_workflow="lightweight",
    )
    request = AgentRequest(
        question="hello",
        agent_code="ne",
        user_id="user-1",
        selected_skills=["nuclear-electronics"],
        metadata={"_skills_requested_for_workflow": False},
    )

    workflow_name = WorkflowRegistry().resolve_name(request, _context(profile))

    assert workflow_name == "lightweight"


def test_workflow_registry_normalizes_deep_research_aliases():
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    for alias in ["deep-research", "deep_research", "deepresearch"]:
        request = AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow=alias,
        )
        workflow_name = WorkflowRegistry().resolve_name(request, _context(profile))
        assert workflow_name == "deep_research"


def test_workflow_registry_normalizes_plan_solve_aliases():
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    for alias in ["plan-solve", "plan_solve", "plansolve"]:
        request = AgentRequest(
            question="plan",
            agent_code="ne",
            user_id="user-1",
            requested_workflow=alias,
        )
        workflow_name = WorkflowRegistry().resolve_name(request, _context(profile))
        assert workflow_name == PLAN_SOLVE_WORKFLOW


def test_workflow_registry_rejects_explicit_unknown_instead_of_default_fallback():
    profile = AgentProfile(
        code="ne",
        name="NE",
        domain="nuclear-electronics",
        default_workflow="full",
    )
    request = AgentRequest(
        question="research",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="deep-reseach",
    )

    with pytest.raises(ValueError, match="Unknown requested workflow: deep-reseach"):
        WorkflowRegistry().resolve_name(request, _context(profile))


def test_workflow_registry_uses_skill_preferred_workflow():
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    request = AgentRequest(
        question="research",
        agent_code="ne",
        user_id="user-1",
        selected_skills=["deep-research"],
    )

    workflow_name = WorkflowRegistry().resolve_name(request, _context(profile))

    assert workflow_name == PLAN_SOLVE_WORKFLOW


@pytest.mark.asyncio
async def test_resolved_subset_of_profile_default_skills_does_not_force_full_workflow(monkeypatch):
    import backend.db.connection as db_connection
    from backend.application.skills import runtime_resolver

    class FakeProfileLoader:
        def load(self, agent_code):
            return AgentProfile(
                code=agent_code,
                name="NE",
                domain="nuclear-electronics",
                default_skills=["nuclear-electronics", "general-table-analysis"],
                default_workflow="lightweight",
            )

    class FakeSessionFactory:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeSkillRuntimeResolver:
        selected_skills = ["nuclear-electronics"]
        workflows = []

        async def resolve(self, *args, **kwargs):
            self.workflows.append(kwargs.get("workflow"))
            return SimpleNamespace(
                selected_skills=list(self.selected_skills),
                available_skills=[],
                managed=True,
            )

    monkeypatch.setattr(
        db_connection,
        "get_async_session_factory",
        lambda: lambda: FakeSessionFactory(),
    )
    monkeypatch.setattr(
        runtime_resolver,
        "SkillRuntimeResolver",
        FakeSkillRuntimeResolver,
    )

    service = AgentService(profile_loader=FakeProfileLoader())
    resolved = await service.resolve_request_skills(
        AgentRequest(question="q", agent_code="ne", user_id="user-1")
    )

    assert resolved.selected_skills == ["nuclear-electronics"]
    assert resolved.metadata["_skills_requested_for_workflow"] is False

    resolved_with_echoed_default = await service.resolve_request_skills(
        AgentRequest(
            question="q",
            agent_code="ne",
            user_id="user-1",
            selected_skills=["nuclear-electronics"],
        )
    )
    assert resolved_with_echoed_default.metadata["_skills_requested_for_workflow"] is False

    FakeSkillRuntimeResolver.selected_skills = ["nuclear-electronics", "deep-research"]
    resolved_with_extra = await service.resolve_request_skills(
        AgentRequest(question="q", agent_code="ne", user_id="user-1")
    )

    assert resolved_with_extra.metadata["_skills_requested_for_workflow"] is True

    await service.resolve_request_skills(
        AgentRequest(
            question="q",
            agent_code="ne",
            user_id="user-1",
            selected_skills=["deep-research"],
        )
    )
    assert FakeSkillRuntimeResolver.workflows[-1] == "plan_solve"


def test_workflow_registry_uses_coordinated_deep_research_workflow():
    profile = AgentProfile(
        code="ne",
        name="NE",
        domain="nuclear-electronics",
        allowed_workflows=["lightweight", "full", "deep_research"],
    )
    request = AgentRequest(
        question="research",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="deep_research",
    )

    workflow = WorkflowRegistry().resolve(request, _context(profile))

    assert isinstance(workflow, CoordinatedAgentWorkflow)
    assert workflow.name == "deep_research"


def test_workflow_registry_uses_coordinated_workflow_for_plan_solve():
    profile = AgentProfile(
        code="ne",
        name="NE",
        domain="nuclear-electronics",
        allowed_workflows=list(SUPPORTED_WORKFLOWS),
    )
    request = AgentRequest(
        question="plan",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="plan_solve",
    )

    workflow = WorkflowRegistry().resolve(request, _context(profile))

    assert isinstance(workflow, CoordinatedAgentWorkflow)
    assert workflow.name == PLAN_SOLVE_WORKFLOW


def test_workflow_registry_uses_standard_workflow_for_lightweight_and_full():
    profile = AgentProfile(
        code="ne",
        name="NE",
        domain="nuclear-electronics",
        allowed_workflows=["lightweight", "full", "deep_research"],
    )
    lightweight_request = AgentRequest(
        question="hello",
        agent_code="ne",
        user_id="user-1",
    )
    full_request = AgentRequest(
        question="hello",
        agent_code="ne",
        user_id="user-1",
        selected_skills=["nuclear-electronics"],
    )

    lightweight = WorkflowRegistry().resolve(lightweight_request, _context(profile))
    full = WorkflowRegistry().resolve(full_request, _context(profile))

    assert isinstance(lightweight, StandardAgentWorkflow)
    assert isinstance(full, StandardAgentWorkflow)
    assert lightweight.name == "lightweight"
    assert full.name == "full"


def test_workflow_specs_load_from_yaml(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        """
workflows:
  custom_review:
    provider: backend.application.agent_workflows.factories:create_standard_workflow
    aliases: [custom-review]
    capabilities: [custom_review, full]
    full_capability: true
    subagent_enabled: false
""",
        encoding="utf-8",
    )

    specs = workflows_module._load_workflow_specs(path)

    assert "custom_review" in specs
    assert specs["custom_review"].provider.endswith(":create_standard_workflow")
    assert "custom-review" in specs["custom_review"].aliases
    assert "full" in specs["custom_review"].capabilities


def test_workflow_registry_materializes_provider_from_spec(monkeypatch):
    profile = AgentProfile(
        code="custom",
        name="Custom",
        domain="custom-domain",
        allowed_workflows=["custom_review"],
    )
    request = AgentRequest(
        question="review",
        agent_code="custom",
        user_id="user-1",
        requested_workflow="custom_review",
    )
    spec = SimpleNamespace(
        provider="backend.application.agent_workflows.factories:create_standard_workflow"
    )
    monkeypatch.setattr(
        "backend.application.agent_workflows.registry.get_workflow_spec",
        lambda workflow_name: spec,
    )
    registry = WorkflowRegistry()
    registry._workflow_names.add("custom_review")

    workflow = registry.resolve(request, _context(profile))

    assert isinstance(workflow, StandardAgentWorkflow)
    assert workflow.name == "custom_review"


def test_workflow_capabilities_are_config_backed():
    assert resolve_workflow_capabilities(PLAN_SOLVE_WORKFLOW) >= {
        PLAN_SOLVE_WORKFLOW,
        "full",
    }


def test_result_only_graph_unit_disables_tool_assembly(monkeypatch):
    from backend.application.tools.tool_assembly_pipeline import ToolAssemblyPipeline
    from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider

    def unexpected_assembly(self):
        raise AssertionError("result-only unit must not materialize tools")

    monkeypatch.setattr(ToolAssemblyPipeline, "assemble", unexpected_assembly)
    builder = ProfiledGraphBuilder(
        ProfiledGraphBuildConfig(
            label="test",
            graph_name="test-graph",
            domain="groundwater-project-analysis",
            tool_policy="groundwater_default",
            workflow_name="full",
            model_name=None,
            thinking_enabled=False,
            subagent_enabled=False,
            use_search=False,
            kb_ids=[],
            selected_skills=set(),
            skills_provider=ScopedSkillsProvider(),
            task_type=None,
            lightweight_mode=False,
            lightweight_has_kb=False,
            thread_id="thread-1",
            external_thread_id="thread-1",
            max_concurrent_subagents=5,
            checkpointer=None,
            request_metadata={"tool_access": False},
        )
    )

    assert builder._assemble_tools(model_name=None) == []


def test_workflow_registry_does_not_route_legacy_research_actions():
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    request = AgentRequest(
        question="confirm",
        agent_code="ne",
        user_id="user-1",
        metadata={
            "confirmation_status": "confirmed",
            "draft_id": "draft-1",
            "research_plan": {"outline_text": "plan"},
        },
    )

    workflow_name = WorkflowRegistry().resolve_name(request, _context(profile))

    assert workflow_name == "lightweight"


def test_standard_workflow_passes_profile_policy_to_agent():
    profile = AgentProfile(
        code="ne",
        name="NE",
        domain="nuclear-electronics",
        tool_policy="ne_default",
    )
    request = AgentRequest(
        question="hello",
        agent_code="ne",
        user_id="user-1",
        metadata={"draft_id": "draft-1", "file_ids": ["file-1"]},
    )
    context = AgentRuntimeContext(
        request_id="req-1",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="user-1:thread-1",
        agent_profile=profile,
        workflow_name="lightweight",
        model_name=None,
        permissions={"knowledge_base": {"allowed_ids": ["kb-1"]}},
        time_context="<runtime_current_time>2026-06-10</runtime_current_time>",
        request_metadata=dict(request.metadata),
    )

    kwargs = StandardAgentWorkflow(name="full")._build_agent_kwargs(request, context)

    assert kwargs["domain"] == "nuclear-electronics"
    assert kwargs["tool_policy"] == "ne_default"
    assert kwargs["workflow_name"] == "lightweight"
    assert kwargs["thread_id"] == "user-1:thread-1"
    assert "draft_id" not in kwargs
    assert "file_ids" not in kwargs
    assert kwargs["time_context"] == context.time_context
    assert kwargs["runtime_permissions"] == context.permissions
    assert kwargs["tool_runtime_context"].permissions == context.permissions
    assert kwargs["tool_runtime_context"].time_context == context.time_context
    assert kwargs["tool_runtime_context"].request_metadata == request.metadata


async def test_standard_workflow_runs_executor_with_internal_thread_id():
    calls = []

    class FakeExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            calls.append(
                {
                    "question": question,
                    "thread_id": thread_id,
                    "runnable_config": runnable_config,
                }
            )
            yield {"type": "chunk", "content": "ok"}

    class FakeExecutorRegistry:
        def build(self, profile, **kwargs):
            calls.append({"executor_kwargs": kwargs})
            return FakeExecutor()

    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    context = _context(profile)
    request = AgentRequest(question="hello", agent_code="ne", user_id="user-1")
    workflow = StandardAgentWorkflow(name="full", executor_registry=FakeExecutorRegistry())

    events = [event async for event in workflow.run(request, context)]

    assert calls[0]["executor_kwargs"]["thread_id"] == "user-1:thread-1"
    assert calls[1]["thread_id"] == "user-1:thread-1"
    assert events[0].thread_id == "thread-1"
    assert events[1].thread_id == "thread-1"
    assert events[1].payload["content"] == "ok"


def test_standard_workflow_uses_executor_registry_instead_of_full_feature_imports():
    class FakeExecutor:
        pass

    class FakeExecutorRegistry:
        def __init__(self):
            self.profile = None
            self.kwargs = None

        def build(self, profile, **kwargs):
            self.profile = profile
            self.kwargs = kwargs
            return FakeExecutor()

    registry = FakeExecutorRegistry()
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    request = AgentRequest(question="hello", agent_code="ne", user_id="user-1")
    workflow = StandardAgentWorkflow(name="full", executor_registry=registry)

    executor = workflow.build_executor(request, _context(profile))

    source = inspect.getsource(StandardAgentWorkflow)
    assert isinstance(executor, FakeExecutor)
    assert registry.profile is profile
    assert "NEFullFeatureAgent" not in source
    assert "CSHFullFeatureAgent" not in source


async def test_standard_workflow_converts_executor_exception_to_error_event(monkeypatch):
    import backend.application.agent_workflows.standard_agent_workflow as standard_workflow

    monkeypatch.setattr(standard_workflow.logger, "exception", lambda *args, **kwargs: None)

    class BrokenExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            raise RuntimeError("model gateway failed")
            yield  # pragma: no cover

    class BrokenWorkflow(StandardAgentWorkflow):
        def build_executor(self, request, context):
            return BrokenExecutor()

    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    request = AgentRequest(question="hello", agent_code="ne", user_id="user-1")

    events = [event async for event in BrokenWorkflow(name="full").run(request, _context(profile))]

    assert [event.type for event in events] == ["workflow.started", "error", "done"]
    assert events[1].payload == {
        "message": "智能体执行失败，请稍后重试",
        "error": "智能体执行失败，请稍后重试",
        "error_code": "agent_execution_failed",
    }


async def test_standard_workflow_rewrites_executor_event_envelope(monkeypatch):
    import backend.application.agent_workflows.standard_agent_workflow as workflow_module

    monkeypatch.setattr(workflow_module.settings, "follow_up_suggestions_enabled", False)

    class FakeExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            yield AgentEvent(
                type="message.delta",
                request_id="executor-request",
                thread_id="executor-thread",
                sequence=99,
                payload={"content": "answer"},
            )

    class FakeWorkflow(StandardAgentWorkflow):
        def build_executor(self, request, context):
            return FakeExecutor()

    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    context = _context(profile)
    events = [
        event
        async for event in FakeWorkflow(name="full").run(
            AgentRequest(question="hello", agent_code="ne", user_id="user-1"),
            context,
        )
    ]

    assert [event.sequence for event in events] == [0, 1, 2]
    assert all(event.request_id == context.request_id for event in events)
    assert all(event.thread_id == context.external_thread_id for event in events)


async def test_standard_workflow_emits_golden_standard_event_sequence():
    class FakeExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            yield {"type": "status", "status": "running"}
            yield {"type": "thinking", "content": "thinking"}
            yield {"type": "chunk", "content": "answer"}

    class FakeExecutorRegistry:
        def build(self, profile, **kwargs):
            return FakeExecutor()

    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    request = AgentRequest(question="hello", agent_code="ne", user_id="user-1")
    workflow = StandardAgentWorkflow(name="full", executor_registry=FakeExecutorRegistry())

    events = [event async for event in workflow.run(request, _context(profile))]

    assert [event.type for event in events] == [
        "workflow.started",
        "workflow.step",
        "reasoning.delta",
        "message.delta",
        "follow_up.created",
        "done",
    ]
    assert events[1].payload == {"content": "running", "status": "running"}
    assert events[2].payload == {"content": "thinking"}
    assert events[3].payload == {"content": "answer"}
    assert events[4].payload["suggestions"]


async def test_standard_workflow_injects_registered_images_before_done():
    from backend.application.agent_executors.image_url_registry import (
        register_image_url,
    )

    class FakeExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            register_image_url("/files/kb-id/figure-1.jpg", "电荷灵敏前置放大器基本电路")
            yield {"type": "thinking", "content": "[IMAGE:001] is available"}
            yield {
                "type": "chunk",
                "content": "## 1. 电荷灵敏前置放大器基本电路\n\n*图1：电荷灵敏前置放大器基本电路*",
            }

    class FakeExecutorRegistry:
        def build(self, profile, **kwargs):
            return FakeExecutor()

    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    request = AgentRequest(question="hello", agent_code="ne", user_id="user-1")
    workflow = StandardAgentWorkflow(name="full", executor_registry=FakeExecutorRegistry())

    events = [event async for event in workflow.run(request, _context(profile))]

    replace_events = [event for event in events if event.type == "message.replace"]
    assert replace_events
    assert "![图1：电荷灵敏前置放大器基本电路]([IMAGE:001])" in replace_events[-1].payload["content"]
    assert events[-1].type == "done"


async def test_standard_workflow_image_replacement_reaches_sse_as_proxy_url():
    import json

    from backend.application.agent_executors.image_url_registry import (
        register_image_url,
    )
    from backend.application.event_stream.sse_encoder import SSEEncoder

    class FakeExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            register_image_url("/files/kb-id/figure-1.jpg", "电荷灵敏前置放大器基本电路")
            yield {"type": "chunk", "content": "*图1：电荷灵敏前置放大器基本电路*"}

    class FakeExecutorRegistry:
        def build(self, profile, **kwargs):
            return FakeExecutor()

    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    request = AgentRequest(question="hello", agent_code="ne", user_id="user-1")
    workflow = StandardAgentWorkflow(name="full", executor_registry=FakeExecutorRegistry())

    chunks = [
        chunk
        async for chunk in SSEEncoder().encode_stream(
            workflow.run(request, _context(profile))
        )
    ]
    replace_payloads = [
        json.loads(chunk.removeprefix("data: ").strip())
        for chunk in chunks
        if chunk.startswith("data: {")
        and json.loads(chunk.removeprefix("data: ").strip()).get("type") == "message.replace"
    ]

    assert replace_payloads
    content = replace_payloads[-1]["payload"]["content"]
    assert "![图1：电荷灵敏前置放大器基本电路](/api/v2/files/kb-image?" in content
    assert "[IMAGE:" not in content


async def test_standard_workflow_emits_follow_up_suggestions(monkeypatch):
    import backend.application.agent_workflows.standard_agent_workflow as workflow_module

    class FakeFollowUpStream:
        def __init__(self):
            self.visible = ""
            self.emitted = False
            self.cancelled = False
            self.force_calls = []

        def note_visible(self, text):
            self.visible += text

        async def take_ready(self, *, force=False):
            self.force_calls.append(force)
            if self.emitted or not self.visible:
                return None
            self.emitted = True
            return ["继续追问什么？"], {"source": "test"}

        def cancel(self):
            self.cancelled = True

    streams = []

    class FakeFollowUpService:
        def start_stream_task(self, context):
            stream = FakeFollowUpStream()
            streams.append(stream)
            return stream

        def note_process_event(self, event):
            return None

    class FakeExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            yield {"type": "chunk", "content": "answer"}

    class FakeExecutorRegistry:
        def build(self, profile, **kwargs):
            return FakeExecutor()

    monkeypatch.setattr(workflow_module, "FollowUpSuggestionService", FakeFollowUpService)
    monkeypatch.setattr(workflow_module.settings, "follow_up_suggestions_enabled", True)

    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    request = AgentRequest(question="hello", agent_code="ne", user_id="user-1")
    workflow = StandardAgentWorkflow(name="full", executor_registry=FakeExecutorRegistry())

    events = [event async for event in workflow.run(request, _context(profile))]

    assert [event.type for event in events] == [
        "workflow.started",
        "message.delta",
        "follow_up.created",
        "done",
    ]
    assert events[2].payload == {
        "suggestions": ["继续追问什么？"],
        "metadata": {"source": "test"},
    }
    assert streams[0].visible == "answer"
    assert streams[0].cancelled is True
    assert streams[0].force_calls[-1] is True
