"""地下水智能体运行时门禁与项目上下文契约（拆自 test_groundwater_project_runtime.py）。"""

import json
import os
import pytest

from backend.application.agent_service import AgentService
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.domain_context import DomainContextResolver
from backend.application.groundwater import (
    GroundwaterProjectService,
    runtime_context as groundwater_runtime_context,
)
from backend.application.policies.knowledge_base_access import (
    KnowledgeBaseAccessPolicy,
    KnowledgeBaseRecord,
)
from backend.application.policies.profile_runtime_policy import ProfileRuntimePolicy
from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.application.tools.assembler import PolicyToolAssembler, ToolAssemblyContext
from backend.application.tools.providers.excel_engine import is_allowed_local_table_path
from backend.application.tools.providers.groundwater_project_tools import (
    _bootstrap_payload,
)
from backend.core.agents.middlewares.context_pruning import ContextPruningMiddleware
from backend.core.agents.middlewares.groundwater_project_workflow import (
    GroundwaterProjectWorkflowMiddleware,
)
from backend.core.agents.middlewares.skill_load_guard import SkillLoadGuardMiddleware
from backend.core.skills.skill_runtime_state import (
    mark_skill_loaded,
    reset_request_skill_state,
    set_request_skill_state,
)
from backend.domain.agents.request import AgentRequest
from backend.domain.tools.runtime_context import ToolRuntimeContext
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pathlib import Path
from types import SimpleNamespace


STANDARD_KB_ID = "7e2975ec-8cf4-46c8-b782-0413d64c8a58"
_BUILTIN_DATASET_ROOT = Path(
    os.getenv("GROUNDWATER_BUILTIN_ROOT", "")
    or Path(__file__).resolve().parents[2] / "六塘项目数据信息" / "standardized"
)


requires_builtin_groundwater_dataset = pytest.mark.skipif(
    not (_BUILTIN_DATASET_ROOT / "manifest.json").is_file(),
    reason=(
        "requires the external Liutang groundwater dataset; set "
        "GROUNDWATER_BUILTIN_ROOT to run dataset integration tests"
    ),
)


def _runtime_context(project_id: str = "liutang-groundwater") -> ToolRuntimeContext:
    return ToolRuntimeContext(
        request_id="req-groundwater",
        user_id="user-groundwater",
        agent_code="groundwater",
        domain="groundwater-project-analysis",
        thread_id="user-groundwater:thread-groundwater",
        request_metadata={"groundwater_project_id": project_id},
        domain_context={"project_id": project_id},
    )


def test_groundwater_profile_binds_standard_knowledge_base():
    profile = AgentProfileLoader().load("groundwater")

    assert profile.default_kb_ids == []
    assert profile.default_skills == ["groundwater-project-analysis"]
    assert profile.tool_policy == "groundwater_default"
    assert profile.default_workflow == "full"
    assert "plan_solve" not in profile.allowed_workflows
    assert "multi_agent_parallel" in profile.allowed_workflows
    assert profile.domain_context_provider == (
        "backend.application.groundwater.runtime_context:"
        "resolve_groundwater_domain_context"
    )


def test_agent_service_resolves_groundwater_context_once():
    profile = AgentProfileLoader().load("groundwater")

    class CountingResolver(DomainContextResolver):
        calls = 0

        def resolve(self, *, profile, request):
            self.calls += 1
            return {
                "project_id": "project-authorized",
                "project_name": "Authorized Project",
            }

    resolver = CountingResolver()
    request = AgentRequest(
        question="分析项目",
        agent_code="groundwater",
        user_id="user-groundwater",
        metadata={"groundwater_project_id": "project-authorized"},
    )
    service = AgentService(
        profile_loader=AgentProfileLoader(),
        domain_context_resolver=resolver,
        runtime_policy=ProfileRuntimePolicy(
            kb_access_policy=KnowledgeBaseAccessPolicy(
                lookup=lambda ids: {
                    STANDARD_KB_ID: KnowledgeBaseRecord(
                        STANDARD_KB_ID,
                        "platform-admin",
                        False,
                        "active",
                    )
                },
                binding_lookup=lambda _code: [STANDARD_KB_ID],
            )
        ),
    )

    _, context = service._resolve_workflow(request)

    assert resolver.calls == 1
    assert context.agent_profile == profile
    assert context.domain_context["project_id"] == "project-authorized"
    assert context.subagent_enabled is True


