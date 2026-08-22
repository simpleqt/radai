from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from backend.application.agent_executors.graph_builder import (
    ProfiledGraphBuildConfig,
    ProfiledGraphBuilder,
)
from backend.application.agents.runtime_agent_loader import RuntimeAgentLoader
from backend.core.agents.middlewares.model_turn_limit import (
    ModelTurnLimitExceeded,
    ModelTurnLimitMiddleware,
)
from backend.domain.agents.runtime_agent import RuntimeAgentSpec
from backend.domain.agents.runtime_budget import AgentRuntimeBudget


def test_runtime_agent_loader_reads_default_manifest():
    specs = RuntimeAgentLoader().load_all()

    assert set(specs) >= {
        "planner",
        "domain_analyst",
        "data_analyst",
        "evidence_researcher",
        "risk_reviewer",
        "solution_architect",
        "synthesizer",
        "analyst",
        "critic",
        "finalizer",
        "report_writer",
        "reviewer",
        "deep-research",
        "general-purpose",
    }
    planner = specs["planner"]
    assert planner.tool_access is False
    assert planner.can_use_network is False
    assert planner.can_read_kb is False
    assert planner.output_schema == "ResearchTaskPlan"

    data_analyst = specs["data_analyst"]
    assert data_analyst.tool_access is True
    assert data_analyst.can_use_network is True
    assert data_analyst.can_read_kb is True
    assert "query_project_tables" in data_analyst.allowed_tools
    assert {
        "list_experiment_datasets",
        "inspect_excel_data",
        "query_excel_data",
        "summarize_excel_data",
        "visualize_excel_data",
        "groundwater_project_skill_read",
        "groundwater_project_bootstrap",
        "inspect_project_tables",
        "groundwater_quality_check",
    }.issubset(data_analyst.allowed_tools)

    skill_control_tools = {
        "read_local_skill_file",
        "read_context_artifact",
    }
    for role in {
        "domain_analyst",
        "data_analyst",
        "evidence_researcher",
        "risk_reviewer",
        "solution_architect",
    }:
        assert skill_control_tools.issubset(specs[role].allowed_tools)
    assert specs["domain_analyst"].max_turns == 10
    assert specs["domain_analyst"].timeout_seconds == 300
    assert specs["risk_reviewer"].max_turns == 8
    assert specs["risk_reviewer"].timeout_seconds == 300
    assert specs["solution_architect"].max_turns == 8
    assert specs["solution_architect"].timeout_seconds == 300

    writer = specs["report_writer"]
    assert writer.tool_access is False
    assert writer.output_schema == "MarkdownReport"

    for role in {"synthesizer", "critic", "finalizer"}:
        assert specs[role].tool_access is False
        assert specs[role].allowed_tools == set()
        assert specs[role].can_use_network is False
        assert specs[role].can_read_kb is False

    deep_research = specs["deep-research"]
    assert deep_research.max_turns == 12
    assert deep_research.timeout_seconds == 300
    assert deep_research.required_tools == set()
    assert deep_research.allowed_tools == {
        "multi_kb_search",
        "web_search",
        "read_local_skill_file",
        "chat_file_search",
        "read_context_artifact",
        "context_read",
    }
    assert {"task", "save_deep_research_report"}.issubset(
        deep_research.disallowed_tools
    )

    general = specs["general-purpose"]
    assert general.allowed_tools is not None
    assert {
        "multi_kb_search",
        "web_search",
        "query_project_tables",
        "query_excel_data",
        "groundwater_prediction",
    }.issubset(general.allowed_tools)
    assert {
        "visualize_excel_data",
        "generate_html_ppt",
        "save_deep_research_report",
    }.isdisjoint(general.allowed_tools)
    assert {"task", "present_files"}.issubset(general.disallowed_tools)


