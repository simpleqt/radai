from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backend.application.agent_executors.profiled_executor import ProfiledAgentExecutor
from backend.application.agent_service import AgentService
from backend.application.agent_workflows.standard_agent_workflow import (
    StandardAgentWorkflow,
)
from backend.application.agents.definition import AgentDefinitionValidator
from backend.application.agents.prompt_registry import PromptProfileSpec
from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_bindings import AgentRuntimeBindings
from backend.domain.agents.runtime_context import AgentRuntimeContext
from backend.domain.agents.workflows import WORKFLOW_SPECS
from backend.domain.tools.policy import ToolPolicy


def _contract_prompt(
    *,
    subagent_enabled,
    use_search,
    available_skills,
    task_type,
    lightweight_mode,
    lightweight_has_kb,
):
    return "contract prompt"


def _runtime_bindings(*, agent_code: str = "materials") -> AgentRuntimeBindings:
    return AgentRuntimeBindings(
        agent_code=agent_code,
        domain="materials-science",
        prompt_profile="materials_prompt",
        tool_policy="materials_default",
        guardrail_policy="default",
        default_workflow="full",
        workflow_names=("full",),
        tool_names=("multi_kb_search", "web_search"),
        skill_names=("materials",),
        domain_context_provider="example.module:build_context",
    )


def test_service_projects_resolved_definition_to_names_only_bindings():
    profile = AgentProfile(
        code="materials",
        name="Materials",
        domain="materials-science",
        default_workflow="full",
        allowed_workflows=["full"],
        prompt_profile="profile_value_is_not_canonical",
        tool_policy="profile_value_is_not_canonical",
        domain_context_provider="example.module:build_context",
    )
    definition = SimpleNamespace(
        profile=profile,
        prompt_spec=SimpleNamespace(name="materials_prompt"),
        prompt_provider=lambda **_: "must not enter runtime context",
        tool_policy=SimpleNamespace(name="materials_default"),
        workflows=(SimpleNamespace(name="full"),),
        workflow_providers=(("full", lambda **_: object()),),
        tools=(SimpleNamespace(name="multi_kb_search"),),
        skills=(SimpleNamespace(name="materials", dependencies=[]),),
        domain_context_provider=lambda **_: {},
    )
    validator = SimpleNamespace(resolve=lambda _code: definition)

    service = AgentService(definition_validator=validator)
    resolved_profile, bindings = service._load_profile_and_bindings("materials")

    assert resolved_profile is profile
    assert bindings == AgentRuntimeBindings(
        agent_code="materials",
        domain="materials-science",
        prompt_profile="materials_prompt",
        tool_policy="materials_default",
        guardrail_policy="default",
        default_workflow="full",
        workflow_names=("full",),
        tool_names=("multi_kb_search",),
        skill_names=("materials",),
        skill_dependencies=(("materials", ()),),
        domain_context_provider="example.module:build_context",
    )
    # The projection is safe for JSON-backed request/checkpoint boundaries and
    # cannot accidentally retain any provider callable from the definition.
    assert json.loads(json.dumps(asdict(bindings)))["prompt_profile"] == (
        "materials_prompt"
    )
    assert not any(callable(value) for value in asdict(bindings).values())


def test_service_keeps_custom_profile_loader_compatibility_without_bindings():
    profile = AgentProfile(
        code="extension-agent",
        name="Extension",
        domain="extension-domain",
        prompt_profile="extension-prompt",
        tool_policy="extension-policy",
    )
    loader = SimpleNamespace(load=lambda _code: profile)

    service = AgentService(profile_loader=loader)
    loaded_profile, bindings = service._load_profile_and_bindings("extension-agent")

    assert loaded_profile is profile
    assert bindings is None


