"""Agent profile 运行时行为契约（拆自 test_agent_runtime_policies.py）。"""

import json
import pytest

from backend.application.agent_executors.lightweight_qa_agent import LightweightQAAgent
from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.application.policies.knowledge_base_access import (
    KnowledgeBaseAccessPolicy,
    KnowledgeBaseRecord,
)
from backend.application.policies.profile_runtime_policy import ProfileRuntimePolicy
from backend.core.thread_key import build_internal_thread_key
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext


def _profile(**kwargs) -> AgentProfile:
    values = {
        "code": "ne",
        "name": "NE",
        "domain": "nuclear-electronics",
        "default_kb_ids": ["stale-yaml-kb"],
        "kb_policy": "bound_or_selected",
        "guardrail_policy": "ne_domain_scope",
    }
    values.update(kwargs)
    return AgentProfile(**values)


def _context(profile: AgentProfile, workflow: str) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        request_id="req-1",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id=build_internal_thread_key("user-1", "thread-1"),
        agent_profile=profile,
        workflow_name=workflow,
        model_name=None,
        permissions={
            "knowledge_base": {"allowed_ids": ["bound-kb"]},
            "domain_context": {"authorized": True},
        },
        use_search=False,
    )


def test_profile_runtime_policy_requires_authorized_groundwater_project():
    profile = _profile(
        code="groundwater",
        domain="groundwater-project-analysis",
        default_kb_ids=[],
        guardrail_policy="groundwater_project_scope",
    )
    policy = ProfileRuntimePolicy(
        kb_access_policy=KnowledgeBaseAccessPolicy(
            lookup=lambda ids: {},
            binding_lookup=lambda _code: [],
        )
    )

    with pytest.raises(PermissionError, match="authorized project"):
        policy.evaluate(
            user_id="user-1",
            requested_kb_ids=[],
            profile=profile,
            domain_context={},
        )

    result = policy.evaluate(
        user_id="user-1",
        requested_kb_ids=[],
        profile=profile,
        domain_context={"project_id": "project-1", "read_only": True},
    )
    assert result.permissions["domain_context"]["project_id"] == "project-1"
    assert "project-1" in result.guardrail_instructions


def test_csh_runtime_policy_searches_selected_knowledge_before_scope_refusal():
    profile = _profile(
        code="csh",
        domain="csh-hydrogeology",
        default_kb_ids=[],
        guardrail_policy="csh_domain_scope",
    )
    policy = ProfileRuntimePolicy(
        kb_access_policy=KnowledgeBaseAccessPolicy(
            lookup=lambda ids: {
                kb_id: KnowledgeBaseRecord(kb_id, None, False, "active")
                for kb_id in ids
            },
            binding_lookup=lambda _code: [],
        )
    )

    result = policy.evaluate(
        user_id="user-1",
        requested_kb_ids=["environment-kb"],
        profile=profile,
        domain_context={},
    )

    assert result.kb_ids == ["environment-kb"]
    assert "环境空气" in result.guardrail_instructions
    assert "必须先检索所选知识库" in result.guardrail_instructions
    assert "未检索不得以超出专业范围为由拒答" in result.guardrail_instructions


@pytest.mark.asyncio
async def test_lightweight_agent_applies_runtime_tool_guardrail():
    allowed = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=["bound-kb"],
        use_search=False,
        prompt_profile="ne",
        domain="nuclear-electronics",
        allowed_tools=["multi_kb_search"],
        guardrail_policy="ne_domain_scope",
        runtime_permissions={
            "knowledge_base": {"allowed_ids": ["bound-kb"]},
            "domain_context": {"authorized": True},
        },
    )
    await allowed._authorize_tool("multi_kb_search", {"query": "test"})

    denied = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=["bound-kb"],
        use_search=False,
        prompt_profile="ne",
        domain="nuclear-electronics",
        allowed_tools=[],
        guardrail_policy="ne_domain_scope",
        runtime_permissions={
            "knowledge_base": {"allowed_ids": ["bound-kb"]},
            "domain_context": {"authorized": True},
        },
    )
    with pytest.raises(PermissionError, match="tool_not_allowed"):
        await denied._authorize_tool("multi_kb_search", {"query": "test"})


@pytest.mark.asyncio
async def test_lightweight_table_analysis_only_uses_assembled_authorized_tools():
    calls: list[tuple[str, dict]] = []

    class FakeTool:
        def __init__(
            self, name: str, *, risk_level: str = "safe", side_effect: str = "none"
        ):
            self.name = name
            self.metadata = {
                "risk_level": risk_level,
                "side_effect": side_effect,
                "policy_source": f"registry:{name}",
                "timeout_seconds": 1,
            }

        async def ainvoke(self, args):
            calls.append((self.name, dict(args)))
            return json.dumps({"status": "success", "tool": self.name})

    tools = {
        "inspect_excel_data": FakeTool("inspect_excel_data", side_effect="local_read"),
        "visualize_excel_data": FakeTool(
            "visualize_excel_data",
            risk_level="write",
            side_effect="artifact_write",
        ),
    }
    agent = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=[],
        use_search=False,
        prompt_profile="ne",
        domain="nuclear-electronics",
        allowed_tools=list(tools),
        runtime_tools=tools,
        runtime_permissions={
            "side_effects": {"allowed": ["local_read", "artifact_write"]}
        },
    )

    result = await agent._run_table_analysis(
        "请为 sample.csv 画图",
        [{"file_id": "file-1", "file_name": "sample.csv"}],
    )

    assert [name for name, _args in calls] == [
        "inspect_excel_data",
        "visualize_excel_data",
    ]
    assert "[图表结果]" in result


async def test_semantic_turn_guard_keeps_only_captured_cited_claims():
    class Executor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            yield {
                "type": "event",
                "event": {
                    "id": "web-result-before-turn-guard",
                    "kind": "tool_result",
                    "title": "联网检索完成",
                    "detail": "已返回网页证据",
                    "tool": "web_search",
                    "level": "success",
                    "metadata": {
                        "query": "SiC detector radiation tolerance",
                        "web_results": [
                            {
                                "title": "SiC detector study",
                                "url": "https://example.org/captured-study",
                                "content_excerpt": "研究显示碳化硅探测器具有耐辐照优势。",
                            }
                        ],
                    },
                },
            }
            yield {
                "type": "chunk",
                "content": (
                    "研究显示碳化硅探测器具有耐辐照优势 "
                    "https://example.org/captured-study\n"
                    "结论：另一项没有来源且与检索结果无关的断言。"
                ),
            }
            yield {
                "type": "error",
                "message": "runtime agent exceeded semantic model-turn limit (6)",
            }

    class Delegate:
        def build_executor(self, request, context):
            return Executor()

    result = await CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=Delegate(),
    )._run_unit(
        role="evidence_researcher",
        question="核验碳化硅探测器的材料优势",
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=_context(_profile(), "deep_research"),
        task_id="task-semantic-turn-partial",
    )

    assert result.error is None
    assert result.timed_out is False
    claims = (result.handoff or {}).get("claims") or []
    assert len(claims) == 1
    assert claims[0]["supporting_refs"] == ["https://example.org/captured-study"]
    assert any(
        event.get("metadata", {}).get("runtime_type")
        == "task_partial_evidence_recovered"
        for event in result.process_events
    )
