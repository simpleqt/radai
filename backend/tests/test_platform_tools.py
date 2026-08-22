"""工具注册/策略装配契约（拆自 test_agent_platform_standardization.py）。"""

import inspect
import pytest

from backend.application.agent_executors import (
    ProfiledAgentExecutor,
    ProfiledGraphBuilder,
)
from backend.application.agent_executors.streaming_runtime import AgentStreamRuntime
from backend.application.tools.assembler import (
    PolicyToolAssembler,
    ToolAssemblyContext,
    ToolPolicyAudit,
)
from backend.application.tools.provider_loader import ToolProviderLoader
from backend.application.tools.registry import ToolRegistry
from backend.application.tools.tool_assembly_pipeline import ToolAssemblyPipeline
from backend.core.agents.middlewares.dangling_tool_call import (
    DanglingToolCallMiddleware,
)
from backend.core.skills.native_skill_tools import normalize_skill_tool_name
from backend.domain.agents.profile import AgentProfile
from backend.domain.tools.spec import ToolSpec
from langchain_core.messages import AIMessage
from langchain_core.tools import tool


def test_csh_managed_retrieval_provider_budget_matches_prompt_limit():
    executor = object.__new__(ProfiledAgentExecutor)
    executor.profile = AgentProfile(code="csh", name="CSH", domain="csh-hydrogeology")
    executor._domain = "csh-hydrogeology"

    assert executor._default_multi_kb_search_max_calls() == 2


def test_ask_clarification_removed_from_agent_tooling():
    from backend.core.agents.features import RuntimeFeatures
    from backend.core.prompts.system_template import apply_prompt_template

    factory_source = inspect.getsource(__import__("backend.core.agents.factory", fromlist=[""]))
    graph_builder_source = inspect.getsource(ProfiledGraphBuilder.build)
    prompt = apply_prompt_template()
    runtime_source = inspect.getsource(AgentStreamRuntime)
    task_tool_source = inspect.getsource(__import__("backend.core.tools.task_tool", fromlist=[""]))

    assert RuntimeFeatures().clarification is False
    assert "clarification=False" in graph_builder_source
    assert "ask_clarification" not in prompt
    assert "ask_clarification_tool" not in factory_source
    assert "_is_clarification_pending" not in runtime_source
    assert "\"ask_clarification\"" not in task_tool_source


def test_skill_tool_name_normalization_handles_malformed_model_arguments():
    assert normalize_skill_tool_name("html-ppt</parameter") == "html-ppt"
    assert normalize_skill_tool_name('"html-ppt"') == "html-ppt"
    assert normalize_skill_tool_name("`csh-hydrogeology`") == "csh-hydrogeology"
    assert normalize_skill_tool_name(" general-table-analysis ") == "general-table-analysis"


def test_dangling_tool_call_middleware_sanitizes_skill_tool_args_before_model_call():
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-1",
                "name": "read_local_skill_file",
                "args": {"skill_name": "nuclear-electronics</parameter"},
            }
        ],
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read_local_skill_file",
                        "arguments": '{"skill_name":"nuclear-electronics</parameter',
                    },
                }
            ]
        },
    )

    patched = DanglingToolCallMiddleware()._build_patched_messages([message])

    assert patched is not None
    assert patched[0].tool_calls[0]["args"] == {"skill_name": "nuclear-electronics"}
    assert "tool_calls" not in patched[0].additional_kwargs


def test_dangling_tool_call_middleware_sanitizes_html_ppt_template_args():
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-1",
                "name": "generate_html_ppt",
                "args": {
                    "template": "tech-sharing</parameter",
                    "theme": "tokyo-night</parameter",
                    "title": "demo",
                },
            }
        ],
    )

    patched = DanglingToolCallMiddleware()._build_patched_messages([message])

    assert patched is not None
    assert patched[0].tool_calls[0]["args"]["template"] == "tech-sharing"
    assert patched[0].tool_calls[0]["args"]["theme"] == "tokyo-night"


def test_dangling_tool_call_middleware_drops_invalid_tool_calls_from_history():
    from backend.core.models.patched_deepseek_openai import PatchedDeepSeekOpenAI

    message = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "id": "call-invalid",
                "name": "multi_kb_search",
                "args": (
                    '{"query":"monitoring well layout</parameter",'
                    '"top_k":10</parameter}'
                ),
                "error": None,
            }
        ],
    )

    patched = DanglingToolCallMiddleware()._build_patched_messages([message])

    assert patched is not None
    assert patched[0].invalid_tool_calls == []

    model = PatchedDeepSeekOpenAI(
        model="test-model",
        api_key="test-key",
        base_url="http://example.invalid/v1",
    )
    payload = model._get_request_payload(patched)
    assert "tool_calls" not in payload["messages"][0]