def test_standard_workflow_uses_canonical_runtime_bindings():
    profile = AgentProfile(
        code="materials",
        name="Materials",
        domain="materials-science",
        prompt_profile="stale_prompt",
        tool_policy="stale_policy",
    )
    request = AgentRequest(
        question="hello",
        agent_code="materials",
        user_id="user-1",
    )
    bindings = _runtime_bindings()
    context = AgentRuntimeContext(
        request_id="request-1",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="user-1:thread-1",
        agent_profile=profile,
        workflow_name="full",
        model_name="test-model",
        agent_bindings=bindings,
    )

    kwargs = StandardAgentWorkflow(name="full")._build_agent_kwargs(request, context)

    assert kwargs["agent_bindings"] is bindings
    assert kwargs["prompt_profile"] == "materials_prompt"
    assert kwargs["tool_policy"] == "materials_default"
    assert kwargs["request_metadata"]["_tool_policy"] == "materials_default"


def test_standard_workflow_rejects_binding_for_another_agent():
    profile = AgentProfile(
        code="materials", name="Materials", domain="materials-science"
    )
    context = AgentRuntimeContext(
        request_id="request-1",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="user-1:thread-1",
        agent_profile=profile,
        workflow_name="full",
        model_name="test-model",
        agent_bindings=_runtime_bindings(agent_code="another-agent"),
    )
    request = AgentRequest(question="hello", agent_code="materials", user_id="user-1")

    with pytest.raises(ValueError, match="agent_code"):
        StandardAgentWorkflow(name="full")._build_agent_kwargs(request, context)


def test_profiled_executor_prefers_canonical_bindings(monkeypatch):
    captured = {}

    class FakeGraphBuilder:
        def __init__(self, config):
            captured["config"] = config

        def build(self):
            return SimpleNamespace(
                graph=object(),
                model_name="test-model",
                tools=[],
                compaction_checkpoints=None,
            )

    monkeypatch.setattr(
        "backend.application.agent_executors.profiled_executor.ProfiledGraphBuilder",
        FakeGraphBuilder,
    )
    profile = AgentProfile(
        code="materials",
        name="Materials",
        domain="materials-science",
        prompt_profile="stale_prompt",
        tool_policy="stale_policy",
    )

    executor = ProfiledAgentExecutor(
        profile=profile,
        agent_bindings=_runtime_bindings(),
        domain="spoofed-domain",
        prompt_profile="spoofed-prompt",
        tool_policy="spoofed-policy",
        workflow_name="full",
        kb_ids=["kb-course-a", "kb-course-b"],
    )

    assert executor._domain == "materials-science"
    assert executor._prompt_profile == "materials_prompt"
    assert executor._tool_policy == "materials_default"
    assert captured["config"].prompt_profile == "materials_prompt"
    assert captured["config"].tool_policy == "materials_default"
    assert captured["config"].lightweight_mode is False
    assert captured["config"].lightweight_has_kb is True


def test_profiled_executor_rejects_binding_for_another_agent():
    profile = AgentProfile(
        code="materials",
        name="Materials",
        domain="materials-science",
        prompt_profile="materials_prompt",
        tool_policy="materials_default",
    )

    with pytest.raises(ValueError, match="agent_code"):
        ProfiledAgentExecutor(
            profile=profile,
            agent_bindings=_runtime_bindings(agent_code="another-agent"),
        )


