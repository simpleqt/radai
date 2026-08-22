"""KB 访问边界与运行时护栏契约（拆自 test_agent_runtime_policies.py）。"""

import asyncio
import pytest

from backend.application.agent_executors.lightweight_qa_agent import LightweightQAAgent
from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
    ResearchTask,
)
from backend.application.policies.knowledge_base_access import (
    KnowledgeBaseAccessError,
    KnowledgeBaseAccessPolicy,
    KnowledgeBaseRecord,
)
from backend.core.agents.middlewares.guardrail_provider import GuardrailRequest
from backend.core.agents.middlewares.runtime_policy import (
    RuntimePolicyGuardrailProvider,
)
from backend.core.thread_key import build_internal_thread_key
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext
from dataclasses import replace


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


def test_kb_access_policy_allows_public_owned_and_explicitly_granted_kbs():
    records = {
        "bound-kb": KnowledgeBaseRecord("bound-kb", "admin-1", False, "active"),
        "owned-kb": KnowledgeBaseRecord("owned-kb", "user-1", False, "active"),
        "default-kb": KnowledgeBaseRecord("default-kb", "admin-1", True, "active"),
    }
    policy = KnowledgeBaseAccessPolicy(
        lookup=lambda ids: {kb_id: records[kb_id] for kb_id in ids if kb_id in records},
        binding_lookup=lambda _code: ["bound-kb"],
        grant_lookup=lambda user_id, _ids: (
            {"bound-kb"} if user_id == "user-1" else set()
        ),
    )

    assert policy.resolve_allowed_kb_ids(
        user_id="user-1",
        requested_ids=["bound-kb", "owned-kb", "default-kb"],
        profile=_profile(),
    ) == ["bound-kb", "owned-kb", "default-kb"]


def test_kb_access_policy_uses_mysql_defaults_when_request_and_bindings_are_empty():
    records = {
        "default-kb": KnowledgeBaseRecord("default-kb", "admin-1", True, "active"),
    }
    policy = KnowledgeBaseAccessPolicy(
        lookup=lambda ids: {kb_id: records[kb_id] for kb_id in ids if kb_id in records},
        binding_lookup=lambda _code: [],
        default_lookup=lambda: ["default-kb"],
    )

    assert policy.resolve_allowed_kb_ids(
        user_id="user-1",
        requested_ids=[],
        profile=_profile(),
    ) == ["default-kb"]


def test_kb_access_policy_rejects_foreign_missing_and_inactive_kbs_but_allows_ownerless_public():
    records = {
        "foreign-kb": KnowledgeBaseRecord("foreign-kb", "user-2", False, "active"),
        "ownerless-kb": KnowledgeBaseRecord("ownerless-kb", None, False, "active"),
        "inactive-kb": KnowledgeBaseRecord("inactive-kb", None, False, "inactive"),
    }
    policy = KnowledgeBaseAccessPolicy(
        lookup=lambda ids: {kb_id: records[kb_id] for kb_id in ids if kb_id in records},
        binding_lookup=lambda _code: ["bound-kb"],
    )

    with pytest.raises(
        KnowledgeBaseAccessError,
        match="foreign-kb.*inactive-kb.*missing-kb",
    ):
        policy.resolve_allowed_kb_ids(
            user_id="user-1",
            requested_ids=[
                "foreign-kb",
                "ownerless-kb",
                "inactive-kb",
                "missing-kb",
            ],
            profile=_profile(),
        )


