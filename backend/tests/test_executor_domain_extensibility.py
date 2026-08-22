"""Contracts that keep executor behavior open to a fourth agent domain."""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from backend.application.agent_executors.graph_builder import (
    _resolve_domain_skill_name,
)
from backend.application.agent_executors.lightweight_qa_agent import (
    LightweightQAAgent,
    _build_lightweight_system_header_and_template,
    _get_lightweight_file_search_prefix,
    _get_lightweight_search_prefix,
)
from backend.application.agent_executors.profiled_executor import (
    ProfiledAgentExecutor,
)
from backend.application.agents.prompt_builder import PromptBuildContext, PromptBuilder
from backend.application.agents.prompt_registry import PromptTemplateRegistry
from backend.application.mcp.tool_catalog import (
    McpToolCatalogQuery,
    _resolve_policy_name,
)
from backend.application.tools.runtime_invoker import RuntimeToolInvoker
from backend.application.tools.tool_assembly_pipeline import ToolAssemblyPipeline
from backend.core.skills.skill_runtime_state import (
    reset_request_skill_state,
    set_request_skill_state,
)
from backend.domain.agents.profile import AgentProfile


def _virtual_profile(**overrides) -> AgentProfile:
    values = {
        "code": "materials",
        "name": "材料科学助手",
        "domain": "materials-science",
        "default_skills": ["materials-science"],
        "prompt_profile": "materials_prompt",
        "tool_policy": "materials_default",
        "guardrail_policy": "materials_scope",
        "ui_profile": "materials",
    }
    values.update(overrides)
    return AgentProfile(**values)


def _virtual_prompt_registry(tmp_path, monkeypatch) -> PromptTemplateRegistry:
    module_name = "tests_virtual_materials_prompt_provider"
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
        del subagent_enabled, use_search, task_type
        module.last_available_skills = available_skills
        return (
            "<agent_role>材料科学轻量助手</agent_role>"
            f"<lightweight>{lightweight_mode}</lightweight>"
            f"<has_kb>{lightweight_has_kb}</has_kb>"
        )

    build_system_prompt.__module__ = module_name
    module.build_system_prompt = build_system_prompt
    module.LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT = (
        "<lightweight_answer_template>材料证据优先</lightweight_answer_template>"
    )
    module.LIGHTWEIGHT_SEARCH_QUERY_PREFIX = (
        "<lightweight_search_domain>材料相变 晶体结构</lightweight_search_domain>"
    )
    module.LIGHTWEIGHT_FILE_SEARCH_PREFIX = (
        "<lightweight_file_search_domain>牌号 成分 工艺</lightweight_file_search_domain>"
    )
    monkeypatch.setitem(sys.modules, module_name, module)

    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        f"""
prompt_profiles:
  materials_prompt:
    provider: {module_name}:build_system_prompt
    aliases: [materials-science]
""",
        encoding="utf-8",
    )
    return PromptTemplateRegistry(registry_path)


def test_profiled_executor_preserves_unrecognized_domain_identity():
    executor = object.__new__(ProfiledAgentExecutor)
    executor.profile = _virtual_profile()
    executor._domain = "materials-science"

    assert executor._domain_key == "materials"
    assert executor._label == "ProfiledAgent[materials]"
    assert executor._graph_name == "materials-full-feature"
    assert "NE" not in executor._label


def test_profiled_executor_rejects_placeholder_runtime_bindings():
    executor = object.__new__(ProfiledAgentExecutor)
    executor.profile = _virtual_profile(tool_policy="default")
    executor._domain = executor.profile.domain
    executor._prompt_profile = executor.profile.prompt_profile
    executor._tool_policy = executor.profile.tool_policy
    executor._guardrail_policy = executor.profile.guardrail_policy

    with pytest.raises(ValueError, match="tool_policy"):
        executor._validate_profile_bindings()


def test_runtime_tool_invoker_never_infers_ne_policy_for_new_domain():
    explicit = SimpleNamespace(agent_profile=_virtual_profile())
    assert RuntimeToolInvoker._resolve_tool_policy(explicit) == "materials_default"

    missing = SimpleNamespace(
        agent_profile=_virtual_profile(tool_policy="default")
    )
    with pytest.raises(ValueError, match="explicit registered tool_policy"):
        RuntimeToolInvoker._resolve_tool_policy(missing)