def test_validator_bindings_context_expand_transitive_skill_dependencies(tmp_path):
    skills_root = tmp_path / "skills"

    def write_skill(name: str, dependencies: list[str]) -> None:
        skill_dir = skills_root / "public" / name
        skill_dir.mkdir(parents=True)
        dependency_yaml = "\n".join(f"  - {item}" for item in dependencies)
        skill_dir.joinpath("SKILL.md").write_text(
            "\n".join(
                [
                    "---",
                    f"name: {name}",
                    f"description: {name} contract skill",
                    "domain: dependency-domain",
                    "workflows:",
                    "  allowed:",
                    "    - full",
                    "dependencies:",
                    dependency_yaml,
                    "---",
                    f"# {name}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    write_skill("root-skill", ["middle-skill"])
    write_skill("middle-skill", ["leaf-skill"])
    write_skill("leaf-skill", [])

    profile = AgentProfile(
        code="dependency-agent",
        name="Dependency Agent",
        domain="dependency-domain",
        default_model="test-model",
        default_skills=["root-skill"],
        default_workflow="full",
        allowed_workflows=["full"],
        prompt_profile="contract",
        tool_policy="contract",
    )
    prompt_registry = SimpleNamespace(
        get_spec=lambda _name: PromptProfileSpec(
            name="contract",
            provider=f"{__name__}:_contract_prompt",
        ),
        resolve_provider=lambda _name: _contract_prompt,
    )
    tool_policy = ToolPolicy(name="contract")
    tool_registry = SimpleNamespace(
        get_policy=lambda _name: tool_policy,
        get=lambda name: (_ for _ in ()).throw(ValueError(f"Unknown tool: {name}")),
    )
    validator = AgentDefinitionValidator(
        profile_loader=SimpleNamespace(load=lambda _code: profile),
        prompt_registry=prompt_registry,
        tool_registry=tool_registry,
        skill_loader=SkillManifestLoader(
            skills_root=skills_root,
            tool_registry=tool_registry,
            validate_tools=False,
        ),
        workflow_specs={"full": WORKFLOW_SPECS["full"]},
    )
    service = AgentService(definition_validator=validator)

    definition = validator.resolve(profile.code)
    resolved_profile, bindings = service._load_profile_and_bindings(profile.code)

    assert [manifest.name for manifest in definition.skills] == [
        "leaf-skill",
        "middle-skill",
        "root-skill",
    ]
    assert bindings is not None
    assert bindings.skill_dependencies == (
        ("leaf-skill", ()),
        ("middle-skill", ("leaf-skill",)),
        ("root-skill", ("middle-skill",)),
    )

    context = service._build_context(
        AgentRequest(
            question="run",
            agent_code=profile.code,
            user_id="user-1",
        ),
        profile=resolved_profile,
        workflow_name="full",
        request_id="request-1",
        external_thread_id="thread-1",
        agent_bindings=bindings,
    )

    assert context.selected_skills == [
        "root-skill",
        "middle-skill",
        "leaf-skill",
    ]
    skills_provider = ScopedSkillsProvider(
        skills_base_path=skills_root,
        authorized_skill_names=set(context.selected_skills),
    )
    runtime_skills = skills_provider.get_available_skills_for_user(
        selected_skills=set(context.selected_skills)
    )
    assert {skill.name for skill in runtime_skills} == set(context.selected_skills)
    skills_provider.validate_skill_selection(runtime_skills)


def test_context_explicit_empty_skills_stays_empty():
    profile = AgentProfile(
        code="materials",
        name="Materials",
        domain="materials-science",
        default_model="test-model",
        default_skills=["root-skill"],
    )
    bindings = AgentRuntimeBindings(
        agent_code="materials",
        domain="materials-science",
        prompt_profile="materials_prompt",
        tool_policy="materials_default",
        guardrail_policy="default",
        default_workflow="full",
        skill_names=("root-skill", "dependency-skill"),
        skill_dependencies=(("root-skill", ("dependency-skill",)),),
    )

    context = AgentService(
        profile_loader=SimpleNamespace(load=lambda _code: profile)
    )._build_context(
        AgentRequest(
            question="run",
            agent_code=profile.code,
            user_id="user-1",
            selected_skills=[],
        ),
        profile=profile,
        workflow_name="full",
        request_id="request-1",
        external_thread_id="thread-1",
        agent_bindings=bindings,
    )

    assert context.selected_skills == []


def test_dependency_expansion_never_adds_skill_outside_definition():
    bindings = AgentRuntimeBindings(
        agent_code="materials",
        domain="materials-science",
        prompt_profile="materials_prompt",
        tool_policy="materials_default",
        guardrail_policy="default",
        default_workflow="full",
        skill_names=("root-skill", "allowed-dependency"),
        skill_dependencies=(
            ("root-skill", ("allowed-dependency", "outside-definition")),
        ),
    )

    assert AgentService._expand_selected_skill_dependencies(
        ["root-skill"],
        agent_bindings=bindings,
    ) == ["root-skill", "allowed-dependency"]
    assert AgentService._expand_selected_skill_dependencies(
        ["outside-definition", "root-skill"],
        agent_bindings=bindings,
    ) == ["outside-definition", "root-skill", "allowed-dependency"]
