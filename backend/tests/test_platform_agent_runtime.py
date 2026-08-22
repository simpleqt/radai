"""Agent 运行时端点、服务与配置契约（拆自 test_agent_platform_standardization.py）。"""

import pytest
import subprocess
import sys

from backend.application.agent_service import AgentService
from backend.application.artifacts.artifact_utils import (
    build_minio_artifact_download_url,
)
from backend.config.settings import Settings
from backend.config.settings_sections import (
    AuthSettings,
    ModelSettings,
    RagSettings,
    ServerSettings,
    StorageSettings,
)
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext
from backend.domain.agents.workflows import MULTI_AGENT_PARALLEL_WORKFLOW
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


def test_settings_are_composed_by_domain_with_flat_access_compatibility():
    app_settings = Settings(redis_host="settings-test-host")

    assert isinstance(app_settings.models, ModelSettings)
    assert isinstance(app_settings.rag, RagSettings)
    assert isinstance(app_settings.storage, StorageSettings)
    assert isinstance(app_settings.auth, AuthSettings)
    assert isinstance(app_settings.server, ServerSettings)
    assert app_settings.storage.redis_host == "settings-test-host"
    assert app_settings.redis_host == "settings-test-host"

    app_settings.redis_host = "settings-updated-host"

    assert app_settings.storage.redis_host == "settings-updated-host"


def test_settings_fields_have_single_domain_owner():
    section_types = (
        ModelSettings,
        RagSettings,
        StorageSettings,
        AuthSettings,
        ServerSettings,
    )
    owners: dict[str, str] = {}

    for section_type in section_types:
        for field_name in section_type.model_fields:
            assert field_name not in owners, (
                f"{field_name} is owned by both {owners[field_name]} "
                f"and {section_type.__name__}"
            )
            owners[field_name] = section_type.__name__

    assert owners["vlm_api_key"] == "ModelSettings"
    assert owners["redis_host"] == "StorageSettings"
    assert owners["cas_enabled"] == "AuthSettings"


def test_agent_service_enables_subagent_for_coordinated_workflow():
    service = AgentService()
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    request = AgentRequest(
        question="coordinate",
        agent_code="ne",
        user_id="user-1",
        requested_workflow=MULTI_AGENT_PARALLEL_WORKFLOW,
    )

    context = service._build_context(
        request,
        profile=profile,
        workflow_name=MULTI_AGENT_PARALLEL_WORKFLOW,
        request_id="req-1",
        external_thread_id="thread-1",
    )

    assert context.subagent_enabled is True


async def test_agent_service_persists_runtime_conversation_before_streaming(monkeypatch):
    calls = []
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    context = _context(profile)

    class FakeConversationStore:
        async def ensure_runtime_conversation(self, **kwargs):
            calls.append(("persist", kwargs))

    class FakeWorkflow:
        async def run(self, request, runtime_context):
            calls.append(("run", runtime_context.external_thread_id))
            yield AgentEvent(
                type="message.delta",
                request_id=runtime_context.request_id,
                thread_id=runtime_context.external_thread_id,
                sequence=1,
                payload={"content": "ok"},
            )

    service = AgentService(conversation_store=FakeConversationStore())
    monkeypatch.setattr(service, "_resolve_workflow", lambda request: (FakeWorkflow(), context))

    events = [
        event
        async for event in service.run_stream(
            AgentRequest(
                question="hello",
                agent_code="ne",
                user_id="user-1",
                conversation_id="thread-1",
                metadata={"source": "test"},
            )
        )
    ]

    assert events[0].payload["content"] == "ok"
    assert calls[0][0] == "persist"
    assert calls[0][1]["thread_id"] == "thread-1"
    assert calls[0][1]["agent_code"] == "ne"
    assert calls[0][1]["workflow_name"] == "lightweight"
    assert calls[1] == ("run", "thread-1")


async def test_agent_service_fails_closed_when_conversation_persist_fails(monkeypatch):
    import backend.application.agent_service as agent_service_module

    monkeypatch.setattr(agent_service_module.logger, "exception", lambda *args, **kwargs: None)
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    context = _context(profile)

    class BrokenConversationStore:
        async def ensure_runtime_conversation(self, **kwargs):
            raise RuntimeError("db unavailable")

    class FakeWorkflow:
        async def run(self, request, runtime_context):
            raise AssertionError("workflow must not run without conversation persistence")
            yield

    service = AgentService(conversation_store=BrokenConversationStore())
    monkeypatch.setattr(service, "_resolve_workflow", lambda request: (FakeWorkflow(), context))

    events = [
        event
        async for event in service.run_stream(
            AgentRequest(
                question="hello",
                agent_code="ne",
                user_id="user-1",
                conversation_id="thread-1",
            )
        )
    ]

    assert [event.type for event in events] == ["error", "done"]
    assert events[0].payload["error_code"] == "conversation_persistence_unavailable"