def test_profile_provider_resolves_authorized_groundwater_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    builtin_root = tmp_path / "builtin-groundwater"
    builtin_root.mkdir()
    (builtin_root / "manifest.json").write_text(
        json.dumps(
            {
                "project": {"name": "六塘测试项目"},
                "data_version": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project_service = GroundwaterProjectService(
        projects_root=tmp_path / "projects",
        builtin_root=builtin_root,
    )
    monkeypatch.setattr(
        groundwater_runtime_context,
        "GroundwaterProjectService",
        lambda: project_service,
    )
    profile = AgentProfileLoader().load("groundwater")
    request = AgentRequest(
        question="分析六塘项目",
        agent_code="groundwater",
        user_id="user-groundwater",
        metadata={"groundwater_project_id": "liutang-groundwater"},
    )

    resolved = DomainContextResolver().resolve(profile=profile, request=request)

    assert resolved["project_id"] == "liutang-groundwater"
    assert resolved["project_name"]
    assert resolved["read_only"] is True


def test_groundwater_tool_rejects_unresolved_metadata_context():
    assembler = PolicyToolAssembler()
    runtime_context = ToolRuntimeContext(
        request_id="req-groundwater",
        user_id="user-groundwater",
        agent_code="groundwater",
        domain="groundwater-project-analysis",
        thread_id="user-groundwater:thread-groundwater",
        request_metadata={"groundwater_project_id": "liutang-groundwater"},
    )
    tool = assembler.materialize_tool_name(
        "groundwater_project_skill_read",
        runtime_context=runtime_context,
    )[0]

    result = json.loads(tool.invoke({"resource": "manifest", "limit": 10}))

    assert result["error"] == "地下水项目上下文未经过 AgentService 解析"


def test_groundwater_skill_and_policy_are_registered():
    manifest = SkillManifestLoader().load("groundwater-project-analysis")
    tools = PolicyToolAssembler().resolve_specs(
        ToolAssemblyContext(
            domain="groundwater-project-analysis",
            workflow="full",
            policy_name="groundwater_default",
            selected_skills={"groundwater-project-analysis"},
        )
    )

    assert manifest.kb_required is True
    assert "groundwater_prediction" in manifest.tools
    assert "groundwater_kml_summary" in manifest.tools
    assert "web_search" in manifest.tools
    assert "multi_kb_search" in {item.name for item in tools}
    assert "web_search" in {item.name for item in tools}
    assert "groundwater_project_skill_read" in {item.name for item in tools}
    assert "groundwater_project_bootstrap" in {item.name for item in tools}
    assert "query_project_tables" in {item.name for item in tools}
    assert "groundwater_kml_summary" in {item.name for item in tools}
    assert "groundwater_borehole_query" not in {item.name for item in tools}


def test_groundwater_business_tools_are_blocked_until_skill_is_loaded():
    middleware = SkillLoadGuardMiddleware(
        required_skill="groundwater-project-analysis",
        guarded_tools={"query_project_tables", "multi_kb_search"},
    )
    request = SimpleNamespace(
        tool_call={
            "id": "call-query",
            "name": "query_project_tables",
            "args": {"sql": "SELECT 1"},
        }
    )
    tokens = set_request_skill_state({"groundwater-project-analysis"})
    try:
        blocked = middleware.wrap_tool_call(request, lambda _: "executed")
        payload = json.loads(blocked.content)
        assert blocked.status == "error"
        assert payload["error"] == "skill_not_loaded"
        assert payload["required_skill"] == "groundwater-project-analysis"

        mark_skill_loaded("groundwater-project-analysis")
        assert middleware.wrap_tool_call(request, lambda _: "executed") == "executed"
    finally:
        reset_request_skill_state(tokens)


def test_groundwater_skill_guard_accepts_loaded_graph_state():
    middleware = SkillLoadGuardMiddleware(
        required_skill="groundwater-project-analysis",
        guarded_tools={"inspect_project_tables"},
    )
    request = SimpleNamespace(
        tool_call={
            "id": "call-graph-state",
            "name": "inspect_project_tables",
            "args": {},
        },
        state={
            "loaded_skills": {
                "groundwater-project-analysis": "# loaded skill content"
            }
        },
    )

    assert middleware._blocked_message(request) is None


async def test_groundwater_async_business_tool_guard_retries_after_skill_load():
    middleware = SkillLoadGuardMiddleware(
        required_skill="groundwater-project-analysis",
        guarded_tools={"multi_kb_search"},
    )
    request = SimpleNamespace(
        tool_call={
            "id": "call-kb",
            "name": "multi_kb_search",
            "args": {"query": "GB/T 14848 IV类限值"},
        }
    )

    async def execute(_):
        return "executed"

    tokens = set_request_skill_state({"groundwater-project-analysis"})
    try:
        blocked = await middleware.awrap_tool_call(request, execute)
        assert json.loads(blocked.content)["error"] == "skill_not_loaded"

        mark_skill_loaded("groundwater-project-analysis")
        assert await middleware.awrap_tool_call(request, execute) == "executed"
    finally:
        reset_request_skill_state(tokens)


def test_groundwater_prompt_requires_real_skill_file_before_project_tools():
    from backend.application.agents.prompt_templates.groundwater import (
        build_system_prompt,
    )

    prompt = build_system_prompt()

    skill_read = 'read_local_skill_file("groundwater-project-analysis")'
    project_skill_read = 'groundwater_project_skill_read(resource="skill")'
    assert skill_read in prompt
    assert prompt.index(skill_read) < prompt.index("groundwater_project_skill_read")
    assert project_skill_read in prompt
    assert prompt.index(project_skill_read) < prompt.index("inspect_project_tables")
    assert "groundwater_project_bootstrap" in prompt
    assert "真实表名、字段映射" in prompt


def test_groundwater_prompt_builder_supports_subagents_without_plan_solve():
    from backend.application.agents.prompt_builder import (
        PromptBuildContext,
        PromptBuilder,
    )

    class FakeSkillsProvider:
        def get_available_skills_for_user(self, selected_skills):
            return []

        def build_skills_prompt_section(self, skills):
            return ""

    prompt = PromptBuilder(skills_provider=FakeSkillsProvider()).build(
        PromptBuildContext(
            domain="groundwater-project-analysis",
            prompt_profile="groundwater",
            selected_skills={"groundwater-project-analysis"},
            subagent_enabled=True,
            use_search=True,
        )
    )

    assert "`task`" in prompt
    assert "允许使用 `task` 委派子智能体" in prompt
    assert "不代表启用或切换到 PlanSolve 工作流" in prompt


def test_groundwater_project_workflow_blocks_sql_until_project_contract_is_loaded():
    middleware = GroundwaterProjectWorkflowMiddleware()
    request = SimpleNamespace(
        tool_call={"id": "call-query", "name": "query_project_tables", "args": {"sql": "SELECT 1"}},
        state={"messages": []},
    )

    blocked = middleware.wrap_tool_call(request, lambda _: "executed")
    payload = json.loads(blocked.content)
    assert blocked.status == "error"
    assert payload["error"] == "groundwater_project_workflow_not_ready"
    assert payload["required_steps"] == [
        'groundwater_project_skill_read(resource="skill")',
        "groundwater_project_bootstrap()",
    ]

    request.state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-project-skill",
                        "name": "groundwater_project_skill_read",
                        "args": {"resource": "skill"},
                    },
                    {
                        "id": "call-bootstrap",
                        "name": "groundwater_project_bootstrap",
                        "args": {},
                    },
                ],
            ),
            ToolMessage(
                content='{"exists": true}',
                name="groundwater_project_skill_read",
                tool_call_id="call-project-skill",
            ),
            ToolMessage(
                content='{"project_id": "liutang-groundwater"}',
                name="groundwater_project_bootstrap",
                tool_call_id="call-bootstrap",
            ),
        ]
    }
    assert middleware.wrap_tool_call(request, lambda _: "executed") == "executed"