def test_mcp_catalog_requires_explicit_policy_without_profile():
    query = McpToolCatalogQuery(domain="materials-science")

    with pytest.raises(ValueError, match="requires policy_name"):
        _resolve_policy_name(query, None)

    assert (
        _resolve_policy_name(McpToolCatalogQuery(), _virtual_profile())
        == "materials_default"
    )


def test_domain_skill_binding_uses_profile_domain_convention():
    assert (
        _resolve_domain_skill_name(
            "materials-science",
            {"materials-science", "general-table-analysis"},
        )
        == "materials-science"
    )
    assert _resolve_domain_skill_name("materials-science", set()) is None
    enforcement = PromptBuilder()._build_skill_enforcement(
        PromptBuildContext(
            domain="materials-science",
            selected_skills={"materials-science"},
            subagent_enabled=False,
            use_search=True,
        )
    )
    assert 'read_local_skill_file("materials-science")' in enforcement


def test_fourth_domain_kb_tool_enforces_its_declared_domain_skill():
    pipeline = ToolAssemblyPipeline(
        kb_ids=["kb-materials"],
        selected_skills={"materials-science"},
        domain="materials-science",
    )
    search_tool = pipeline._create_multi_kb_search_tool()

    token = set_request_skill_state({"materials-science"})
    try:
        result = asyncio.run(search_tool.ainvoke({"query": "奥氏体相变"}))
    finally:
        reset_request_skill_state(token)

    payload = json.loads(result)
    assert payload["error"] == "skill_not_loaded"
    assert payload["required_skill"] == "materials-science"
    assert 'read_local_skill_file("materials-science")' in payload["message"]


def test_fourth_domain_lightweight_uses_registered_provider_resources(
    tmp_path,
    monkeypatch,
):
    registry = _virtual_prompt_registry(tmp_path, monkeypatch)

    header, answer_template = _build_lightweight_system_header_and_template(
        prompt_profile="materials_prompt",
        domain="materials-science",
        use_search=True,
        lightweight_has_kb=True,
        available_skills={"materials-science"},
        prompt_registry=registry,
    )
    search_prefix = _get_lightweight_search_prefix(
        prompt_profile="materials_prompt",
        domain="materials-science",
        prompt_registry=registry,
    )
    file_prefix = _get_lightweight_file_search_prefix(
        prompt_profile="materials_prompt",
        domain="materials-science",
        prompt_registry=registry,
    )

    assert "材料科学轻量助手" in header
    assert "<has_kb>True</has_kb>" in header
    assert "材料证据优先" in answer_template
    assert "材料相变" in search_prefix
    assert "牌号" in file_prefix
    assert sys.modules[
        "tests_virtual_materials_prompt_provider"
    ].last_available_skills == {"materials-science"}
    assert "核电子学" not in "".join(
        (header, answer_template, search_prefix, file_prefix)
    )

    agent = LightweightQAAgent(
        graph=object(),
        model_name=None,
        kb_ids=[],
        use_search=False,
        prompt_domain="materials",
        prompt_profile="materials_prompt",
        domain="materials-science",
        prompt_registry=registry,
        selected_skills={"materials-science"},
    )
    assert agent._prompt_profile == "materials_prompt"
    assert agent._prompt_domain == "materials-science"
    assert "材料科学轻量助手" in agent._lightweight_system_header


def test_unknown_lightweight_prompt_profile_fails_fast(tmp_path, monkeypatch):
    registry = _virtual_prompt_registry(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="Unknown prompt profile"):
        _get_lightweight_search_prefix(
            prompt_profile="not-registered",
            domain="materials-science",
            prompt_registry=registry,
        )

    with pytest.raises(ValueError, match="explicit registered prompt_profile"):
        LightweightQAAgent(
            graph=object(),
            model_name=None,
            kb_ids=[],
            use_search=False,
        )


def test_explicit_unknown_full_prompt_profile_does_not_fallback_to_domain(
    tmp_path,
    monkeypatch,
):
    registry = _virtual_prompt_registry(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="Unknown prompt profile"):
        PromptBuilder(prompt_registry=registry)._resolve_prompt_builder(
            "misspelled-profile",
            fallback_key="materials-science",
        )