def test_tool_registry_loads_specs_and_policies():
    registry = ToolRegistry()

    multi_kb = registry.get("multi_kb_search")
    ne_policy = registry.get_policy("ne_default")
    fmca_policy = registry.get_policy("fmca_default")

    assert multi_kb.risk_level == "safe"
    assert "multi_kb_search" in ne_policy.include
    assert "multi_kb_search" in fmca_policy.include
    assert "multi_kb_search" not in fmca_policy.exclude


def test_tool_registry_resolves_skill_conditioned_tools():
    registry = ToolRegistry()

    tools = registry.resolve_policy_tools(
        policy_name="ne_default",
        domain="nuclear-electronics",
        workflow="full",
        selected_skills={"general-table-analysis"},
    )

    names = {tool.name for tool in tools}
    assert "multi_kb_search" in names
    assert "inspect_excel_data" in names
    assert "academic_search" not in names


def test_fmca_tool_policy_allows_bound_kb_search():
    registry = ToolRegistry()

    tools = registry.resolve_policy_tools(
        policy_name="fmca_default",
        domain="fundamental-mechanics-course",
        workflow="lightweight",
        selected_skills={"general-table-analysis"},
    )

    names = {tool.name for tool in tools}
    assert "multi_kb_search" in names
    assert "inspect_excel_data" in names


def test_tool_policy_references_only_declared_tools():
    registry = ToolRegistry()
    declared = {spec.name for spec in registry.list_specs()}

    for policy_name in ("ne_default", "csh_default", "fmca_default"):
        policy = registry.get_policy(policy_name)
        referenced = set(policy.include) | set(policy.exclude)
        for tool_names in policy.include_when_skill_selected.values():
            referenced.update(tool_names)
        assert referenced <= declared


def test_all_declared_tool_providers_are_importable():
    registry = ToolRegistry()
    loader = ToolProviderLoader()

    for spec in registry.list_specs():
        provider = loader.load(spec)
        assert provider is not None, spec.name


def test_tool_registry_resolves_excel_experiment_skill_tools():
    registry = ToolRegistry()

    tools = registry.resolve_policy_tools(
        policy_name="ne_default",
        domain="nuclear-electronics",
        workflow="full",
        selected_skills={"excel-experiment-analysis"},
    )

    names = {tool.name for tool in tools}
    assert "list_experiment_datasets" in names
    assert "inspect_excel_data" in names


def test_tool_provider_loader_imports_declared_provider():
    registry = ToolRegistry()
    provider = ToolProviderLoader().load(registry.get("generate_html_ppt"))

    assert provider.name == "generate_html_ppt"


def test_tool_provider_contract_rejects_invoke_shaped_non_tool():
    class InvokeOnlyObject:
        def invoke(self, _value):
            return "not a LangChain tool"

    with pytest.raises(TypeError, match="neither a callable factory nor a tool"):
        ToolProviderLoader.validate_provider_contract(InvokeOnlyObject())


def test_policy_tool_assembler_materializes_tool_factory_results():
    assembler = PolicyToolAssembler()

    tools = assembler.materialize_tool_name("inspect_excel_data")

    assert {tool.name for tool in tools} >= {
        "inspect_excel_data",
        "query_excel_data",
        "summarize_excel_data",
        "visualize_excel_data",
    }


def test_policy_tool_assembler_audits_expected_tool_set():
    context = ToolAssemblyContext(
        domain="nuclear-electronics",
        workflow="full",
        policy_name="ne_default",
        selected_skills={"html-ppt"},
    )

    audit = PolicyToolAssembler().audit(
        context,
        actual_tool_names={"multi_kb_search", "extra_tool"},
    )

    assert audit.expected >= {"multi_kb_search", "chat_file_search", "generate_html_ppt"}
    assert audit.present == {"multi_kb_search"}
    assert audit.missing >= {"chat_file_search", "generate_html_ppt"}
    assert audit.unexpected == {"extra_tool"}


def test_tool_assembly_loads_declared_tools_via_registry():
    pipeline = ToolAssemblyPipeline()

    html_tool = pipeline._load_declared_skill_tool("generate_html_ppt")
    excel_tools = pipeline._load_declared_skill_tool("inspect_excel_data")

    assert html_tool.name == "generate_html_ppt"
    assert {tool.name for tool in excel_tools} >= {
        "inspect_excel_data",
        "query_excel_data",
        "summarize_excel_data",
        "visualize_excel_data",
    }