def test_kb_access_policy_enforces_bound_only_and_disabled_modes():
    bound_only = _profile(kb_policy="bound_only")
    disabled = _profile(kb_policy="disabled")
    records = {
        "bound-kb": KnowledgeBaseRecord("bound-kb", "admin-1", False, "active"),
    }
    policy = KnowledgeBaseAccessPolicy(
        lookup=lambda ids: {kb_id: records[kb_id] for kb_id in ids if kb_id in records},
        binding_lookup=lambda _code: ["bound-kb"],
    )

    # A foreign private binding is silently removed when it is only an
    # implicit agent default; the binding itself is not an access grant.
    assert (
        policy.resolve_allowed_kb_ids(
            user_id="user-1",
            requested_ids=[],
            profile=bound_only,
        )
        == []
    )
    with pytest.raises(KnowledgeBaseAccessError):
        policy.resolve_allowed_kb_ids(
            user_id="user-1",
            requested_ids=["other-kb"],
            profile=bound_only,
        )
    with pytest.raises(KnowledgeBaseAccessError):
        policy.resolve_allowed_kb_ids(
            user_id="user-1",
            requested_ids=["bound-kb"],
            profile=disabled,
        )


def test_kb_access_policy_validates_bound_kb_status_and_existence():
    inactive_policy = KnowledgeBaseAccessPolicy(
        lookup=lambda ids: {
            "bound-kb": KnowledgeBaseRecord("bound-kb", "admin-1", False, "inactive")
        },
        binding_lookup=lambda _code: ["bound-kb"],
    )
    with pytest.raises(KnowledgeBaseAccessError, match="bound-kb"):
        inactive_policy.resolve_allowed_kb_ids(
            user_id="user-1",
            requested_ids=["bound-kb"],
            profile=_profile(),
        )

    missing_policy = KnowledgeBaseAccessPolicy(
        lookup=lambda ids: {},
        binding_lookup=lambda _code: ["bound-kb"],
    )
    with pytest.raises(KnowledgeBaseAccessError, match="bound-kb"):
        missing_policy.resolve_allowed_kb_ids(
            user_id="user-1",
            requested_ids=["bound-kb"],
            profile=_profile(),
        )


def test_kb_access_policy_filters_private_agent_binding_for_users_and_guests():
    records = {
        "private-kb": KnowledgeBaseRecord("private-kb", "owner-1", False, "active"),
        "public-kb": KnowledgeBaseRecord("public-kb", None, False, "active"),
        "default-kb": KnowledgeBaseRecord("default-kb", "owner-1", True, "active"),
    }
    policy = KnowledgeBaseAccessPolicy(
        lookup=lambda ids: {kb_id: records[kb_id] for kb_id in ids if kb_id in records},
        binding_lookup=lambda _code: ["private-kb", "public-kb", "default-kb"],
    )

    assert policy.resolve_allowed_kb_ids(
        user_id="user-2",
        requested_ids=[],
        profile=_profile(),
    ) == ["public-kb", "default-kb"]
    assert policy.resolve_allowed_kb_ids(
        user_id="guest_device-scope",
        requested_ids=[],
        profile=_profile(),
    ) == ["public-kb", "default-kb"]
    with pytest.raises(KnowledgeBaseAccessError, match="private-kb"):
        policy.resolve_allowed_kb_ids(
            user_id="guest_device-scope",
            requested_ids=["private-kb"],
            profile=_profile(),
        )


def test_runtime_guardrail_denies_unresolved_kb_and_groundwater_permissions():
    kb_provider = RuntimePolicyGuardrailProvider(
        allowed_tools=["multi_kb_search"],
        guardrail_policy="ne_domain_scope",
        permissions={"knowledge_base": {"allowed_ids": []}},
    )
    kb_decision = kb_provider.evaluate(
        GuardrailRequest(tool_name="multi_kb_search", tool_input={})
    )
    assert kb_decision.allow is False
    assert kb_decision.reasons[0].code == "knowledge_base_not_authorized"

    groundwater_provider = RuntimePolicyGuardrailProvider(
        allowed_tools=["groundwater_prediction"],
        guardrail_policy="groundwater_project_scope",
        permissions={"domain_context": {"authorized": False}},
    )
    groundwater_decision = groundwater_provider.evaluate(
        GuardrailRequest(tool_name="groundwater_prediction", tool_input={})
    )
    assert groundwater_decision.allow is False
    assert groundwater_decision.reasons[0].code == "groundwater_project_not_authorized"