def test_groundwater_project_workflow_accepts_preloaded_graph_state():
    middleware = GroundwaterProjectWorkflowMiddleware()
    request = SimpleNamespace(
        tool_call={
            "id": "call-query",
            "name": "query_project_tables",
            "args": {"sql": "SELECT * FROM points LIMIT 5"},
        },
        state={
            "messages": [],
            "project_skill": "# 当前项目 Skill",
            "project_bootstrap": {
                "project_id": "gw-1",
                "data_version": 4,
                "tables": {"points": {"row_count": 198}},
            },
        },
    )

    assert middleware.wrap_tool_call(request, lambda _: "executed") == "executed"


@requires_builtin_groundwater_dataset
def test_builtin_liutang_project_is_available(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    service = GroundwaterProjectService()
    context = service.resolve("any-authenticated-user", "liutang-groundwater")
    manifest = service.read_resource(context, "manifest")

    assert context.read_only is True
    assert manifest["content"]["record_counts"]["points"] == 198
    assert manifest["content"]["record_counts"]["constructed_wells"] == 31


@requires_builtin_groundwater_dataset
def test_groundwater_tools_use_request_scoped_project_context(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    assembler = PolicyToolAssembler()
    tools = assembler.materialize_policy_tools(
        ToolAssemblyContext(
            domain="groundwater-project-analysis",
            workflow="full",
            policy_name="groundwater_default",
            selected_skills={"groundwater-project-analysis"},
        ),
        skip_tool_names={"multi_kb_search", "chat_file_search"},
        runtime_context=_runtime_context(),
    )

    manifest_result = json.loads(
        tools["groundwater_project_skill_read"].invoke(
            {"resource": "manifest", "limit": 10}
        )
    )
    well_result = json.loads(
        tools["query_project_tables"].invoke(
            {
                "sql": "SELECT * FROM constructed_wells WHERE point_code = 'Z11'",
                "max_rows": 20,
            }
        )
    )

    assert manifest_result["project_id"] == "liutang-groundwater"
    assert well_result["engine"] == "duckdb"
    assert well_result["rows"][0]["point_code"] == "Z11"


@requires_builtin_groundwater_dataset
def test_groundwater_project_bootstrap_returns_compact_schema_contract(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    tool = PolicyToolAssembler().materialize_tool_name(
        "groundwater_project_bootstrap",
        runtime_context=_runtime_context(),
    )[0]

    result = json.loads(tool.invoke({}))

    assert result["project_id"] == "liutang-groundwater"
    assert result["data_version"]
    assert result["tables"]["points"]["row_count"] == 198
    assert "point_code" in result["tables"]["points"]["columns"]
    assert "监测点、监测井、坐标或状态" in result["table_routes"]


def test_project_bootstrap_passes_selected_project_root_as_scoped_authority(tmp_path):
    project_root = tmp_path / "data" / "groundwater_projects" / "gw-test"
    table_path = project_root / "data" / "points.csv"
    table_path.parent.mkdir(parents=True)
    table_path.write_text("point_code,x_coord,y_coord\nGW01,1,2\n", encoding="utf-8")
    context = SimpleNamespace(
        project_id="gw-test",
        project_name="Test project",
        project_root=project_root,
        data_version=4,
        manifest={
            "datasets": {"points": {"csv": "data/points.csv"}},
            "quality_status": {"status": "ready"},
            "coordinate_reference": {},
        },
    )

    assert not is_allowed_local_table_path(str(table_path))
    payload = _bootstrap_payload(context)

    assert payload["tables"]["points"]["row_count"] == 1
    assert payload["tables"]["points"]["columns"] == [
        "point_code",
        "x_coord",
        "y_coord",
    ]


@requires_builtin_groundwater_dataset
def test_groundwater_bootstrap_survives_tool_message_context_pruning(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    # 断言 legacy 工具消息裁剪行为;CONTEXT_OBJECT_MODE env 默认已是 on,显式钉 off。
    monkeypatch.setenv("CONTEXT_OBJECT_MODE", "off")
    tool = PolicyToolAssembler().materialize_tool_name(
        "groundwater_project_bootstrap",
        runtime_context=_runtime_context(),
    )[0]
    bootstrap = tool.invoke({})
    state = {
        "messages": [
            HumanMessage(content="监测点怎么布设"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-bootstrap",
                        "name": "groundwater_project_bootstrap",
                        "args": {},
                    }
                ],
            ),
            ToolMessage(
                content=bootstrap,
                name="groundwater_project_bootstrap",
                tool_call_id="call-bootstrap",
            ),
        ]
    }
    middleware = ContextPruningMiddleware()

    updates = middleware._process_state_elevation(state)
    assert updates["project_bootstrap"]["tables"]["points"]["row_count"] == 198
    elevated_state = {**state, **updates}
    assembled = middleware._assemble_jit_system_message(
        SystemMessage(content="system"),
        elevated_state,
        messages=state["messages"],
    )
    assert "GROUNDWATER_PROJECT_SQL_CONTRACT" in assembled.content
    assert "points[rows=198" in assembled.content
    assert "point_code" in assembled.content


@requires_builtin_groundwater_dataset
def test_groundwater_tools_strip_malformed_parameter_suffix(monkeypatch):
    """A gateway may leak its XML-like parameter close tag into tool args."""
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    assembler = PolicyToolAssembler()
    tools = assembler.materialize_policy_tools(
        ToolAssemblyContext(
            domain="groundwater-project-analysis",
            workflow="full",
            policy_name="groundwater_default",
            selected_skills={"groundwater-project-analysis"},
        ),
        skip_tool_names={"multi_kb_search", "chat_file_search"},
        runtime_context=_runtime_context(),
    )

    resource_result = json.loads(
        tools["groundwater_project_skill_read"].invoke(
            {"resource": "background</parameter>"}
        )
    )
    query_result = json.loads(
        tools["query_project_tables"].invoke(
            {"sql": "SELECT COUNT(*) AS total FROM points</parameter"}
        )
    )

    assert resource_result["resource"] == "background"
    assert query_result["rows"][0]["total"] == 198


def test_create_project_is_user_scoped(tmp_path: Path):
    service = GroundwaterProjectService(
        projects_root=tmp_path / "projects",
        builtin_root=tmp_path / "missing-builtin",
    )
    created = service.create_project(
        "user-a",
        name="测试地下水项目",
        project_id="test-project",
        region="测试区",
        coordinate_system="CGCS2000",
    )

    assert created.project_root == (tmp_path / "projects" / "user-a" / "test-project").resolve()
    assert service.resolve("user-a", "test-project").project_name == "测试地下水项目"
    with pytest.raises(FileNotFoundError):
        service.resolve("user-b", "test-project")