def test_runtime_agent_contract_is_appended_at_system_prompt_priority():
    spec = RuntimeAgentSpec(
        name="reviewer",
        description="review",
        system_prompt="只依据分支交接审查答案。",
        allowed_tools=set(),
        disallowed_tools={"web_search"},
        tool_access=False,
        output_schema="MarkdownAnswer",
        can_use_network=False,
        can_read_kb=False,
    )
    builder = ProfiledGraphBuilder(
        ProfiledGraphBuildConfig(
            label="test",
            graph_name="test",
            domain="nuclear-electronics",
            tool_policy="ne_default",
            workflow_name="full",
            model_name=None,
            thinking_enabled=False,
            subagent_enabled=False,
            use_search=False,
            kb_ids=[],
            selected_skills=set(),
            skills_provider=SimpleNamespace(),
            task_type=None,
            lightweight_mode=False,
            lightweight_has_kb=False,
            thread_id="thread-1",
            external_thread_id="thread-1",
            max_concurrent_subagents=1,
            checkpointer=None,
            runtime_agent_spec=spec,
        )
    )

    prompt = builder._append_runtime_agent_contract("top-level prompt")

    assert prompt.startswith("top-level prompt")
    assert '<runtime_agent_contract>' in prompt
    assert "角色名称：reviewer" in prompt
    assert "只依据分支交接审查答案。" in prompt
    assert "当前实际可用工具：无" in prompt
    assert "期望输出结构：MarkdownAnswer" in prompt


def test_runtime_agent_contract_lists_only_materialized_tools():
    spec = RuntimeAgentSpec(
        name="evidence_researcher",
        description="research",
        system_prompt="核验来源。",
        allowed_tools={"multi_kb_search", "web_search"},
        required_tools={"multi_kb_search"},
    )
    builder = object.__new__(ProfiledGraphBuilder)
    builder._config = SimpleNamespace(runtime_agent_spec=spec)

    prompt = builder._append_runtime_agent_contract(
        "base",
        available_tool_names={"web_search"},
    )

    assert "当前实际可用工具：web_search" in prompt
    assert "当前缺失的必须工具：multi_kb_search" in prompt


def test_runtime_agent_contract_forbids_using_web_url_as_artifact_ref():
    spec = RuntimeAgentSpec(
        name="domain_analyst",
        description="analysis",
        system_prompt="分析证据。",
        allowed_tools={"web_search", "read_context_artifact"},
    )
    builder = object.__new__(ProfiledGraphBuilder)
    builder._config = SimpleNamespace(runtime_agent_spec=spec)

    prompt = builder._append_runtime_agent_contract(
        "base",
        available_tool_names={"web_search", "read_context_artifact"},
    )

    assert "artifact://context/..." in prompt
    assert "网页 URL" in prompt
    assert "必须为最终总结预留最后一个模型轮次" in prompt


def test_runtime_agent_model_turn_limit_is_semantic_and_fail_closed():
    middleware = ModelTurnLimitMiddleware(2)

    assert middleware._before_model({}) == {"runtime_model_turns": 1}
    assert middleware._before_model({"runtime_model_turns": 1}) == {
        "runtime_model_turns": 1
    }
    with pytest.raises(ModelTurnLimitExceeded, match=r"turn limit \(2\)"):
        middleware._before_model({"runtime_model_turns": 2})


def test_runtime_agent_last_admitted_turn_forces_tool_free_finalization():
    middleware = ModelTurnLimitMiddleware(2)
    messages = [HumanMessage(content="核验并总结")]
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=messages,
        system_message=SystemMessage(content="system"),
        tools=[{"type": "function", "function": {"name": "web_search"}}],
        tool_choice="auto",
        state={"messages": messages, "runtime_model_turns": 2},
        runtime=None,
    )
    captured = []

    response = middleware.wrap_model_call(
        request,
        lambda patched: captured.append(patched)
        or ModelResponse(result=[AIMessage(content="final answer")]),
    )

    assert response.result[0].content == "final answer"
    assert captured[0].tools == []
    assert captured[0].tool_choice is None
    assert "final admitted model turn" in captured[0].system_message.content


@pytest.mark.asyncio
async def test_runtime_agent_last_admitted_turn_forces_tool_free_finalization_async():
    middleware = ModelTurnLimitMiddleware(2)
    messages = [HumanMessage(content="核验并总结")]
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=messages,
        system_message=SystemMessage(content="system"),
        tools=[{"type": "function", "function": {"name": "web_search"}}],
        tool_choice="auto",
        state={"messages": messages, "runtime_model_turns": 2},
        runtime=None,
    )
    captured = []

    async def handler(patched: ModelRequest) -> ModelResponse:
        captured.append(patched)
        return ModelResponse(result=[AIMessage(content="async final answer")])

    response = await middleware.awrap_model_call(request, handler)

    assert response.result[0].content == "async final answer"
    assert captured[0].tools == []
    assert captured[0].tool_choice is None
    assert "final admitted model turn" in captured[0].system_message.content


