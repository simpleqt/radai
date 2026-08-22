from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.application.agent_service import AgentService
from backend.application.agent_workflows.factories import create_standard_workflow
from backend.application.agents.definition import (
    AgentRuntimeRegistryScope,
    ResolvedAgentDefinition,
)
from backend.application.agents.prompt_registry import PromptProfileSpec
from backend.application.policies import ProfileRuntimePolicy
from backend.application.policies.knowledge_base_access import (
    KnowledgeBaseAccessPolicy,
)
from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.application.skills.runtime_resolver import (
    RuntimeSkillResolution,
    SkillRuntimeError,
)
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.workflows import WORKFLOW_SPECS
from backend.domain.tools.policy import ToolPolicy
from backend.domain.tools.spec import ToolSpec


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _SessionFactory:
    def __call__(self):
        return _SessionContext()


class _ToolRegistry:
    def __init__(self) -> None:
        self.specs = {
            "web_search": ToolSpec(
                name="web_search",
                provider="unused:web_search",
                domains={"fourth-domain"},
                workflows={"full"},
                required_skills={"dynamic-root"},
            ),
            "multi_kb_search": ToolSpec(
                name="multi_kb_search",
                provider="unused:multi_kb_search",
                domains={"fourth-domain"},
                workflows={"full"},
                required_skills={"dynamic-dependency"},
            ),
        }

    def get(self, name: str) -> ToolSpec:
        try:
            return self.specs[name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool: {name}") from exc


def _prompt_provider(**_kwargs) -> str:
    return "fourth-domain prompt"


def _write_skill(
    root: Path,
    name: str,
    *,
    dependencies: tuple[str, ...] = (),
    conflicts: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
) -> None:
    skill_dir = root / "public" / name
    skill_dir.mkdir(parents=True)
    lines = [
        "---",
        f"name: {name}",
        f"description: {name} test skill",
        "domain: fourth-domain",
        "workflows:",
        "  allowed:",
        "    - full",
        "dependencies:",
        *([f"  - {item}" for item in dependencies] or ["  []"]),
        "conflicts:",
        *([f"  - {item}" for item in conflicts] or ["  []"]),
        "tools:",
        *([f"  - {item}" for item in tools] or ["  []"]),
        "---",
        f"# {name}",
    ]
    skill_dir.joinpath("SKILL.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _definition(skills_root: Path) -> ResolvedAgentDefinition:
    tool_registry = _ToolRegistry()
    skill_loader = SkillManifestLoader(
        skills_root=skills_root,
        tool_registry=tool_registry,
        validate_tools=False,
    )
    profile = AgentProfile(
        code="fourth-agent",
        name="Fourth Agent",
        domain="fourth-domain",
        default_model="test-model",
        default_skills=["fourth-base"],
        default_workflow="full",
        allowed_workflows=["full"],
        prompt_profile="fourth-prompt",
        tool_policy="fourth-policy",
        kb_policy="disabled",
    )
    prompt_spec = PromptProfileSpec(
        name="fourth-prompt",
        provider=f"{__name__}:_prompt_provider",
    )
    workflow_spec = WORKFLOW_SPECS["full"]
    return ResolvedAgentDefinition(
        profile=profile,
        prompt_spec=prompt_spec,
        prompt_provider=_prompt_provider,
        tool_policy=ToolPolicy(
            name="fourth-policy",
            include_when_skill_selected={
                "dynamic-root": {"web_search"},
                "dynamic-dependency": {"multi_kb_search"},
            },
        ),
        tools=(),
        skills=(skill_loader.load("fourth-base"),),
        workflows=(workflow_spec,),
        workflow_providers=(("full", create_standard_workflow),),
        runtime_scope=AgentRuntimeRegistryScope(
            prompt_registry=SimpleNamespace(),
            tool_registry=tool_registry,
            skill_loader=skill_loader,
            skills_base_path=skills_root,
        ),
    )


def _service(definition: ResolvedAgentDefinition) -> AgentService:
    validator = SimpleNamespace(resolve=lambda _agent_code: definition)
    return AgentService(
        definition_validator=validator,
        runtime_policy=ProfileRuntimePolicy(
            kb_access_policy=KnowledgeBaseAccessPolicy(
                lookup=lambda _ids: {},
                binding_lookup=lambda _code: [],
                default_lookup=lambda: [],
            )
        ),
    )


@pytest.mark.asyncio
async def test_fourth_agent_dynamic_public_skill_closure_scopes_runtime(
    tmp_path,
    monkeypatch,
):
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "fourth-base")
    _write_skill(
        skills_root,
        "dynamic-root",
        dependencies=("dynamic-dependency",),
        tools=("web_search",),
    )
    _write_skill(
        skills_root,
        "dynamic-dependency",
        tools=("multi_kb_search",),
    )
    _write_skill(skills_root, "unselected-public")
    definition = _definition(skills_root)
    service = _service(definition)

    async def resolve_unmanaged(self, session, **_kwargs):
        return RuntimeSkillResolution(
            selected_skills=["dynamic-root"],
            available_skills=[],
            managed=False,
        )

    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: _SessionFactory(),
    )
    monkeypatch.setattr(
        "backend.application.skills.runtime_resolver.SkillRuntimeResolver.resolve",
        resolve_unmanaged,
    )

    request = await service.resolve_request_skills(
        AgentRequest(
            question="run the fourth-domain review",
            agent_code="fourth-agent",
            user_id="user-1",
            selected_skills=["dynamic-root"],
            auth_role="user",
        )
    )
    workflow, context = service._resolve_workflow(request)
    kwargs = workflow._build_agent_kwargs(request, context)

    assert request.selected_skills == ["dynamic-root", "dynamic-dependency"]
    assert context.selected_skills == ["dynamic-root", "dynamic-dependency"]
    assert kwargs["skills_provider"]._authorized_skill_names == {
        "dynamic-root",
        "dynamic-dependency",
    }
    assert "unselected-public" not in kwargs[
        "skills_provider"
    ]._authorized_skill_names
    assert definition.tool_policy.allowed_tool_names(
        selected_skills=set(context.selected_skills)
    ) == {"web_search", "multi_kb_search"}