def test_tool_assembly_resolves_policy_specs_without_mutating_tools():
    pipeline = ToolAssemblyPipeline(
        domain="nuclear-electronics",
        workflow_name="full",
        tool_policy="ne_default",
        selected_skills={"html-ppt", "deep-research"},
    )

    specs = pipeline._resolve_policy_tool_specs()

    names = {spec.name for spec in specs}
    assert "multi_kb_search" in names
    assert "chat_file_search" in names
    assert "generate_html_ppt" in names
    assert "save_deep_research_report" not in names
    assert "academic_search" not in names


def test_tool_assembly_applies_registry_metadata_to_dynamic_tools():
    pipeline = ToolAssemblyPipeline(
        domain="nuclear-electronics",
        workflow_name="full",
        tool_policy="ne_default",
        selected_skills=set(),
        use_search=False,
        kb_ids=["kb-1"],
    )

    tools = {tool_item.name: tool_item for tool_item in pipeline.assemble()}

    assert tools["multi_kb_search"].metadata["risk_level"] == "safe"
    assert tools["multi_kb_search"].metadata["policy_source"] == "registry:multi_kb_search"
    assert tools["read_context_artifact"].metadata["timeout_seconds"] == 30.0
    assert tools["context_read"].metadata["timeout_seconds"] == 30.0
    assert tools["read_context_artifact"].metadata["side_effect"] == "none"


def test_tool_assembly_enforces_policy_as_only_business_tool_source():
    @tool("policy_allowed_tool")
    def policy_allowed_tool() -> str:
        """A policy allowed test tool."""
        return "ok"

    class FakeAssembler:
        def resolve_specs(self, context):
            return [
                ToolSpec(
                    name="policy_allowed_tool",
                    provider="tests:policy_allowed_tool",
                    domains={context.domain},
                    workflows={context.workflow},
                )
            ]

        def materialize_policy_tools(self, context, *, skip_tool_names=None):
            return {policy_allowed_tool.name: policy_allowed_tool}

        def audit(self, context, *, actual_tool_names):
            expected = {"policy_allowed_tool"}
            return ToolPolicyAudit(
                policy_name=context.policy_name,
                domain=context.domain,
                workflow=context.workflow,
                expected=expected,
                present=expected & actual_tool_names,
                missing=expected - actual_tool_names,
                unexpected=actual_tool_names - expected,
            )

    pipeline = ToolAssemblyPipeline(
        domain="nuclear-electronics",
        workflow_name="full",
        tool_policy="ne_default",
        selected_skills=set(),
        policy_tool_assembler=FakeAssembler(),
    )

    names = {tool_item.name for tool_item in pipeline.assemble()}

    assert names == {"policy_allowed_tool"}


def test_tool_assembly_resolves_deep_research_policy_specs():
    pipeline = ToolAssemblyPipeline(
        domain="nuclear-electronics",
        workflow_name="deep_research",
        tool_policy="ne_default",
        selected_skills={"deep-research"},
    )

    names = {spec.name for spec in pipeline._resolve_policy_tool_specs()}

    assert "multi_kb_search" in names
    assert "save_deep_research_report" in names


def test_tool_assembly_resolves_csh_policy_specs():
    pipeline = ToolAssemblyPipeline(
        domain="csh-hydrogeology",
        workflow_name="full",
        tool_policy="csh_default",
        selected_skills={"general-table-analysis"},
    )

    names = {spec.name for spec in pipeline._resolve_policy_tool_specs()}

    assert "multi_kb_search" in names
    assert "inspect_excel_data" in names
    assert "academic_search" not in names


def test_tool_assembly_materializes_only_policy_allowed_excel_tools():
    pipeline = ToolAssemblyPipeline(
        domain="nuclear-electronics",
        workflow_name="full",
        tool_policy="ne_default",
        selected_skills={"general-table-analysis"},
        use_search=False,
        kb_ids=[],
    )

    names = {tool_item.name for tool_item in pipeline.assemble()}

    assert {"inspect_excel_data", "query_excel_data", "summarize_excel_data", "visualize_excel_data"} <= names
    assert "list_experiment_datasets" not in names
    assert "excel_data_analysis" not in names


def test_lightweight_default_table_skill_assembles_policy_scoped_excel_tools():
    pipeline = ToolAssemblyPipeline(
        domain="nuclear-electronics",
        workflow_name="lightweight",
        tool_policy="ne_default",
        selected_skills={"nuclear-electronics", "general-table-analysis"},
        lightweight_mode=True,
        use_search=False,
        kb_ids=[],
    )

    names = {tool_item.name for tool_item in pipeline.assemble()}

    assert {
        "inspect_excel_data",
        "query_excel_data",
        "summarize_excel_data",
        "visualize_excel_data",
    } <= names
    assert "list_experiment_datasets" not in names