def test_runtime_agent_loader_rejects_missing_required_fields(tmp_path: Path):
    config_path = tmp_path / "runtime_agents.yaml"
    config_path.write_text(
        """
agents:
  broken:
    description: Missing prompt
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required field: system_prompt"):
        RuntimeAgentLoader(config_path).load_all()


def test_runtime_agent_loader_validates_required_tools_are_allowed(tmp_path: Path):
    config_path = tmp_path / "runtime_agents.yaml"
    config_path.write_text(
        """
agents:
  broken:
    description: Bad tool contract
    system_prompt: prompt
    allowed_tools:
      - multi_kb_search
    required_tools:
      - query_project_tables
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="required_tools must be included"):
        RuntimeAgentLoader(config_path).load_all()


def test_runtime_agent_loader_rejects_required_and_disallowed_tool_overlap(
    tmp_path: Path,
):
    config_path = tmp_path / "runtime_agents.yaml"
    config_path.write_text(
        """
agents:
  broken:
    description: Contradictory tool contract
    system_prompt: prompt
    required_tools:
      - multi_kb_search
    disallowed_tools:
      - multi_kb_search
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="required_tools cannot be disallowed"):
        RuntimeAgentLoader(config_path).load_all()


@pytest.mark.parametrize(
    ("definition", "error"),
    [
        ("not-a-mapping", "definition must be a mapping"),
        (
            """\n    description: Bad tool list\n    system_prompt: prompt\n    allowed_tools: web_search""",
            "allowed_tools must be a list of strings",
        ),
        (
            """\n    description: Bad boolean\n    system_prompt: prompt\n    tool_access: maybe""",
            "tool_access must be a boolean",
        ),
        (
            """\n    description: Typo\n    system_prompt: prompt\n    max_turn: 3""",
            "unknown fields: max_turn",
        ),
        (
            """\n    description: Bad schema\n    system_prompt: prompt\n    output_schema: ResearchPlanTypo""",
            "output_schema is unknown: ResearchPlanTypo",
        ),
    ],
)
def test_runtime_agent_loader_rejects_malformed_manifest_fields(
    tmp_path: Path, definition: str, error: str
):
    config_path = tmp_path / "runtime_agents.yaml"
    config_path.write_text(
        f"agents:\n  broken: {definition}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=error):
        RuntimeAgentLoader(config_path).load_all()


def test_runtime_agent_loader_rejects_duplicate_canonical_names(tmp_path: Path):
    config_path = tmp_path / "runtime_agents.yaml"
    config_path.write_text(
        """
agents:
  first:
    name: shared
    description: First
    system_prompt: prompt
  second:
    name: shared
    description: Second
    system_prompt: prompt
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate canonical name: shared"):
        RuntimeAgentLoader(config_path).load_all()


def test_runtime_agent_loader_reports_unknown_agent():
    loader = RuntimeAgentLoader()

    with pytest.raises(ValueError, match="Unknown runtime agent"):
        loader.get("missing-agent")


def test_agent_runtime_budget_reads_and_clamps_env(monkeypatch):
    monkeypatch.setenv("COORDINATED_RESEARCH_MAX_CONCURRENT", "99")
    monkeypatch.setenv("PLAN_SOLVE_MAX_CONCURRENT_SUBAGENTS", "0")
    monkeypatch.setenv("DEEP_RESEARCH_UNIT_IDLE_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("DEEP_RESEARCH_PARTIAL_HANDOFF_GRACE_SECONDS", "0.5")

    budget = AgentRuntimeBudget.from_env()

    assert budget.max_concurrent_research == 5
    assert budget.plan_solve_max_concurrent_subagents == 1
    assert budget.unit_idle_timeout_seconds == 10
    assert budget.partial_handoff_grace_seconds == 0.5