@pytest.mark.asyncio
async def test_managed_selection_is_not_expanded_from_repository_manifest(
    tmp_path,
    monkeypatch,
):
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "fourth-base")
    _write_skill(
        skills_root,
        "dynamic-root",
        dependencies=("dynamic-dependency",),
        tools=("web_search",),
    )
    _write_skill(
        skills_root,
        "dynamic-dependency",
        tools=("multi_kb_search",),
    )
    definition = _definition(skills_root)
    service = _service(definition)

    async def resolve_managed(self, session, **_kwargs):
        # A real SkillRuntimeResolver rejects this incomplete dependency set.
        # The stub proves AgentService never repairs it from repository data,
        # which would widen the managed authorization decision.
        return RuntimeSkillResolution(
            selected_skills=["dynamic-root"],
            available_skills=[],
            managed=True,
        )

    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: _SessionFactory(),
    )
    monkeypatch.setattr(
        "backend.application.skills.runtime_resolver.SkillRuntimeResolver.resolve",
        resolve_managed,
    )

    request = await service.resolve_request_skills(
        AgentRequest(
            question="managed request",
            agent_code="fourth-agent",
            user_id="user-1",
            selected_skills=["dynamic-root"],
            auth_role="user",
        )
    )
    workflow, context = service._resolve_workflow(request)
    kwargs = workflow._build_agent_kwargs(request, context)

    assert request.selected_skills == ["dynamic-root"]
    assert context.selected_skills == ["dynamic-root"]
    assert kwargs["skills_provider"]._authorized_skill_names == {"dynamic-root"}


@pytest.mark.parametrize(
    ("selected_skills", "expected_code"),
    [
        (["missing-root"], "SKILL_DEPENDENCY_MISSING"),
        (["cycle-a"], "SKILL_DEPENDENCY_CYCLE"),
        (["conflict-a", "conflict-b"], "SKILL_CONFLICT"),
    ],
)
def test_unmanaged_dynamic_skill_closure_fails_fast(
    tmp_path,
    selected_skills,
    expected_code,
):
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "fourth-base")
    _write_skill(skills_root, "missing-root", dependencies=("not-installed",))
    _write_skill(skills_root, "cycle-a", dependencies=("cycle-b",))
    _write_skill(skills_root, "cycle-b", dependencies=("cycle-a",))
    _write_skill(skills_root, "conflict-a", conflicts=("conflict-b",))
    _write_skill(skills_root, "conflict-b")
    definition = _definition(skills_root)

    with pytest.raises(SkillRuntimeError) as exc_info:
        AgentService._resolve_unmanaged_skill_closure(
            definition,
            selected_skills=selected_skills,
            workflow_name="full",
        )

    assert exc_info.value.code == expected_code