def test_runtime_guardrail_preserves_service_tool_metadata():
    provider = RuntimePolicyGuardrailProvider(
        allowed_tools=["publish_report"],
        guardrail_policy="ne_domain_scope",
        permissions={},
    )

    decision = provider.evaluate(
        GuardrailRequest(
            tool_name="publish_report",
            tool_input={},
            risk_level="write",
            side_effect="artifact_write",
            policy_source="registry:publish_report",
        )
    )

    assert decision.allow is True
    assert decision.metadata["risk_level"] == "write"
    assert decision.metadata["side_effect"] == "artifact_write"
    assert decision.metadata["policy_source"] == "registry:publish_report"


def test_runtime_guardrail_denies_configured_network_risk():
    provider = RuntimePolicyGuardrailProvider(
        allowed_tools=["external_search"],
        guardrail_policy="ne_domain_scope",
        permissions={"tool_risk": {"denied_levels": ["network"]}},
    )

    decision = provider.evaluate(
        GuardrailRequest(
            tool_name="external_search",
            tool_input={},
            risk_level="network",
            side_effect="external_read",
            policy_source="registry:external_search",
        )
    )

    assert decision.allow is False
    assert decision.reasons[0].code == "tool_risk_denied"


def test_runtime_guardrail_denies_disallowed_side_effect():
    provider = RuntimePolicyGuardrailProvider(
        allowed_tools=["save_report"],
        guardrail_policy="ne_domain_scope",
        permissions={"side_effects": {"allowed": ["external_read"]}},
    )

    decision = provider.evaluate(
        GuardrailRequest(
            tool_name="save_report",
            tool_input={},
            risk_level="write",
            side_effect="artifact_write",
            policy_source="registry:save_report",
        )
    )

    assert decision.allow is False
    assert decision.reasons[0].code == "tool_side_effect_denied"


def test_runtime_guardrail_denies_write_tools_for_guest_identity():
    provider = RuntimePolicyGuardrailProvider(
        allowed_tools=["save_report"],
        guardrail_policy="default",
        permissions={"auth_role": "guest"},
    )

    decision = provider.evaluate(
        GuardrailRequest(
            tool_name="save_report",
            tool_input={},
            risk_level="write",
            side_effect="artifact_write",
        )
    )

    assert decision.allow is False
    assert decision.reasons[0].code == "tool_write_requires_authenticated_user"


@pytest.mark.asyncio
async def test_lightweight_guardrail_uses_bound_tool_risk_metadata():
    tool = type(
        "NetworkTool",
        (),
        {
            "name": "web_search",
            "metadata": {
                "risk_level": "network",
                "side_effect": "external_read",
                "policy_source": "registry:web_search",
            },
        },
    )()
    agent = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=[],
        use_search=True,
        prompt_profile="ne",
        domain="nuclear-electronics",
        allowed_tools=["web_search"],
        runtime_tools={"web_search": tool},
        runtime_permissions={"tool_risk": {"denied_levels": ["network"]}},
    )

    with pytest.raises(PermissionError, match="tool_risk_denied"):
        await agent._authorize_tool("web_search", {"query": "test"}, tool_obj=tool)


async def test_kb_prefetch_failure_is_cleared_when_specialist_recovers(monkeypatch):
    class Executor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            yield {
                "type": "event",
                "event": {
                    "id": "kb-recovered",
                    "kind": "tool_result",
                    "title": "知识库检索完成",
                    "detail": "子智能体已自行完成知识库检索",
                    "tool": "multi_kb_search",
                    "level": "success",
                },
            }
            yield {"type": "chunk", "content": "recovered evidence"}

    class Delegate:
        def build_executor(self, request, context):
            return Executor()

    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=Delegate(),
    )

    async def fail_prefetch(*args, **kwargs):
        raise RuntimeError("temporary prefetch failure")

    monkeypatch.setattr(
        workflow,
        "_invoke_runtime_tool_structured",
        fail_prefetch,
    )
    context = replace(
        _context(_profile(), "deep_research"),
        kb_ids=["kb-1"],
        use_search=False,
    )

    result = await workflow._run_research_task(
        task=ResearchTask(
            key="kb-task",
            title="KB evidence",
            question="find KB evidence",
            expected_tools=("multi_kb_search",),
            agent_type="evidence_researcher",
        ),
        index=1,
        role="evidence_researcher",
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=context,
    )

    assert result.answer == "recovered evidence"
    assert result.error is None
    assert any(
        event.get("tool") == "multi_kb_search"
        and event.get("kind") == "tool_result"
        and event.get("level") == "success"
        for event in result.process_events
    )


