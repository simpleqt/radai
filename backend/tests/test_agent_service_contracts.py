"""Contract tests for AgentService executor building and config registries.

覆盖三块此前无直接护栏的契约:
- ``AgentService.build_executor_async``(会话回滚的唯一构建入口);
- ``WorkflowSpec`` 注册表加载规则(别名/能力/执行策略校验);
- 进程级配置缓存(AgentProfileLoader / RuntimeAgentLoader 跨实例复用,
  且缓存命中返回的对象可被调用方安全修改)。
"""

from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from backend.application.agent_service import AgentService
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.agents.runtime_agent_loader import RuntimeAgentLoader
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.workflows import (
    FULL_WORKFLOW,
    _load_workflow_specs,
    normalize_workflow_name,
)


class _FakeGraphBuilder:
    """Skip model creation and LangGraph compilation inside executor tests."""

    last_graph = None

    def __init__(self, config):
        self._config = config

    def build(self):
        graph = object()
        _FakeGraphBuilder.last_graph = graph
        return SimpleNamespace(
            graph=graph,
            model_name="test-model",
            tools=[],
            compaction_checkpoints=None,
            context_pruning_middleware=None,
        )


class _NoDatabaseKbAccess:
    """KB binding lookup stub: the test environment has no MySQL."""

    def resolve_allowed_kb_ids(self, *, user_id, requested_ids, profile):
        return list(requested_ids or [])


def _agent_service_without_database() -> AgentService:
    from backend.application.policies import ProfileRuntimePolicy

    return AgentService(
        runtime_policy=ProfileRuntimePolicy(
            kb_access_policy=_NoDatabaseKbAccess(),
        ),
    )


async def test_build_executor_async_returns_executor_for_full_workflow(monkeypatch):
    monkeypatch.setattr(
        "backend.application.agent_executors.profiled_executor.ProfiledGraphBuilder",
        _FakeGraphBuilder,
    )
    service = _agent_service_without_database()
    request = AgentRequest(
        question="hello",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="full",
        # 测试环境没有托管模型目录(MySQL);显式模型名跳过 DB 解析。
        model_name="test-model",
    )

    executor = await service.build_executor_async(request)

    assert executor is not None
    assert executor.graph is _FakeGraphBuilder.last_graph
    assert executor.profile.code == "ne"


async def test_build_executor_async_rejects_workflow_without_executor(monkeypatch):
    service = _agent_service_without_database()
    monkeypatch.setattr(
        service._workflow_registry,
        "resolve",
        lambda *args, **kwargs: object(),
    )
    request = AgentRequest(
        question="hello",
        agent_code="ne",
        user_id="user-1",
        model_name="test-model",
    )

    with pytest.raises(ValueError, match="does not expose an executor"):
        await service.build_executor_async(request)


def test_workflow_specs_normalize_aliases_and_capabilities(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        textwrap.dedent(
            """
            workflows:
              custom_flow:
                provider: pkg.mod:create_flow
                aliases:
                  - Custom-Flow
                full_capability: true
              plain_flow:
                provider: pkg.mod:create_plain
            """
        ),
        encoding="utf-8",
    )

    specs = _load_workflow_specs(path)

    custom = specs["custom_flow"]
    assert "custom_flow" in custom.aliases
    assert "custom-flow" in {alias.lower() for alias in custom.aliases}
    assert FULL_WORKFLOW in custom.capabilities
    assert custom.execution_strategy == "graph"
    plain = specs["plain_flow"]
    assert plain.capabilities == {"plain_flow"}
    assert plain.full_capability is False


def test_workflow_specs_reject_invalid_execution_strategy(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        textwrap.dedent(
            """
            workflows:
              broken_flow:
                provider: pkg.mod:create_flow
                execution_strategy: quantum
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid execution_strategy"):
        _load_workflow_specs(path)


def test_workflow_specs_reject_empty_registry(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text("workflows: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no valid workflows"):
        _load_workflow_specs(path)


def test_builtin_workflow_alias_normalization():
    assert normalize_workflow_name("deep-research") == "deep_research"
    assert normalize_workflow_name("  FULL ") == "full"


def _write_profile(config_dir, code: str, *, skills: list[str]) -> None:
    lines = [
        f"code: {code}",
        "name: Cache Probe",
        "domain: cache-probe",
        "default_workflow: full",
        "default_skills:",
        *[f"  - {item}" for item in skills],
    ]
    config_dir.joinpath(f"{code}.yaml").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def test_profile_loader_shares_parse_across_instances(tmp_path, monkeypatch):
    config_dir = tmp_path / "agents"
    config_dir.mkdir()
    _write_profile(config_dir, "probe", skills=["skill-a"])

    import backend.application.agents.profile_loader as profile_loader_module

    real_yaml = profile_loader_module.yaml
    parse_calls = {"count": 0}

    def counting_safe_load(stream):
        parse_calls["count"] += 1
        return real_yaml.safe_load(stream)

    monkeypatch.setattr(
        profile_loader_module,
        "yaml",
        SimpleNamespace(safe_load=counting_safe_load),
    )

    first = AgentProfileLoader(config_dir).load("probe")
    second = AgentProfileLoader(config_dir).load("probe")

    assert parse_calls["count"] == 1
    assert first is not second
    assert first.default_skills == second.default_skills

    # 缓存命中返回的可变字段必须相互隔离,调用方就地修改不得污染缓存。
    first.default_skills.append("mutated")
    third = AgentProfileLoader(config_dir).load("probe")
    assert third.default_skills == ["skill-a"]


def test_profile_loader_picks_up_file_changes(tmp_path):
    config_dir = tmp_path / "agents"
    config_dir.mkdir()
    _write_profile(config_dir, "probe", skills=["skill-a"])
    loader = AgentProfileLoader(config_dir)
    assert loader.load("probe").default_skills == ["skill-a"]

    _write_profile(config_dir, "probe", skills=["skill-a", "skill-b-changed"])

    assert AgentProfileLoader(config_dir).load("probe").default_skills == [
        "skill-a",
        "skill-b-changed",
    ]


_RUNTIME_AGENTS_YAML = textwrap.dedent(
    """
    agents:
      planner:
        description: test planner
        tool_access: false
        can_use_network: false
        can_read_kb: false
        max_turns: 2
        timeout_seconds: 30
        model: fast
        output_schema: ResearchTaskPlan
        allowed_tools: []
        required_tools: []
        disallowed_tools: []
        system_prompt: plan things
    """
)


def test_runtime_agent_loader_shares_parse_across_instances(tmp_path, monkeypatch):
    config_path = tmp_path / "runtime_agents.yaml"
    config_path.write_text(_RUNTIME_AGENTS_YAML, encoding="utf-8")

    import backend.application.agents.runtime_agent_loader as loader_module

    real_yaml = loader_module.yaml
    parse_calls = {"count": 0}

    def counting_safe_load(stream):
        parse_calls["count"] += 1
        return real_yaml.safe_load(stream)

    monkeypatch.setattr(
        loader_module,
        "yaml",
        SimpleNamespace(safe_load=counting_safe_load),
    )

    first = RuntimeAgentLoader(config_path).load_all()
    second = RuntimeAgentLoader(config_path).load_all()

    assert parse_calls["count"] == 1
    assert set(first) == {"planner"}
    assert set(second) == {"planner"}

    # invalidate_cache 保持既有语义:强制下一次重新解析。
    RuntimeAgentLoader(config_path).invalidate_cache()
    RuntimeAgentLoader(config_path).load_all()
    assert parse_calls["count"] == 2
