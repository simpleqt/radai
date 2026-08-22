"""Profile/Prompt 装配与执行器注册契约（拆自 test_agent_platform_standardization.py）。"""

import importlib.util
import inspect
import pytest

from backend.application.agent_executors import (
    AgentExecutorRegistry,
    ProfiledAgentExecutor,
    ProfiledGraphBuildConfig,
    ProfiledGraphBuilder,
)
from backend.application.agent_executors.streaming_runtime import (
    AgentStreamRuntime,
    StreamRuntimeConfig,
)
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.agents.prompt_builder import PromptBuildContext, PromptBuilder
from backend.application.agents.prompt_registry import PromptTemplateRegistry
from backend.domain.agents.profile import AgentProfile
from pathlib import Path


def _module_not_found(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is None
    except ModuleNotFoundError:
        return True


def test_agent_executor_registry_returns_profiled_executor(monkeypatch):
    class FakeProfiledExecutor:
        def __init__(self, *, profile, **kwargs):
            self.profile = profile
            self.kwargs = kwargs

    import backend.application.agent_executors.registry as registry_module

    monkeypatch.setattr(registry_module, "ProfiledAgentExecutor", FakeProfiledExecutor)
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")

    executor = AgentExecutorRegistry().build(profile, model_name="m1")
    source = inspect.getsource(registry_module.AgentExecutorRegistry)

    assert isinstance(executor, FakeProfiledExecutor)
    assert executor.profile is profile
    assert executor.kwargs["model_name"] == "m1"
    assert "NEFullFeatureAgent" not in source
    assert "CSHFullFeatureAgent" not in source


def test_profiled_executor_is_platform_executor_contract():
    source = inspect.getsource(ProfiledAgentExecutor)

    assert "aquery_stream" in source
    assert "ainvoke" in source
    assert "def graph" in source
    assert "create_profiled_full_agent" not in source
    assert "agent_factory" not in source
    assert "NEFullFeatureAgent" not in source
    assert "CSHFullFeatureAgent" not in source


def test_legacy_full_feature_agent_modules_are_removed():
    removed_modules = [
        "backend.custom.agents.agent_factory",
        "backend.custom.agents.ne_full_feature_agent",
        "backend.custom.agents.csh_full_feature_agent",
        "backend.custom.agents.deep_research_orchestrator_agent",
        "backend.custom.agents.csh_deep_research_orchestrator_agent",
    ]

    for module_name in removed_modules:
        assert _module_not_found(module_name)


def test_low_level_runtime_modules_moved_out_of_custom():
    assert not Path("backend/custom").exists()

    removed_modules = [
        "backend.custom",
        "backend.custom.tools.request_limits",
        "backend.custom.agents.streaming.image_url_registry",
        "backend.custom.agents.streaming.stream_timeouts",
        "backend.custom.agents.streaming.stream_utils",
        "backend.custom.agents.streaming.stream_handler",
        "backend.custom.agents.lightweight_qa_agent",
        "backend.custom.agents.support.artifact_utils",
        "backend.custom.agents.support.prompts",
        "backend.custom.agents.support.csh_prompts",
        "backend.custom.agents.support.tool_assembly",
        "backend.custom.agents.support.explore_prompts",
        "backend.custom.agents.support.follow_up_prompts",
        "backend.custom.agents.research_draft_agent",
        "backend.custom.workflow.research_state",
        "backend.custom.tasks.manager",
        "backend.custom.tasks.runner",
    ]

    for module_name in removed_modules:
        assert _module_not_found(module_name)

    import backend.application.agent_executors.profiled_executor as profiled_executor
    import backend.application.agent_executors.stream_handler as stream_handler
    import backend.application.agent_executors.stream_tool_events as stream_tool_events
    import backend.application.agent_executors.streaming_runtime_impl as streaming_runtime_impl

    # The stream main-flow implementation was split out of streaming_runtime;
    # the low-level module references live in the split impl module now.
    runtime_source = inspect.getsource(streaming_runtime_impl)
    executor_source = inspect.getsource(profiled_executor)
    stream_handler_source = inspect.getsource(stream_handler)
    # The tool-event implementation was split out of stream_handler; the
    # artifact_utils reference lives in the split module now.
    stream_tool_events_source = inspect.getsource(stream_tool_events)

    assert "backend.core.tools.request_limits" in runtime_source
    assert "backend.application.agent_executors.stream_timeouts" in runtime_source
    assert "backend.application.agent_executors.image_url_registry" in runtime_source
    assert "backend.application.agent_executors.stream_handler" in runtime_source
    assert "backend.application.agent_executors.lightweight_qa_agent" in runtime_source
    assert "backend.application.artifacts.artifact_utils" in stream_tool_events_source
    assert "backend.application.agents.prompt_templates.ne" in stream_tool_events_source
    assert "backend.application.agent_executors.stream_utils" not in runtime_source
    assert "backend.application.agent_executors.stream_utils" in executor_source
    assert "backend.application.artifacts.artifact_utils" in executor_source
    assert "backend.application.tools.tool_assembly_pipeline" in inspect.getsource(ProfiledGraphBuilder._assemble_tools)
    assert "backend.custom.tools.request_limits" not in runtime_source
    assert "backend.custom.agents.streaming.stream_timeouts" not in runtime_source
    assert "backend.custom.agents.streaming.image_url_registry" not in runtime_source
    assert "backend.custom.agents.streaming.stream_utils" not in runtime_source
    assert "backend.custom.agents.streaming.stream_handler" not in runtime_source
    assert "backend.custom.agents.lightweight_qa_agent" not in runtime_source
    assert "backend.custom.agents.support.artifact_utils" not in stream_handler_source
    assert "backend.custom.agents.support.artifact_utils" not in stream_tool_events_source
    assert "backend.custom.agents.support.prompts" not in stream_handler_source
    assert "backend.custom.agents.streaming.image_url_registry" not in executor_source
    assert "backend.custom.agents.streaming.stream_utils" not in executor_source
    assert "backend.custom.agents.support.artifact_utils" not in executor_source
    assert "backend.custom.agents.support.tool_assembly" not in inspect.getsource(ProfiledGraphBuilder._assemble_tools)


def test_explore_and_follow_up_prompts_moved_to_application_resources():
    import backend.application.explore.service as explore_learning
    import backend.application.suggestions.follow_up_service as follow_up_suggestions

    explore_source = inspect.getsource(explore_learning)
    follow_up_source = inspect.getsource(follow_up_suggestions)

    assert "backend.application.explore.prompts" in explore_source
    assert "backend.application.suggestions.follow_up_prompts" in follow_up_source
    assert "backend.custom.agents.support" not in explore_source
    assert "backend.custom.agents.support" not in follow_up_source


def test_profiled_executor_graph_property_uses_owned_graph():
    graph = object()
    executor = object.__new__(ProfiledAgentExecutor)
    executor.profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    executor._graph = graph

    assert executor.graph is graph


def test_profiled_executor_rejects_unknown_runtime_arguments():
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")

    with pytest.raises(TypeError, match="unexpected_metadata"):
        ProfiledAgentExecutor(
            profile=profile,
            unexpected_metadata="must-not-cross-runtime-boundary",
        )


def test_prompt_builder_builds_domain_prompt_with_skill_enforcement():
    class FakeSkillsProvider:
        def get_available_skills_for_user(self, selected_skills):
            return []

        def build_skills_prompt_section(self, skills):
            return "<skill_system>loaded</skill_system>"

    prompt = PromptBuilder(skills_provider=FakeSkillsProvider()).build(
        PromptBuildContext(
            domain="nuclear-electronics",
            selected_skills={"nuclear-electronics", "html-ppt"},
            subagent_enabled=False,
            use_search=True,
            time_context="<runtime_current_time>2026-06-10</runtime_current_time>",
        )
    )

    assert "<skill_system>loaded</skill_system>" in prompt
    assert "read_local_skill_file(\"nuclear-electronics\")" in prompt
    assert "generate_html_ppt" in prompt
    assert "ask_clarification" not in prompt
    assert "<runtime_current_time>2026-06-10</runtime_current_time>" in prompt


def test_prompt_builder_injects_time_context_in_lightweight_mode():
    prompt = PromptBuilder().build(
        PromptBuildContext(
            domain="nuclear-electronics",
            selected_skills=set(),
            subagent_enabled=False,
            use_search=False,
            lightweight_mode=True,
            time_context="<runtime_current_time>2026-06-10</runtime_current_time>",
        )
    )

    assert "<runtime_current_time>2026-06-10</runtime_current_time>" in prompt


def test_prompt_template_registry_loads_provider_from_yaml(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        """
prompt_profiles:
  custom_ne:
    provider: backend.application.agents.prompt_templates.ne:build_system_prompt
    aliases: [custom-domain]
""",
        encoding="utf-8",
    )

    provider = PromptTemplateRegistry(path).resolve_provider(
        "custom_ne",
        fallback_key="custom-domain",
    )
    prompt = provider(
        subagent_enabled=False,
        use_search=False,
        available_skills=set(),
        task_type=None,
        lightweight_mode=False,
        lightweight_has_kb=False,
    )

    assert "核电子学" in prompt


def test_prompt_builder_uses_prompt_profile_over_domain():
    class FakeSkillsProvider:
        def get_available_skills_for_user(self, selected_skills):
            return []

        def build_skills_prompt_section(self, skills):
            return ""

    prompt = PromptBuilder(skills_provider=FakeSkillsProvider()).build(
        PromptBuildContext(
            domain="unknown-domain",
            prompt_profile="csh",
            selected_skills=set(),
            subagent_enabled=False,
            use_search=False,
        )
    )

    assert "污染场地" in prompt or "水文地质" in prompt


def test_full_feature_agents_delegate_prompt_to_platform_builder():
    builder_source = inspect.getsource(ProfiledGraphBuilder._build_system_prompt)
    prompt_builder_source = inspect.getsource(PromptBuilder._resolve_prompt_builder)

    assert "PromptBuilder" in builder_source
    assert "PromptBuildContext" in builder_source
    assert "PromptTemplateRegistry" in inspect.getsource(PromptBuilder)
    assert "backend.custom.agents.support" not in prompt_builder_source
    assert "shared_build_system_prompt" not in builder_source


def test_profiled_graph_builder_owns_graph_building():
    source = inspect.getsource(ProfiledGraphBuilder)

    assert "create_agent_graph" in source
    assert "ProfiledGraphBuildConfig" in source
    assert "PromptBuilder" in source
    assert "NEFullFeatureAgent" not in source
    assert "CSHFullFeatureAgent" not in source
    assert ProfiledGraphBuildConfig is not None


def test_profiled_executor_uses_platform_streaming_runtime():
    source = inspect.getsource(ProfiledAgentExecutor.aquery_stream)

    assert "AgentStreamRuntime" in source
    assert "StreamRuntimeConfig" in source
    assert "NEFullFeatureAgent" not in source
    assert "CSHFullFeatureAgent" not in source
    assert AgentStreamRuntime is not None
    assert StreamRuntimeConfig is not None


def test_profile_loader_reads_configured_profiles():
    loader = AgentProfileLoader()

    ne_profile = loader.load("ne")
    csh_profile = loader.load("csh")

    assert ne_profile.code == "ne"
    assert ne_profile.default_skills == ["nuclear-electronics", "general-table-analysis"]
    assert csh_profile.code == "csh"
    assert csh_profile.default_kb_ids == []


def test_profile_loader_lists_profiles_from_config():
    profiles = AgentProfileLoader().list_profiles()

    assert [profile.code for profile in profiles] == [
        "audit",
        "course_eval",
        "csh",
        "fmca",
        "groundwater",
        "landslide",
        "ne",
    ]


def test_profile_loader_skips_runtime_agent_manifest(tmp_path):
    (tmp_path / "ne.yaml").write_text(
        "\n".join(
            [
                "code: ne",
                "name: NE",
                "domain: ne",
                "default_workflow: lightweight",
                "allowed_workflows:",
                "  - lightweight",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "runtime_agents.yaml").write_text(
        "agents:\n  deep-research:\n    description: runtime role\n",
        encoding="utf-8",
    )

    profiles = AgentProfileLoader(config_dir=tmp_path).list_profiles()

    assert [profile.code for profile in profiles] == ["ne"]


def test_profile_loader_uses_yaml_as_single_source(tmp_path):
    loader = AgentProfileLoader(config_dir=tmp_path)

    assert loader.list_profiles() == []
    try:
        loader.load("ne")
    except ValueError as exc:
        assert "Unknown agent profile config" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected missing YAML profile to fail")