def test_minio_artifact_fallback_url_targets_proxy_download():
    url = build_minio_artifact_download_url(
        minio_object="users/anonymous/threads/t1/html-ppt/a.html",
        filename="html-ppt/a.html",
        mime_type="text/html",
    )

    assert url.startswith("/api/v2/files/minio-artifact/download?")
    assert "object_name=users%2Fanonymous%2Fthreads%2Ft1%2Fhtml-ppt%2Fa.html" in url
    # The response type comes from the stored object, so a caller-controlled
    # content type must never ride along in the download URL.
    assert "mime_type" not in url


def test_files_router_exposes_minio_artifact_download_before_file_id_route():
    from backend.api.routers.files import router

    paths = [route.path for route in router.routes]

    assert "/files/minio-artifact/download" in paths
    assert paths.index("/files/minio-artifact/download") < paths.index("/files/{file_id}/download")


async def test_run_blocking_timeout_does_not_wait_for_worker():
    import asyncio
    import threading

    from backend.application.blocking import run_blocking

    release = threading.Event()
    started = threading.Event()

    def blocked_call():
        started.set()
        release.wait()

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    try:
        with pytest.raises(TimeoutError):
            await run_blocking(blocked_call, timeout_seconds=0.02)
        assert loop.time() - started_at < 0.5
        assert started.wait(timeout=0.5)
    finally:
        release.set()


