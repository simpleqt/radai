"""Focused contracts for profile-driven frontend and guest behavior."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.dependencies.auth import ApiKeyAuth
from backend.api.routers import guest_agent
from backend.application.agents.info_service import AgentInfoService
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.skills.runtime_resolver import RuntimeSkillOption
from backend.db.crud.agent_crud import get_runtime_agent_kb_ids
from backend.db.models.agent import Agent, AgentKnowledgeBase
from backend.domain.agents.profile import AgentProfile

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _AsyncSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_agent_info_disabled_kb_policy_never_exposes_bindings(monkeypatch):
    profile = AgentProfile(
        code="no-kb",
        name="No KB",
        domain="general",
        kb_policy="disabled",
    )
    service = AgentInfoService()
    binding_lookup = AsyncMock(return_value=["must-not-be-visible"])
    monkeypatch.setattr(service, "_get_agent_default_kb_ids", binding_lookup)
    monkeypatch.setattr(
        service, "_filter_accessible_kb_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(service, "_fetch_kb_options", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_supports_tool", lambda *_args: False)
    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("database disabled in unit test")),
    )

    info = await service.get_info(profile)

    binding_lookup.assert_not_awaited()
    assert info["kb_ids"] == []
    assert info["available_kbs"] == []


def _disable_info_service_kb_queries(service: AgentInfoService, monkeypatch) -> None:
    monkeypatch.setattr(
        service, "_get_agent_default_kb_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        service, "_filter_accessible_kb_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(service, "_fetch_kb_options", AsyncMock(return_value=[]))


@pytest.mark.asyncio
async def test_runtime_kb_bindings_do_not_depend_on_display_record_active_flag():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Agent.__table__.create)
        await connection.run_sync(AgentKnowledgeBase.__table__.create)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            Agent(
                agent_code="fmca",
                agent_name="FMCA",
                is_active=False,
            )
        )
        session.add(
            AgentKnowledgeBase(
                agent_code="fmca",
                kb_id="kb-fmca",
                sort_order=0,
            )
        )
        await session.commit()

        assert await get_runtime_agent_kb_ids(session, "fmca") == ["kb-fmca"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_info_uses_policy_filtered_public_catalog_when_unmanaged(
    monkeypatch,
):
    profile = AgentProfileLoader().load("ne")
    service = AgentInfoService()
    _disable_info_service_kb_queries(service, monkeypatch)

    async def resolve_unmanaged(*_args, **_kwargs):
        return SimpleNamespace(
            managed=False,
            selected_skills=list(profile.default_skills),
            available_skills=[],
        )

    monkeypatch.setattr(
        "backend.application.skills.runtime_resolver.SkillRuntimeResolver.resolve",
        resolve_unmanaged,
    )
    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: _AsyncSessionContext,
    )

    info = await service.get_info(profile)
    options = {str(item["name"]): item for item in info["available_skills"]}

    assert {"nuclear-electronics", "general-table-analysis", "html-ppt"} <= set(
        options
    )
    assert options["nuclear-electronics"]["required"] is True
    assert options["nuclear-electronics"]["selectable"] is False
    assert "nuclear-electronics" in info["required_skills"]
    assert options["deep-research"]["preferred_workflow"] == "plan_solve"
    assert options["deep-research"]["allowed_workflows"] == [
        "deep_research",
        "plan_solve",
    ]
    assert options["deep-research"]["guest_selectable"] is False


@pytest.mark.asyncio
async def test_agent_info_managed_empty_catalog_is_authoritative(monkeypatch):
    profile = AgentProfileLoader().load("ne")
    service = AgentInfoService()
    _disable_info_service_kb_queries(service, monkeypatch)

    async def resolve_managed(*_args, **_kwargs):
        return SimpleNamespace(
            managed=True,
            selected_skills=[],
            available_skills=[],
        )

    monkeypatch.setattr(
        "backend.application.skills.runtime_resolver.SkillRuntimeResolver.resolve",
        resolve_managed,
    )
    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: _AsyncSessionContext,
    )

    info = await service.get_info(profile)

    assert info["available_skills"] == []
    assert info["selected_skills"] == []
    assert info["required_skills"] == []


@pytest.mark.asyncio
async def test_agent_info_catalog_failure_is_explicit_and_keeps_builtin_skills(
    monkeypatch,
):
    profile = AgentProfileLoader().load("ne")
    service = AgentInfoService()
    _disable_info_service_kb_queries(service, monkeypatch)

    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("catalog unavailable")),
    )

    info = await service.get_info(profile)

    assert info["skill_catalog_status"] == "degraded"
    assert info["skill_catalog_error"]["code"] == "SKILL_CATALOG_UNAVAILABLE"
    assert info["available_skills"]
    assert set(profile.default_skills) <= set(info["selected_skills"])


@pytest.mark.asyncio
async def test_agent_info_exposes_managed_skill_workflow_and_guest_capabilities(
    monkeypatch,
):
    profile = AgentProfileLoader().load("ne")
    service = AgentInfoService()
    _disable_info_service_kb_queries(service, monkeypatch)
    option = RuntimeSkillOption(
        skill_id="managed-research-id",
        name="managed-research",
        label="Managed Research",
        description="Managed workflow-aware Skill",
        version="1.0.0",
        required=False,
        default_selected=False,
        selectable=True,
        allowed_workflows=["plan_solve"],
        preferred_workflow="plan_solve",
        guest_selectable=False,
    )

    async def resolve_managed(*_args, **_kwargs):
        return SimpleNamespace(
            managed=True,
            selected_skills=[],
            available_skills=[option],
        )

    monkeypatch.setattr(
        "backend.application.skills.runtime_resolver.SkillRuntimeResolver.resolve",
        resolve_managed,
    )
    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: _AsyncSessionContext,
    )

    info = await service.get_info(profile)

    assert info["available_skills"] == [
        {
            "id": option.skill_id,
            "name": option.name,
            "label": option.label,
            "description": option.description,
            "version": option.version,
            "required": False,
            "default_selected": False,
            "selectable": True,
            "allowed_workflows": ["plan_solve"],
            "preferred_workflow": "plan_solve",
            "guest_selectable": False,
        }
    ]


@pytest.mark.asyncio
async def test_agent_info_exposes_workflow_capabilities(monkeypatch):
    profile = AgentProfile(
        code="fourth-domain",
        name="Fourth Domain",
        domain="fourth-domain",
        default_workflow="full",
        allowed_workflows=["full", "deep_research"],
    )
    service = AgentInfoService()
    monkeypatch.setattr(
        service, "_get_agent_default_kb_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        service, "_filter_accessible_kb_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(service, "_fetch_kb_options", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_supports_tool", lambda *_args: False)

    # Skill bindings are optional metadata for this response contract. Avoid a
    # deployment database connection in the focused unit test.
    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("database disabled in unit test")),
    )

    info = await service.get_info(profile)

    assert info["agent_code"] == "fourth-domain"
    assert info["default_workflow"] == "full"
    assert info["allowed_workflows"] == ["full", "deep_research"]
    assert info["task_type"] == "full"


def test_agent_info_tool_capability_requires_effective_tool_policy(monkeypatch):
    calls: list[dict[str, object]] = []

    class FakeToolRegistry:
        def resolve_policy_tools(self, **kwargs):
            calls.append(kwargs)
            if kwargs["policy_name"] == "allows_search":
                return [SimpleNamespace(name="web_search")]
            return []

    # _supports_tool 已接线到进程级共享注册表（消除每请求 YAML 重解析），
    # 打桩点随之改为共享访问器；断言意图不变（策略解析必须被咨询）。
    monkeypatch.setattr(
        "backend.application.tools.assembler.shared_default_tool_registry",
        lambda: FakeToolRegistry(),
    )
    profile = AgentProfile(
        code="fourth-domain",
        name="Fourth Domain",
        domain="fourth-domain",
        default_skills=["fourth-domain"],
        default_workflow="full",
        allowed_workflows=["full"],
        tool_policy="denies_search",
    )

    assert AgentInfoService._supports_tool(profile, "web_search") is False
    allowed_profile = replace(profile, tool_policy="allows_search")
    assert AgentInfoService._supports_tool(allowed_profile, "web_search") is True
    assert calls[-1]["selected_skills"] == {"fourth-domain"}


@pytest.mark.asyncio
async def test_agent_info_endpoint_uses_resolved_runtime_definition(monkeypatch):
    from backend.api.routers import agent_runtime
    from backend.api.routers import agent_runtime_catalog

    profile = AgentProfile(
        code="fourth-domain",
        name="Fourth Domain",
        domain="fourth-domain",
        default_workflow="full",
        allowed_workflows=["full"],
    )
    resolve = Mock(return_value=SimpleNamespace(profile=profile))
    monkeypatch.setattr(
        agent_runtime_catalog,
        "AgentDefinitionValidator",
        lambda: SimpleNamespace(resolve=resolve),
    )
    get_info = AsyncMock(return_value={"agent_code": profile.code})
    monkeypatch.setattr(agent_runtime.AgentInfoService, "get_info", get_info)

    result = await agent_runtime.get_agent_runtime_info(
        profile.code,
        auth=ApiKeyAuth(api_key="", user_id="guest", role="user"),
    )

    assert result == {"agent_code": profile.code}
    resolve.assert_called_once_with(profile.code)
    assert get_info.await_args.args[0] is profile


@pytest.mark.parametrize("frontend_root", ["frontend", "frontend_csh_ai"])
def test_frontend_capabilities_have_no_local_skill_or_kb_fallback(frontend_root):
    source_root = PROJECT_ROOT / frontend_root / "src" / "views" / "ne-full"
    chat_box = (source_root / "components" / "ChatBox.vue").read_text(
        encoding="utf-8"
    )
    chat_runtime = (source_root / "composables" / "useAgentChat.ts").read_text(
        encoding="utf-8"
    )
    api_source = (PROJECT_ROOT / frontend_root / "src" / "api" / "agent-v2.ts").read_text(
        encoding="utf-8"
    )

    assert "fallbackSkills" not in chat_box
    assert "c056bf59-6091-45ed-98cb-e2e860580a69" not in chat_box
    assert "agentInfo?.search_enabled === true" in chat_box
    assert "deepResearchAvailable.value" in chat_runtime
    assert "searchAvailable.value" in chat_runtime
    assert "agentInfo.value?.selected_skills" in chat_runtime
    assert "skill.required || skill.default_selected" in chat_runtime
    assert "nuclear-electronics" not in chat_runtime
    assert "csh-hydrogeology" not in chat_runtime
    assert "csh-ai" not in chat_runtime
    assert "allowed.length === 0" not in api_source
    assert "preferred_workflow?: string | null" in api_source
    assert "guest_selectable?: boolean" in api_source
    assert "profileSelectedSkills" in api_source
    assert "userSelectedSkills" in api_source
    assert "skillAllowed.includes(preferred)" in api_source
    assert "options.useSearch && allowed.includes('full')" in api_source
    assert "options.useSearch || hasUserSelectedCapability" not in api_source
    assert "skill.id === 'deep-research'" not in chat_box
    assert "skill.guest_selectable === true" in chat_box
    assert "isDeepResearchSkill" not in chat_runtime


def test_guest_workflow_is_profile_driven_for_any_registered_agent(monkeypatch):
    profile = AgentProfile(
        code="fourth-domain",
        name="Fourth Domain",
        domain="fourth-domain",
        default_workflow="full",
        allowed_workflows=["full"],
    )
    monkeypatch.setattr(guest_agent.AgentProfileLoader, "load", lambda *_args: profile)

    assert guest_agent._guest_workflow_name("fourth-domain") == "full"


@pytest.mark.asyncio
async def test_guest_unknown_profile_fails_before_streaming(monkeypatch):
    # 白名单先于 profile 解析；本测试意图是「未知 profile → 404」，
    # 把测试 code 放进白名单以到达 profile 加载路径。
    monkeypatch.setenv("GUEST_ALLOWED_AGENT_CODES", "missing-profile")
    request = Request({"type": "http", "headers": []})

    with pytest.raises(HTTPException) as exc_info:
        await guest_agent.query_guest(
            guest_agent.GuestQueryRequest(
                question="test",
                agent_code="missing-profile",
            ),
            request,
            ApiKeyAuth(api_key="", user_id="guest", role="user"),
            session=object(),
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_guest_rejects_profile_with_subagent_default(monkeypatch):
    profile = AgentProfile(
        code="research-only",
        name="Research Only",
        domain="research-only",
        default_workflow="deep_research",
        allowed_workflows=["deep_research"],
    )
    monkeypatch.setattr(guest_agent.AgentProfileLoader, "load", lambda *_args: profile)
    monkeypatch.setenv("GUEST_ALLOWED_AGENT_CODES", "research-only")
    request = Request({"type": "http", "headers": []})

    with pytest.raises(HTTPException) as exc_info:
        await guest_agent.query_guest(
            guest_agent.GuestQueryRequest(
                question="test",
                agent_code="research-only",
            ),
            request,
            ApiKeyAuth(api_key="", user_id="guest", role="user"),
            session=object(),
        )

    assert exc_info.value.status_code == 400
