from types import SimpleNamespace

import pytest

from backend.application.agent_service import AgentService
from backend.application.skills.domain_policy import skill_domain_matches
from backend.application.skills.runtime_resolver import RuntimeSkillOption
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest


def test_skill_domain_policy_supports_explicit_and_cross_agent_domains():
    assert skill_domain_matches(
        "nuclear-electronics",
        agent_code="ne",
        agent_domain="nuclear-electronics",
    )
    assert skill_domain_matches(
        "ne",
        agent_code="ne",
        agent_domain="nuclear-electronics",
    )
    assert skill_domain_matches(
        "general",
        agent_code="ne",
        agent_domain="nuclear-electronics",
    )
    assert skill_domain_matches(
        "*",
        agent_code="ne",
        agent_domain="nuclear-electronics",
    )
    assert skill_domain_matches(
        "deep-research",
        agent_code="ne",
        agent_domain="nuclear-electronics",
    )
    assert not skill_domain_matches(
        "groundwater",
        agent_code="ne",
        agent_domain="nuclear-electronics",
    )


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_only_required_bindings_are_tracked_as_must_load(monkeypatch):
    profile = AgentProfile(
        code="test-agent",
        name="Test",
        domain="general",
        default_skills=["optional-default"],
        default_workflow="full",
        allowed_workflows=["full"],
    )
    loader = SimpleNamespace(load=lambda _code: profile)
    options = [
        RuntimeSkillOption(
            skill_id="required-id",
            name="required-skill",
            label="Required",
            description="Required",
            version="1.0.0",
            required=True,
            default_selected=True,
            selectable=False,
            allowed_workflows=["full"],
            preferred_workflow="full",
            guest_selectable=False,
        ),
        RuntimeSkillOption(
            skill_id="optional-id",
            name="optional-default",
            label="Optional",
            description="Optional",
            version="1.0.0",
            required=False,
            default_selected=True,
            selectable=True,
            allowed_workflows=["full"],
            preferred_workflow="full",
            guest_selectable=False,
        ),
    ]

    async def resolve(*_args, **_kwargs):
        return SimpleNamespace(
            managed=True,
            selected_skills=["required-skill", "optional-default"],
            available_skills=options,
        )

    monkeypatch.setattr(
        "backend.application.skills.runtime_resolver.SkillRuntimeResolver.resolve",
        resolve,
    )
    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: _SessionContext,
    )

    resolved = await AgentService(profile_loader=loader).resolve_request_skills(
        AgentRequest(
            question="unrelated question",
            agent_code=profile.code,
            user_id="user-1",
            requested_workflow="full",
            selected_skills=None,
            auth_role="user",
        )
    )

    assert resolved.selected_skills == ["required-skill", "optional-default"]
    assert resolved.metadata["_required_skills"] == ["required-skill"]