async def test_agent_runtime_query_endpoint_streams_standard_sse_smoke(monkeypatch):
    import backend.api.routers.agent_runtime as agent_runtime
    import backend.api.routers.agent_runtime_tasks as agent_runtime_tasks
    import backend.application.checkpointing.checkpointer as checkpointer_module
    from backend.api.dependencies.auth import ApiKeyAuth

    class FakeAgentService:
        def __init__(self, checkpointer=None):
            self.checkpointer = checkpointer

        async def resolve_request_skills(self, request):
            return request

        async def run_stream(self, request):
            yield AgentEvent(
                type="message.delta",
                request_id="req-smoke",
                thread_id=request.conversation_id or "thread-smoke",
                sequence=1,
                payload={"content": "ok"},
                created_at="2026-06-08T00:00:00+00:00",
            )
            yield AgentEvent(
                type="done",
                request_id="req-smoke",
                thread_id=request.conversation_id or "thread-smoke",
                sequence=2,
                payload={},
                created_at="2026-06-08T00:00:01+00:00",
            )

    monkeypatch.setattr(agent_runtime_tasks, "AgentService", FakeAgentService)
    monkeypatch.setattr(checkpointer_module, "get_checkpointer_from_request", lambda request: None)

    response = await agent_runtime.query_agent_runtime(
        agent_runtime.AgentRuntimeQueryRequest(
            question="hello",
            agent_code="ne",
            thread_id="thread-smoke",
            workflow="lightweight",
            thinking=False,
        ),
        request=SimpleNamespace(),
        auth=ApiKeyAuth(api_key="test", user_id="user-smoke"),
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert response.media_type == "text/event-stream"
    assert 'data: {"type": "message.delta", "payload": {"content": "ok"}' in body
    assert '"thread_id": "thread-smoke"' in body
    assert body.endswith("data: [DONE]\n\n")


async def test_agent_runtime_query_strips_reserved_client_metadata(monkeypatch):
    import backend.api.routers.agent_runtime as agent_runtime
    import backend.api.routers.agent_runtime_tasks as agent_runtime_tasks
    import backend.application.checkpointing.checkpointer as checkpointer_module
    from backend.api.dependencies.auth import ApiKeyAuth

    captured = {}

    class FakeAgentService:
        def __init__(self, checkpointer=None):
            pass

        async def resolve_request_skills(self, request):
            captured["request"] = request
            return request

        async def run_stream(self, request):
            yield AgentEvent(
                type="done",
                request_id="req-security",
                thread_id="thread-security",
                sequence=1,
            )

    monkeypatch.setattr(agent_runtime_tasks, "AgentService", FakeAgentService)
    monkeypatch.setattr(checkpointer_module, "get_checkpointer_from_request", lambda request: None)

    response = await agent_runtime.query_agent_runtime(
        agent_runtime.AgentRuntimeQueryRequest(
            question="hello",
            agent_code="ne",
            workflow="lightweight",
            metadata={
                "source": "client",
                "_skills_resolved": True,
                "_managed_skills_resolved": True,
                "_allow_skill_fallback": True,
            },
        ),
        request=SimpleNamespace(),
        auth=ApiKeyAuth(api_key="test", user_id="user-security", role="user"),
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks[-1] == "data: [DONE]\n\n"
    request = captured["request"]
    assert request.metadata == {"source": "client"}
    assert request.auth_role == "user"
    assert request.skills_resolved is False
    assert request.managed_skills_resolved is False


async def test_agent_runtime_query_rejects_unknown_profile_before_streaming():
    import backend.api.routers.agent_runtime as agent_runtime
    from backend.api.dependencies.auth import ApiKeyAuth

    with pytest.raises(Exception) as exc_info:
        await agent_runtime.query_agent_runtime(
            agent_runtime.AgentRuntimeQueryRequest(
                question="hello",
                agent_code="does-not-exist",
            ),
            request=SimpleNamespace(),
            auth=ApiKeyAuth(api_key="test", user_id="user-security"),
        )

    assert getattr(exc_info.value, "status_code", None) == 404


def test_agent_runtime_info_endpoint_exists_for_frontend_compat():
    from backend.api.routers.agent_runtime import router

    paths = {route.path for route in router.routes}

    assert "/agent-runtime/profiles/{agent_code}/info" in paths


def test_agent_runtime_info_endpoint_uses_application_service():
    import backend.api.routers.agent_runtime as agent_runtime
    from backend.application.agents.info_service import AgentInfoService

    assert agent_runtime.AgentInfoService is AgentInfoService


def test_agent_runtime_task_endpoints_exist_for_frontend_compat():
    from backend.api.routers.agent_runtime import router

    paths = {route.path for route in router.routes}

    assert "/agent-runtime/query" in paths
    assert "/agent-runtime/profiles" in paths
    assert "/agent-runtime/profiles/{agent_code}/info" in paths
    assert "/agent-runtime/tools" in paths
    assert "/agent-runtime/admin/tool-runtime" in paths
    assert "/agent-runtime/skill-manifests" in paths
    assert "/agent-runtime/runs/{run_id}" in paths
    assert "/agent-runtime/runs/{run_id}/events" in paths
    assert "/agent-runtime/runs/{run_id}/units" in paths
    assert "/agent-runtime/runs/{run_id}/tool-calls" in paths
    assert "/agent-runtime/runs/{run_id}/context-packs" in paths
    assert "/agent-runtime/runs/{run_id}/handoffs" in paths
    assert "/agent-runtime/runs/{run_id}/replay" in paths
    assert "/agent-runtime/{agent_code}/task/{task_id}/status" in paths
    assert "/agent-runtime/{agent_code}/task/{task_id}/stream" in paths
    assert "/agent-runtime/{agent_code}/task/{task_id}/cancel" in paths
    assert "/agent-runtime/{agent_code}/thread/{thread_id}/task" in paths


def test_agent_runtime_task_endpoints_no_longer_depend_on_legacy_service():
    import backend.api.routers.agent_runtime as agent_runtime

    assert not hasattr(agent_runtime, "_load_legacy_task_api")
    assert not hasattr(agent_runtime, "ResearchTaskService")


async def test_agent_runtime_catalog_endpoints_return_platform_config():
    import backend.api.routers.agent_runtime as agent_runtime
    from backend.api.dependencies.auth import ApiKeyAuth

    auth = ApiKeyAuth(api_key="test", user_id="user-1")

    profiles = await agent_runtime.list_agent_profiles(auth=auth)
    tools = await agent_runtime.list_tools(auth=auth)
    skills = await agent_runtime.list_skill_manifests(auth=auth)
    policy_tools = await agent_runtime.list_policy_tools(
        policy_name="ne_default",
        agent_code="ne",
        workflow="full",
        selected_skills="html-ppt,general-table-analysis",
        auth=auth,
    )

    profile_codes = {profile["code"] for profile in profiles["profiles"]}
    tool_names = {tool["name"] for tool in tools["tools"]}
    skill_names = {skill["name"] for skill in skills["skills"]}
    policy_tool_names = {tool["name"] for tool in policy_tools["tools"]}

    assert {"ne", "csh"} <= profile_codes
    assert {"multi_kb_search", "generate_html_ppt"} <= tool_names
    assert {"nuclear-electronics", "general-table-analysis"} <= skill_names
    assert "html-ppt" in skill_names
    assert {"multi_kb_search", "generate_html_ppt", "inspect_excel_data"} <= policy_tool_names
    assert policy_tools["selected_skills"] == ["general-table-analysis", "html-ppt"]


async def test_agent_runtime_skill_manifests_preserves_runtime_auth_errors(monkeypatch):
    from fastapi import HTTPException

    import backend.api.routers.agent_runtime as agent_runtime
    import backend.db.connection as db_connection
    from backend.api.dependencies.auth import ApiKeyAuth
    from backend.application.skills.runtime_resolver import (
        SkillRuntimeError,
        SkillRuntimeResolver,
    )

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    def fake_session_factory():
        return FakeSessionContext()

    async def deny_access(self, session, **kwargs):
        raise SkillRuntimeError(
            "Skill 无权使用",
            code="SKILL_ACCESS_DENIED",
            status_code=403,
        )

    monkeypatch.setattr(
        db_connection,
        "get_async_session_factory",
        lambda: fake_session_factory,
    )
    monkeypatch.setattr(SkillRuntimeResolver, "resolve", deny_access)

    with pytest.raises(HTTPException) as exc_info:
        await agent_runtime.list_skill_manifests(
            auth=ApiKeyAuth(api_key="test", user_id="user-1")
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {
        "code": "SKILL_ACCESS_DENIED",
        "message": "Skill 无权使用",
    }


async def test_agent_runtime_managed_skill_catalog_does_not_append_unbound_public_skills(
    monkeypatch,
):
    import backend.api.routers.agent_runtime as agent_runtime
    import backend.db.connection as db_connection
    from backend.api.dependencies.auth import ApiKeyAuth
    from backend.application.skills.runtime_resolver import SkillRuntimeResolver

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def resolve(self, session, **kwargs):
        option = SimpleNamespace(
            skill_id="skill-1",
            name="nuclear-electronics",
            description="bound",
            version="1.0.0",
            required=False,
            default_selected=True,
            selectable=True,
            allowed_workflows=["lightweight", "full"],
            preferred_workflow="full",
            guest_selectable=False,
        )
        return SimpleNamespace(
            managed=True,
            selected_skills=[option.name],
            available_skills=[option],
        )

    monkeypatch.setattr(
        db_connection,
        "get_async_session_factory",
        lambda: lambda: FakeSessionContext(),
    )
    monkeypatch.setattr(SkillRuntimeResolver, "resolve", resolve)

    payload = await agent_runtime.list_skill_manifests(
        auth=ApiKeyAuth(api_key="test", user_id="user-1")
    )

    assert [item["name"] for item in payload["skills"]] == ["nuclear-electronics"]


def test_v2_api_does_not_register_legacy_agent_routes():
    from backend.api.routers.agent_runtime_v2 import api_router

    paths = {route.path for route in api_router.routes}

    assert not any(path == "/api/v2/agent" or path.startswith("/api/v2/agent/") for path in paths)
    assert not any(path == "/api/v2/csh_agent" or path.startswith("/api/v2/csh_agent/") for path in paths)
    assert any(path.startswith("/api/v2/agent-runtime") for path in paths)


def test_v2_api_only_registers_standard_runtime_guest_and_explore_namespaces():
    from backend.api.routers.agent_runtime_v2 import api_router

    paths = {route.path for route in api_router.routes}
    roots = {
        path.split("/", 4)[3]
        for path in paths
        if path.startswith("/api/v2/") and len(path.split("/")) > 3
    }

    assert roots == {"agent-runtime", "guest", "explore"}


def test_api_package_init_does_not_trigger_http_server_before_v2_router_import():
    script = """
import sys
import backend.api.routers.auth
if 'backend.api.http_server' in sys.modules:
    raise SystemExit('http_server imported by child router import')
from backend.api.routers.agent_runtime_v2 import api_router
paths = {route.path for route in api_router.routes}
if not any(path.startswith('/api/v2/agent-runtime') for path in paths):
    raise SystemExit('agent-runtime routes missing')
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
