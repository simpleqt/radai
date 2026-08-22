"""Agent 定义解析与注册表校验器契约（拆自 test_agent_definition_contracts.py）。"""

import pytest
import shutil
import sys
import yaml

from backend.application.agent_service import AgentService
from backend.application.agent_workflows.factories import create_standard_workflow
from backend.application.agents.definition import AgentDefinitionValidator
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.agents.prompt_registry import PromptTemplateRegistry
from backend.application.agents.scaffolder import AgentScaffolder
from backend.application.policies import ProfileRuntimePolicy
from backend.application.policies.knowledge_base_access import KnowledgeBaseAccessPolicy
from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.application.skills.public_catalog import PublicSkillCatalog
from backend.application.tools.registry import ToolRegistry
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.workflows import get_workflow_spec
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _prepare_scaffold_project(tmp_path: Path) -> tuple[Path, Path]:
    """Create the registry subset required by an independent source tree."""
    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\n",
        encoding="utf-8",
    )
    prompt_registry = tmp_path / "backend" / "config" / "prompts" / "registry.yaml"
    tools_dir = tmp_path / "backend" / "config" / "tools"
    policy_registry = tools_dir / "policies.yaml"
    workflow_registry = tmp_path / "backend" / "config" / "workflows" / "registry.yaml"
    prompt_registry.parent.mkdir(parents=True)
    tools_dir.mkdir(parents=True)
    workflow_registry.parent.mkdir(parents=True)
    prompt_registry.write_text(
        "prompt_profiles: {}\nmetadata:\n  owner: test\n",
        encoding="utf-8",
    )
    policy_registry.write_text(
        "policies: {}\nmetadata:\n  owner: test\n",
        encoding="utf-8",
    )
    (tools_dir / "registry.yaml").write_text(
        (PROJECT_ROOT / "backend" / "config" / "tools" / "registry.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    workflow_registry.write_text(
        (PROJECT_ROOT / "backend" / "config" / "workflows" / "registry.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    return prompt_registry, policy_registry


def _install_virtual_prompt_provider(monkeypatch) -> str:
    module_name = "virtual_materials_agent_prompt"
    module = ModuleType(module_name)

    def build_system_prompt(
        subagent_enabled=False,
        use_search=True,
        available_skills=None,
        task_type=None,
        *,
        lightweight_mode=False,
        lightweight_has_kb=False,
    ):
        del (
            subagent_enabled,
            use_search,
            available_skills,
            task_type,
            lightweight_mode,
            lightweight_has_kb,
        )
        return "<agent_domain>materials-science</agent_domain>"

    module.build_system_prompt = build_system_prompt
    monkeypatch.setitem(sys.modules, module_name, module)
    return module_name


def test_lightweight_is_a_manifest_selected_unified_executor_strategy():
    spec = get_workflow_spec("lightweight")
    workflow = create_standard_workflow(name="lightweight")

    assert spec is not None
    assert spec.execution_strategy == "lightweight"
    assert workflow._execution_strategy == "lightweight"
    assert type(workflow).__name__ == "StandardAgentWorkflow"


def test_registered_profiles_resolve_to_complete_definitions():
    reports = AgentDefinitionValidator().validate_all()

    assert {report.agent_code for report in reports} == {
        "audit",
        "ne",
        "csh",
        "fmca",
        "groundwater",
        "course_eval",
        "landslide",
    }
    assert all(report.valid for report in reports)
    assert all(report.definition and report.definition.tools for report in reports)


def test_ne_uses_full_default_and_keeps_lightweight_for_explore():
    profile = AgentProfileLoader().load("ne")

    assert profile.default_workflow == "full"
    assert "full" in profile.allowed_workflows
    assert "lightweight" in profile.allowed_workflows


def test_fmca_keeps_full_default_and_allows_explicit_lightweight():
    profile = AgentProfileLoader().load("fmca")

    assert profile.default_workflow == "full"
    assert "full" in profile.allowed_workflows
    assert "lightweight" in profile.allowed_workflows


def test_validator_aggregates_cross_registry_failures(tmp_path):
    (tmp_path / "broken.yaml").write_text(
        """
code: broken
name: Broken
domain: broken-domain
default_skills: [missing-skill]
default_workflow: full
allowed_workflows: [full]
prompt_profile: missing-prompt
tool_policy: missing-policy
guardrail_policy: missing-guardrail
domain_context_provider: missing_provider_module:resolve
""",
        encoding="utf-8",
    )

    report = AgentDefinitionValidator(
        profile_loader=AgentProfileLoader(config_dir=tmp_path)
    ).validate("broken")

    assert report.valid is False
    assert {issue.component for issue in report.issues} >= {
        "prompt",
        "tool_policy",
        "skill",
        "guardrail",
        "domain_context",
    }


def test_fourth_domain_is_resolved_from_extension_contract_only(
    tmp_path,
    monkeypatch,
):
    """A new domain needs declarations and its own provider, not core edits."""
    module_name = _install_virtual_prompt_provider(monkeypatch)
    agents_dir = tmp_path / "agents"
    prompts_dir = tmp_path / "prompts"
    tools_dir = tmp_path / "tools"
    skills_dir = tmp_path / "skills"
    for path in (
        agents_dir,
        prompts_dir,
        tools_dir,
        skills_dir / "public" / "materials-science",
    ):
        path.mkdir(parents=True, exist_ok=True)

    (agents_dir / "materials.yaml").write_text(
        """
code: materials
name: Materials Agent
domain: materials-science
default_skills: [materials-science]
default_workflow: full
allowed_workflows: [lightweight, full]
prompt_profile: materials
tool_policy: materials_default
guardrail_policy: default
ui_profile: materials
""",
        encoding="utf-8",
    )
    (prompts_dir / "registry.yaml").write_text(
        f"""
prompt_profiles:
  materials:
    provider: {module_name}:build_system_prompt
    aliases: [materials-science]
""",
        encoding="utf-8",
    )
    (tools_dir / "registry.yaml").write_text(
        (PROJECT_ROOT / "backend" / "config" / "tools" / "registry.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (tools_dir / "policies.yaml").write_text(
        """
policies:
  materials_default:
    include: [multi_kb_search, chat_file_search, read_context_artifact, web_search]
    include_when_skill_selected: {}
    exclude: []
""",
        encoding="utf-8",
    )
    (skills_dir / "public" / "materials-science" / "SKILL.md").write_text(
        """---
name: materials-science
description: Materials evidence retrieval and analysis
domain: materials-science
tools: [multi_kb_search, chat_file_search, read_context_artifact, web_search]
workflows:
  allowed: [lightweight, full]
  preferred: full
---

# Materials
Use evidence before inference.
""",
        encoding="utf-8",
    )

    tool_registry = ToolRegistry(config_dir=tools_dir)
    validator = AgentDefinitionValidator(
        profile_loader=AgentProfileLoader(config_dir=agents_dir),
        prompt_registry=PromptTemplateRegistry(prompts_dir / "registry.yaml"),
        tool_registry=tool_registry,
        skill_loader=SkillManifestLoader(
            skills_root=skills_dir,
            tool_registry=tool_registry,
        ),
    )

    definition = validator.resolve("materials")

    assert definition.profile.domain == "materials-science"
    assert definition.prompt_spec.provider == f"{module_name}:build_system_prompt"
    assert {tool.name for tool in definition.tools} == {
        "multi_kb_search",
        "chat_file_search",
        "read_context_artifact",
        "web_search",
    }
    assert [skill.name for skill in definition.skills] == ["materials-science"]


def test_fourth_domain_public_catalog_never_leaks_other_domain_skills(tmp_path):
    _prepare_scaffold_project(tmp_path)
    shutil.copytree(
        PROJECT_ROOT / "backend" / "skills" / "public",
        tmp_path / "backend" / "skills" / "public",
        dirs_exist_ok=True,
    )
    AgentScaffolder(tmp_path).create(
        agent_code="materials",
        name="Materials Agent",
        domain="materials-science",
        validate=True,
    )
    tool_registry = ToolRegistry(config_dir=tmp_path / "backend" / "config" / "tools")
    loader = SkillManifestLoader(
        skills_root=tmp_path / "backend" / "skills",
        tool_registry=tool_registry,
        validate_tools=False,
    )
    profile = AgentProfileLoader(
        config_dir=tmp_path / "backend" / "config" / "agents",
        supported_workflows={"full": get_workflow_spec("full")},
    ).load("materials")

    visible = PublicSkillCatalog(
        skill_loader=loader,
        tool_registry=tool_registry,
    ).list_compatible(profile)

    assert {manifest.name for manifest in visible} == {"materials-science"}


def test_validate_all_reports_malformed_profile_without_hiding_valid_profiles(
    tmp_path,
):
    (tmp_path / "ne.yaml").write_text(
        (PROJECT_ROOT / "backend" / "config" / "agents" / "ne.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (tmp_path / "broken.yaml").write_text("code: [unterminated\n", encoding="utf-8")

    reports = AgentDefinitionValidator(
        profile_loader=AgentProfileLoader(config_dir=tmp_path)
    ).validate_all()

    assert [report.agent_code for report in reports] == ["broken", "ne"]
    assert reports[0].valid is False
    assert reports[0].issues[-1].component == "profile"
    assert reports[1].valid is True


@pytest.mark.parametrize(
    ("field_name", "configured_value"),
    [
        ("allowed_workflows", "full"),
        ("default_skills", "domain-skill"),
        ("default_kb_ids", "kb-1"),
    ],
)
def test_profile_loader_rejects_explicit_non_list_fields(
    tmp_path,
    field_name,
    configured_value,
):
    document = {
        "code": "strict-profile",
        "name": "Strict Profile",
        "domain": "strict-domain",
        "default_workflow": "full",
        "allowed_workflows": ["full"],
        "default_skills": [],
        "default_kb_ids": [],
    }
    document[field_name] = configured_value
    (tmp_path / "strict-profile.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=rf"field {field_name} must be a list"):
        AgentProfileLoader(config_dir=tmp_path).load("strict-profile")


def test_profile_loader_preserves_defaults_only_when_list_fields_are_absent(tmp_path):
    (tmp_path / "minimal.yaml").write_text(
        "code: minimal\nname: Minimal\ndomain: minimal\n",
        encoding="utf-8",
    )

    profile = AgentProfileLoader(config_dir=tmp_path).load("minimal")

    assert profile.default_kb_ids == []
    assert profile.default_skills == []
    assert profile.default_workflow == "lightweight"
    assert "full" in profile.allowed_workflows


def test_validator_aggregates_scalar_allowed_workflows_as_profile_failure(tmp_path):
    (tmp_path / "bad-workflows.yaml").write_text(
        """code: bad-workflows
name: Bad Workflows
domain: bad-workflows
default_workflow: full
allowed_workflows: full
""",
        encoding="utf-8",
    )
    (tmp_path / "ne.yaml").write_text(
        (PROJECT_ROOT / "backend" / "config" / "agents" / "ne.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    reports = AgentDefinitionValidator(
        profile_loader=AgentProfileLoader(config_dir=tmp_path)
    ).validate_all()

    assert [report.agent_code for report in reports] == ["bad-workflows", "ne"]
    assert reports[0].valid is False
    assert reports[0].definition is None
    assert any(
        issue.component == "profile"
        and issue.code == "load_failed"
        and "allowed_workflows must be a list" in issue.message
        for issue in reports[0].issues
    )
    assert reports[1].valid is True


def test_prompt_registry_rejects_duplicate_aliases(tmp_path):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        """prompt_profiles:
  first:
    provider: builtins:str
    aliases: [shared]
  second:
    provider: builtins:str
    aliases: [shared]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate prompt profile alias"):
        PromptTemplateRegistry(registry)


def test_validator_rejects_resolvable_but_incompatible_prompt_provider(tmp_path):
    agents_dir = tmp_path / "agents"
    prompts_dir = tmp_path / "prompts"
    agents_dir.mkdir()
    prompts_dir.mkdir()
    (agents_dir / "bad-provider.yaml").write_text(
        """code: bad-provider
name: Bad Provider
domain: bad-provider
default_skills: []
default_workflow: full
allowed_workflows: [full]
prompt_profile: bad-provider
tool_policy: ne_default
kb_policy: made-up-policy
guardrail_policy: default
""",
        encoding="utf-8",
    )
    (prompts_dir / "registry.yaml").write_text(
        """prompt_profiles:
  bad-provider:
    provider: builtins:len
    aliases: []
""",
        encoding="utf-8",
    )

    report = AgentDefinitionValidator(
        profile_loader=AgentProfileLoader(config_dir=agents_dir),
        prompt_registry=PromptTemplateRegistry(prompts_dir / "registry.yaml"),
    ).validate("bad-provider")

    issue_codes = {(issue.component, issue.code) for issue in report.issues}
    assert ("prompt", "resolution_failed") in issue_codes
    assert ("kb_policy", "unknown") in issue_codes


def test_validator_rejects_async_prompt_provider(tmp_path, monkeypatch):
    module_name = "async_agent_prompt_provider"
    module = ModuleType(module_name)

    async def build_system_prompt(**_kwargs):
        return "never awaited"

    module.build_system_prompt = build_system_prompt
    monkeypatch.setitem(sys.modules, module_name, module)
    agents_dir = tmp_path / "agents"
    prompts_dir = tmp_path / "prompts"
    agents_dir.mkdir()
    prompts_dir.mkdir()
    (agents_dir / "async-agent.yaml").write_text(
        """code: async-agent
name: Async Agent
domain: async-agent
default_skills: []
default_workflow: full
allowed_workflows: [full]
prompt_profile: async-agent
tool_policy: ne_default
guardrail_policy: default
""",
        encoding="utf-8",
    )
    (prompts_dir / "registry.yaml").write_text(
        f"""prompt_profiles:
  async-agent:
    provider: {module_name}:build_system_prompt
""",
        encoding="utf-8",
    )

    report = AgentDefinitionValidator(
        profile_loader=AgentProfileLoader(config_dir=agents_dir),
        prompt_registry=PromptTemplateRegistry(prompts_dir / "registry.yaml"),
    ).validate("async-agent")

    assert any("must be synchronous" in issue.message for issue in report.issues)


def test_validator_resolves_transitive_skill_dependency_closure(tmp_path):
    agents_dir = tmp_path / "agents"
    skills_dir = tmp_path / "skills"
    agents_dir.mkdir()
    for skill_name, dependencies in (
        ("root-skill", ["dependency"]),
        ("dependency", []),
    ):
        skill_dir = skills_dir / "public" / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            + yaml.safe_dump(
                {
                    "name": skill_name,
                    "description": f"{skill_name} description",
                    "domain": "closure-agent",
                    "tools": [],
                    "workflows": {"allowed": ["full"], "preferred": "full"},
                    "dependencies": dependencies,
                },
                sort_keys=False,
            )
            + "---\n",
            encoding="utf-8",
        )
    (agents_dir / "closure-agent.yaml").write_text(
        """code: closure-agent
name: Closure Agent
domain: closure-agent
default_skills: [root-skill]
default_workflow: full
allowed_workflows: [full]
prompt_profile: ne
tool_policy: ne_default
guardrail_policy: default
""",
        encoding="utf-8",
    )

    definition = AgentDefinitionValidator(
        profile_loader=AgentProfileLoader(config_dir=agents_dir),
        skill_loader=SkillManifestLoader(
            skills_root=skills_dir,
            validate_tools=False,
        ),
    ).resolve("closure-agent")

    assert [manifest.name for manifest in definition.skills] == [
        "dependency",
        "root-skill",
    ]


def test_validator_rejects_tool_factory_with_unsupported_required_argument(
    tmp_path,
):
    agents_dir = tmp_path / "agents"
    tools_dir = tmp_path / "tools"
    agents_dir.mkdir()
    tools_dir.mkdir()
    (agents_dir / "bad-tool.yaml").write_text(
        """code: bad-tool
name: Bad Tool
domain: bad-tool
default_skills: []
default_workflow: full
allowed_workflows: [full]
prompt_profile: ne
tool_policy: bad_tool_policy
guardrail_policy: default
""",
        encoding="utf-8",
    )
    (tools_dir / "registry.yaml").write_text(
        """tools:
  invalid_factory:
    provider: builtins:len
    domains: ['*']
    workflows: [full]
    required_skills: []
""",
        encoding="utf-8",
    )
    (tools_dir / "policies.yaml").write_text(
        """policies:
  bad_tool_policy:
    include: [invalid_factory]
    include_when_skill_selected: {}
    exclude: []
""",
        encoding="utf-8",
    )

    tool_registry = ToolRegistry(config_dir=tools_dir)
    report = AgentDefinitionValidator(
        profile_loader=AgentProfileLoader(config_dir=agents_dir),
        tool_registry=tool_registry,
        skill_loader=SkillManifestLoader(
            skills_root=tmp_path / "skills",
            tool_registry=tool_registry,
            validate_tools=False,
        ),
    ).validate("bad-tool")

    assert any(
        issue.component == "tool_policy"
        and issue.code == "tool_resolution_failed"
        and "unsupported runtime parameters" in issue.message
        for issue in report.issues
    )


@pytest.mark.parametrize(
    ("field_name", "configured_value", "message_field"),
    [
        ("include", "web_search", "include"),
        ("exclude", "web_search", "exclude"),
        (
            "include_when_skill_selected",
            ["web_search"],
            "include_when_skill_selected",
        ),
        (
            "include_when_skill_selected",
            {"deep-research": "web_search"},
            "include_when_skill_selected.deep-research",
        ),
    ],
)
def test_tool_registry_rejects_malformed_policy_collection_fields(
    tmp_path,
    field_name,
    configured_value,
    message_field,
):
    (tmp_path / "registry.yaml").write_text("tools: {}\n", encoding="utf-8")
    policy = {"include": [], "exclude": [], "include_when_skill_selected": {}}
    policy[field_name] = configured_value
    (tmp_path / "policies.yaml").write_text(
        yaml.safe_dump({"policies": {"strict": policy}}, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=rf"{message_field} must be a (list|mapping)"):
        ToolRegistry(config_dir=tmp_path)


@pytest.mark.parametrize(
    "field_name",
    ["domains", "workflows", "required_skills", "retryable_errors"],
)
def test_tool_registry_rejects_malformed_tool_spec_list_fields(
    tmp_path,
    field_name,
):
    tool = {"provider": "builtins:str", field_name: "full"}
    (tmp_path / "registry.yaml").write_text(
        yaml.safe_dump({"tools": {"strict_tool": tool}}, sort_keys=False),
        encoding="utf-8",
    )
    (tmp_path / "policies.yaml").write_text("policies: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match=rf"field {field_name} must be a list"):
        ToolRegistry(config_dir=tmp_path)


def test_tool_registry_preserves_empty_defaults_only_for_absent_fields(tmp_path):
    (tmp_path / "registry.yaml").write_text(
        "tools:\n  minimal:\n    provider: builtins:str\n",
        encoding="utf-8",
    )
    (tmp_path / "policies.yaml").write_text(
        "policies:\n  minimal: {}\n",
        encoding="utf-8",
    )

    registry = ToolRegistry(config_dir=tmp_path)

    assert registry.get("minimal").domains == set()
    assert registry.get("minimal").workflows == set()
    assert registry.get("minimal").required_skills == set()
    assert registry.get("minimal").retryable_errors == set()
    assert registry.get_policy("minimal").include == set()
    assert registry.get_policy("minimal").exclude == set()
    assert registry.get_policy("minimal").include_when_skill_selected == {}


def test_resolved_definition_routes_dynamic_skill_with_scoped_loader():
    validator = AgentDefinitionValidator()
    service = AgentService(
        definition_validator=validator,
        runtime_policy=ProfileRuntimePolicy(
            kb_access_policy=KnowledgeBaseAccessPolicy(
                lookup=lambda _ids: {},
                binding_lookup=lambda _code: [],
            )
        ),
    )

    workflow, context = service._resolve_workflow(
        AgentRequest(
            question="Perform a systematic evidence review",
            agent_code="ne",
            user_id="dynamic-skill-user",
            selected_skills=["deep-research"],
            skills_resolved=True,
            skills_requested_for_workflow=True,
        )
    )

    assert context.workflow_name == "plan_solve"
    assert workflow.name == "plan_solve"
    assert workflow._resolved_definition is validator.resolve("ne")