async def test_cancelled_kb_placeholder_is_not_treated_as_prefetched_evidence(
    monkeypatch,
):
    """熔断后的"已取消"占位结果不得当成预取成功的知识库证据。

    占位结果是成功形状(有 text、无 error):修复前取消告示文本被注入专家
    提示词,prefetched_tools 契约核验误判 multi_kb_search 已完成。
    """

    class Executor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            yield {"type": "chunk", "content": "recovered evidence"}

    class Delegate:
        def build_executor(self, request, context):
            return Executor()

    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=Delegate(),
    )

    async def cancelled_prefetch(*args, **kwargs):
        return {
            "text": "本次请求的知识库检索已取消，未继续执行排队中的检索。",
            "evidence_records": [],
            "stats": {"cancelled": True},
        }

    monkeypatch.setattr(
        workflow,
        "_invoke_runtime_tool_structured",
        cancelled_prefetch,
    )
    context = replace(
        _context(_profile(), "deep_research"),
        kb_ids=["kb-1"],
        use_search=False,
    )

    result = await workflow._run_research_task(
        task=ResearchTask(
            key="kb-cancelled",
            title="KB evidence",
            question="find KB evidence",
            expected_tools=("multi_kb_search",),
            agent_type="evidence_researcher",
        ),
        index=1,
        role="evidence_researcher",
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=context,
    )

    assert result.answer == "recovered evidence"
    # 预取不得以"成功"事件收尾:取消占位必须走失败降级通道。
    assert not any(
        "已将选中知识库的召回结果交给子智能体" in str(event.get("detail") or "")
        for event in result.process_events
    )


async def test_shrunken_kb_fuse_does_not_open_request_wide_circuit(monkeypatch):
    """晚启动分支的收缩引信超时只放弃本分支预取,不得对全请求拉闸。

    phase_bounded_timeout 按剩余时间收缩引信(补研分支可远低于名义 180 秒);
    修复前以收缩引信超时也会 set kb_circuit_open + 取消所有在途 KB 检索,
    后续证据恢复分支全部死在无 KB 的通道里。
    """

    import time as _time

    class Executor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            yield {"type": "chunk", "content": "late branch answer"}

    class Delegate:
        def build_executor(self, request, context):
            return Executor()

    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=Delegate(),
    )

    async def hanging_prefetch(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(
        workflow,
        "_invoke_runtime_tool_structured",
        hanging_prefetch,
    )
    context = replace(
        _context(_profile(), "deep_research"),
        kb_ids=["kb-1"],
        use_search=False,
    )
    circuit = asyncio.Event()

    await workflow._run_research_task(
        task=ResearchTask(
            key="kb-late",
            title="late KB branch",
            question="find KB evidence",
            expected_tools=("multi_kb_search",),
            agent_type="evidence_researcher",
        ),
        index=1,
        role="evidence_researcher",
        kb_circuit_open=circuit,
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=context,
        research_deadline=_time.monotonic() + 0.05,
    )

    assert not circuit.is_set()


async def test_turn_kb_context_budget_is_shared_across_parallel_tasks():
    from backend.core.tools.request_limits import (
        get_turn_kb_chars_remaining,
        record_turn_kb_chars,
        reset_turn_kb_context_budget,
    )

    reset_turn_kb_context_budget()

    async def consume(char_count: int) -> int:
        await asyncio.sleep(0)
        return record_turn_kb_chars(char_count)

    await asyncio.gather(consume(100), consume(75))

    assert get_turn_kb_chars_remaining(250) == 75
    assert record_turn_kb_chars(25) == 200
