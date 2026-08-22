"""agent init/validate 脚手架与 CLI 契约（拆自 test_agent_definition_contracts.py）。"""

import asyncio
import json
import pytest
import yaml

from backend.application.agent_executors.graph_builder import _resolve_domain_skill_name
from backend.application.agent_executors.lightweight_qa_agent import (
    _build_lightweight_system_header_and_template,
)
from backend.application.agent_service import AgentService
from backend.application.agents.definition import AgentDefinitionValidator
from backend.application.agents.scaffolder import AgentScaffolder
from backend.application.policies import ProfileRuntimePolicy
from backend.application.policies.knowledge_base_access import KnowledgeBaseAccessPolicy
from backend.application.tools.assembler import PolicyToolAssembler
from backend.application.tools.registry import ToolRegistry
from backend.application.tools.runtime_invoker import RuntimeToolInvoker
from backend.application.tools.tool_assembly_pipeline import ToolAssemblyPipeline
from backend.cli import main as cli_main
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
from backend.core.skills.skill_runtime_state import (
    reset_request_skill_state,
    set_request_skill_state,
)
from backend.domain.agents.request import AgentRequest
from pathlib import Path


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


def test_agent_validate_cli_returns_machine_readable_failure(capsys):
    exit_code = cli_main(["agent", "validate", "not-registered"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output["valid"] is False
    assert output["reports"][0]["issues"][0]["component"] == "profile"


def test_agent_init_scaffolds_owned_files_and_registry_entries(tmp_path):
    prompt_registry, policy_registry = _prepare_scaffold_project(tmp_path)

    result = AgentScaffolder(tmp_path).create(
        agent_code="materials",
        name="Materials Agent",
        domain="materials-science",
        validate=True,
    )

    assert result.validated is True
    assert all(path.exists() for path in result.created_files)
    assert "code: materials" in result.created_files[0].read_text(encoding="utf-8")
    prompt_document = yaml.safe_load(prompt_registry.read_text(encoding="utf-8"))
    policy_document = yaml.safe_load(policy_registry.read_text(encoding="utf-8"))
    assert prompt_document["prompt_profiles"]["materials"]["aliases"] == [
        "materials",
        "materials-science",
    ]
    assert policy_document["policies"]["materials_default"]["include"] == [
        "multi_kb_search",
        "chat_file_search",
        "read_context_artifact",
        "context_read",
        "web_search",
    ]
    assert prompt_document["metadata"] == {"owner": "test"}
    assert policy_document["metadata"] == {"owner": "test"}
    profile_document = yaml.safe_load(
        result.created_files[0].read_text(encoding="utf-8")
    )
    assert profile_document["allowed_workflows"] == ["full"]
    assert profile_document["default_skills"] == ["materials-science"]
    assert result.created_files[2].parent.name == "materials-science"
    skill_frontmatter = yaml.safe_load(
        result.created_files[2].read_text(encoding="utf-8").split("---", 2)[1]
    )
    assert "chat_file_search" not in skill_frontmatter["tools"]
    assert (
        "chat_file_search"
        in policy_document["policies"]["materials_default"]["include"]
    )
    assert (
        _resolve_domain_skill_name(
            profile_document["domain"],
            set(profile_document["default_skills"]),
        )
        == "materials-science"
    )
    assert "preferred: full" in result.created_files[2].read_text(encoding="utf-8")
    with AgentScaffolder(tmp_path).definition_validator() as validator:
        generated = validator.resolve("materials")
        lightweight_header = generated.prompt_provider(
            subagent_enabled=False,
            use_search=False,
            available_skills=set(),
            task_type=None,
            lightweight_mode=True,
            lightweight_has_kb=False,
        )
    assert "nuclear-electronics" not in lightweight_header
    assert "csh-hydrogeology" not in lightweight_header
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        AgentScaffolder(tmp_path).create(
            agent_code="materials",
            name="Duplicate",
            validate=False,
        )


def test_agent_init_rolls_back_all_files_when_validation_fails(
    tmp_path,
    monkeypatch,
):
    prompt_registry, policy_registry = _prepare_scaffold_project(tmp_path)
    prompt_original = prompt_registry.read_text(encoding="utf-8")
    policy_original = policy_registry.read_text(encoding="utf-8")
    monkeypatch.setattr(
        AgentDefinitionValidator,
        "resolve",
        lambda self, code: (_ for _ in ()).throw(ValueError("invalid generated agent")),
    )

    with pytest.raises(ValueError, match="invalid generated agent"):
        AgentScaffolder(tmp_path).create(
            agent_code="materials",
            name="Materials Agent",
            validate=True,
        )

    assert prompt_registry.read_text(encoding="utf-8") == prompt_original
    assert policy_registry.read_text(encoding="utf-8") == policy_original
    assert not (tmp_path / "backend" / "config" / "agents" / "materials.yaml").exists()
    assert not (
        tmp_path
        / "backend"
        / "application"
        / "agents"
        / "prompt_templates"
        / "materials.py"
    ).exists()
    assert not (
        tmp_path / "backend" / "skills" / "public" / "materials" / "SKILL.md"
    ).exists()
    assert not (
        tmp_path / "backend" / "skills" / "public" / "materials-science" / "SKILL.md"
    ).exists()


def test_agent_init_removes_partial_new_file_and_restores_registry_bytes(
    tmp_path,
    monkeypatch,
):
    prompt_registry, policy_registry = _prepare_scaffold_project(tmp_path)
    registry_originals = {
        prompt_registry: prompt_registry.read_bytes(),
        policy_registry: policy_registry.read_bytes(),
    }
    root_entries = set(tmp_path.iterdir())
    original_write_new_file = AgentScaffolder._write_new_file

    def fail_during_prompt_write(path, content):
        if path.name == "materials.py":
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as file_obj:
                file_obj.write(content[:20])
                raise OSError("simulated partial scaffold write")
        original_write_new_file(path, content)

    monkeypatch.setattr(
        AgentScaffolder,
        "_write_new_file",
        staticmethod(fail_during_prompt_write),
    )

    with pytest.raises(OSError, match="simulated partial scaffold write"):
        AgentScaffolder(tmp_path).create(
            agent_code="materials",
            name="Materials Agent",
            domain="materials-science",
            validate=False,
        )

    expected_created_files = (
        tmp_path / "backend" / "config" / "agents" / "materials.yaml",
        tmp_path
        / "backend"
        / "application"
        / "agents"
        / "prompt_templates"
        / "materials.py",
        tmp_path / "backend" / "skills" / "public" / "materials-science" / "SKILL.md",
    )
    assert all(not path.exists() for path in expected_created_files)
    assert {
        path: path.read_bytes() for path in registry_originals
    } == registry_originals
    assert set(tmp_path.iterdir()) == root_entries


def test_agent_init_serializes_dry_run_check_with_concurrent_creation(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    _prepare_scaffold_project(tmp_path)
    first_entered = Event()
    release_first = Event()
    second_started = Event()
    second_finished = Event()

    class BlockingScaffolder(AgentScaffolder):
        def _create_locked(self, **kwargs):
            first_entered.set()
            if not release_first.wait(timeout=5):
                raise TimeoutError("concurrency test did not release first create")
            return super()._create_locked(**kwargs)

    def create_first():
        return BlockingScaffolder(tmp_path).create(
            agent_code="raced-agent",
            name="Raced Agent",
            validate=False,
        )

    def check_same_agent_with_dry_run():
        second_started.set()
        try:
            return AgentScaffolder(tmp_path).create(
                agent_code="raced-agent",
                name="Raced Agent",
                validate=False,
                dry_run=True,
            )
        finally:
            second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(create_first)
        assert first_entered.wait(timeout=2)
        second_future = executor.submit(check_same_agent_with_dry_run)
        assert second_started.wait(timeout=2)
        try:
            assert not second_finished.wait(timeout=0.1)
        finally:
            release_first.set()

        first_result = first_future.result(timeout=10)
        with pytest.raises(FileExistsError, match="Refusing to overwrite"):
            second_future.result(timeout=10)

    assert all(path.exists() for path in first_result.created_files)
    prompt_profiles = yaml.safe_load(
        (tmp_path / "backend" / "config" / "prompts" / "registry.yaml").read_text(
            encoding="utf-8"
        )
    )["prompt_profiles"]
    policies = yaml.safe_load(
        (tmp_path / "backend" / "config" / "tools" / "policies.yaml").read_text(
            encoding="utf-8"
        )
    )["policies"]
    assert set(prompt_profiles) == {"raced-agent"}
    assert set(policies) == {"raced-agent_default"}


def test_agent_init_lightweight_uses_only_reachable_tools_and_safe_module_text(
    tmp_path,
):
    _prepare_scaffold_project(tmp_path)

    result = AgentScaffolder(tmp_path).create(
        agent_code="safe-agent",
        name='Unsafe """ display name',
        domain="safe-domain",
        default_workflow="lightweight",
        validate=True,
    )

    profile = yaml.safe_load(result.created_files[0].read_text(encoding="utf-8"))
    policies = yaml.safe_load(
        (tmp_path / "backend" / "config" / "tools" / "policies.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert profile["allowed_workflows"] == ["lightweight"]
    assert policies["policies"]["safe-agent_default"]["include"] == [
        "multi_kb_search",
        "chat_file_search",
        "web_search",
    ]
    compile(
        result.created_files[1].read_text(encoding="utf-8"),
        str(result.created_files[1]),
        "exec",
    )
    assert not list(result.created_files[1].parent.glob("__pycache__/safe_agent*.pyc"))


def test_agent_init_rejects_prompt_alias_collision_before_writing(tmp_path):
    prompt_registry, _policy_registry = _prepare_scaffold_project(tmp_path)
    prompt_registry.write_text(
        """prompt_profiles:
  existing:
    provider: builtins:str
    aliases: [claimed-domain]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Prompt alias already claimed"):
        AgentScaffolder(tmp_path).create(
            agent_code="new-agent",
            name="New Agent",
            domain="claimed-domain",
            validate=False,
        )

    assert not (tmp_path / "backend" / "config" / "agents" / "new-agent.yaml").exists()


def test_agent_init_requires_source_checkout(tmp_path):
    with pytest.raises(ValueError, match="source checkout"):
        AgentScaffolder(tmp_path).create(
            agent_code="materials",
            name="Materials",
            validate=False,
        )


def test_agent_validate_rejects_scalar_policy_include_instead_of_empty_greenlight(
    tmp_path,
    capsys,
):
    _prepare_scaffold_project(tmp_path)
    policy_registry = tmp_path / "backend" / "config" / "tools" / "policies.yaml"
    policy_registry.write_text(
        """policies:
  malformed:
    include: web_search
""",
        encoding="utf-8",
    )

    exit_code = cli_main(["agent", "validate", "--project-root", str(tmp_path)])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output["valid"] is False
    assert output["reports"] == []
    assert "field include must be a list" in output["error"]


def test_agent_validate_cli_can_read_scaffold_target_tree(tmp_path, capsys):
    _prepare_scaffold_project(tmp_path)
    AgentScaffolder(tmp_path).create(
        agent_code="materials",
        name="Materials Agent",
        domain="materials-science",
        validate=True,
    )

    exit_code = cli_main(
        [
            "agent",
            "validate",
            "materials",
            "--project-root",
            str(tmp_path),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["valid"] is True
    assert output["reports"][0]["definition"]["agent_code"] == "materials"


def test_scaffold_to_runtime_preserves_fourth_domain_skill_guard(tmp_path):
    """Prove one generated declaration set crosses every central boundary."""
    _prepare_scaffold_project(tmp_path)
    scaffolder = AgentScaffolder(tmp_path)
    scaffolder.create(
        agent_code="materials",
        name="Materials Agent",
        domain="materials-science",
        default_workflow="full",
        validate=True,
    )

    with scaffolder.definition_validator() as validator:
        definition = validator.resolve("materials")
        profile, bindings = AgentService(
            definition_validator=validator
        )._load_profile_and_bindings("materials")

        assert definition.profile is profile
        assert bindings is not None
        assert bindings.domain == "materials-science"
        assert bindings.skill_names == ("materials-science",)

        # A new session has no attachment index.  The optional
        # chat_file_search capability remains policy-authorized, but must not
        # become an unconditional Skill dependency that blocks graph build.
        tools = ToolAssemblyPipeline(
            kb_ids=[],
            use_search=False,
            selected_skills=set(profile.default_skills),
            skills_provider=ScopedSkillsProvider(
                skills_base_path=tmp_path / "backend" / "skills"
            ),
            domain=bindings.domain,
            workflow_name="full",
            tool_policy=bindings.tool_policy,
            policy_tool_assembler=PolicyToolAssembler(
                registry=ToolRegistry(
                    config_dir=tmp_path / "backend" / "config" / "tools"
                )
            ),
        ).assemble()
        assert "chat_file_search" not in {tool.name for tool in tools}

        pipeline = ToolAssemblyPipeline(
            kb_ids=["kb-materials"],
            selected_skills=set(profile.default_skills),
            domain=bindings.domain,
            workflow_name="full",
            tool_policy=bindings.tool_policy,
        )
        search_tool = pipeline._create_multi_kb_search_tool()
        token = set_request_skill_state({"materials-science"})
        try:
            result = asyncio.run(search_tool.ainvoke({"query": "austenite phase"}))
        finally:
            reset_request_skill_state(token)

    payload = json.loads(result)
    assert payload["error"] == "skill_not_loaded"
    assert payload["required_skill"] == "materials-science"


def test_scaffold_definition_composes_real_executor_from_target_registries(
    tmp_path,
    monkeypatch,
):
    """Validation and executor construction must consume the same source tree."""

    _prepare_scaffold_project(tmp_path)
    scaffolder = AgentScaffolder(tmp_path)
    scaffolder.create(
        agent_code="materials",
        name="Materials Agent",
        domain="materials-science",
        default_workflow="full",
        validate=True,
    )

    captured: dict[str, object] = {}

    class _FakeGraph:
        name = "materials-test-graph"

    def fake_create_agent_graph(*, tools, system_prompt, **_kwargs):
        captured["tools"] = tools
        captured["system_prompt"] = system_prompt
        return _FakeGraph()

    monkeypatch.setattr(
        "backend.core.models.create_chat_model",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "backend.core.agents.create_agent_graph",
        fake_create_agent_graph,
    )

    with scaffolder.definition_validator() as validator:
        definition = validator.resolve("materials")
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
                question="Explain austenite phase stability",
                agent_code="materials",
                user_id="materials-user",
                selected_skills=["materials-science"],
                use_search=False,
            )
        )
        executor = workflow.build_executor(
            AgentRequest(
                question="Explain austenite phase stability",
                agent_code="materials",
                user_id="materials-user",
                selected_skills=["materials-science"],
                use_search=False,
            ),
            context,
        )

        assert executor._resolved_agent_definition is definition
        assert executor._prompt_provider is definition.prompt_provider
        assert (
            executor._policy_tool_assembler._registry
            is definition.runtime_scope.tool_registry
        )
        internal_invoker = RuntimeToolInvoker(
            context=context,
            workflow_name="full",
            resolved_agent_definition=definition,
        )
        assert "read_context_artifact" in internal_invoker.available_tool_names()

    assert "<agent_domain>materials-science</agent_domain>" in str(
        captured["system_prompt"]
    )
    assert "materials-science" in str(captured["system_prompt"])
    lightweight_header, _ = _build_lightweight_system_header_and_template(
        prompt_profile="materials",
        domain="materials-science",
        use_search=False,
        lightweight_has_kb=False,
        prompt_provider=definition.prompt_provider,
    )
    assert "<agent_domain>materials-science</agent_domain>" in lightweight_header
