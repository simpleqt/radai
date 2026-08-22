"""协调单元事件/持久化/停滞契约（专用 Fake 子家族；拆自 test_agent_runtime_policies.py）。"""

import asyncio

from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.core.thread_key import build_internal_thread_key
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
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


class _FakeExecutor:
    def __init__(self, role: str, calls: list[dict], activity: dict):
        self._role = role
        self._calls = calls
        self._activity = activity

    async def aquery_stream(self, question, thread_id, runnable_config=None):
        self._activity["active"] += 1
        self._activity["max_active"] = max(
            self._activity["max_active"], self._activity["active"]
        )
        self._calls.append(
            {"role": self._role, "question": question, "thread_id": thread_id}
        )
        await asyncio.sleep(0.01)
        self._activity["active"] -= 1
        yield {"type": "chunk", "content": f"{self._role}-answer"}


class _FakeDelegate:
    def __init__(self):
        self.calls: list[dict] = []
        self.activity = {"active": 0, "max_active": 0}

    def build_executor(self, request, context):
        role = request.metadata["coordination_role"]
        return _FakeExecutor(role, self.calls, self.activity)


class _EmptyWriterExecutor(_FakeExecutor):
    async def aquery_stream(self, question, thread_id, runnable_config=None):
        self._calls.append(
            {"role": self._role, "question": question, "thread_id": thread_id}
        )
        if self._role == "planner":
            yield {"type": "chunk", "content": ""}
        elif self._role == "report_writer":
            yield {"type": "chunk", "content": ""}
        else:
            yield {"type": "chunk", "content": f"{self._role} evidence"}


class _EmptyWriterDelegate(_FakeDelegate):
    def build_executor(self, request, context):
        role = request.metadata["coordination_role"]
        return _EmptyWriterExecutor(role, self.calls, self.activity)


class _PersistingGraph:
    def __init__(self):
        self.updates: list[dict] = []

    async def aupdate_state(self, config, values, **kwargs):
        self.updates.append({"config": config, "values": values, "kwargs": kwargs})


class _PersistingExecutor(_FakeExecutor):
    def __init__(
        self, role: str, calls: list[dict], activity: dict, graph: _PersistingGraph
    ):
        super().__init__(role, calls, activity)
        self.graph = graph


class _PersistingDelegate(_FakeDelegate):
    def __init__(self):
        super().__init__()
        self.graph = _PersistingGraph()

    def build_executor(self, request, context):
        role = request.metadata.get("coordination_role", "parent")
        return _PersistingExecutor(role, self.calls, self.activity, self.graph)


class _StalledExecutor(_FakeExecutor):
    async def aquery_stream(self, question, thread_id, runnable_config=None):
        self._calls.append(
            {"role": self._role, "question": question, "thread_id": thread_id}
        )
        await asyncio.sleep(10)
        yield {"type": "chunk", "content": "too late"}


class _StalledDelegate(_FakeDelegate):
    def build_executor(self, request, context):
        role = request.metadata["coordination_role"]
        return _StalledExecutor(role, self.calls, self.activity)


class _ToolEventExecutor(_FakeExecutor):
    async def aquery_stream(self, question, thread_id, runnable_config=None):
        self._calls.append(
            {"role": self._role, "question": question, "thread_id": thread_id}
        )
        yield {
            "type": "event",
            "event": {
                "id": "inner-tool-start",
                "kind": "tool_start",
                "title": "工具调用",
                "detail": "使用工具: query_project_tables",
                "tool": "query_project_tables",
                "level": "active",
                "metadata": {"input": {"sql": "SELECT 1"}},
            },
        }
        yield {
            "type": "event",
            "event": {
                "id": "inner-tool-result",
                "kind": "tool_result",
                "title": "结果摘要",
                "detail": "1 row",
                "tool": "query_project_tables",
                "level": "info",
                "metadata": {
                    "_coordination_evidence_records": [
                        {
                            "id": "ev-project-row-1",
                            "source_tool": "query_project_tables",
                            "source_id": "query_project_tables",
                            "chunk_id": "inner-tool-result",
                            "doc_title": "project row",
                            "claim": "project evidence confirms one row",
                            "content": "project evidence confirms one row",
                        }
                    ]
                },
            },
        }
        yield {
            "type": "chunk",
            "content": "结论：project evidence confirms one row.",
        }


class _ToolEventDelegate(_FakeDelegate):
    def build_executor(self, request, context):
        role = request.metadata["coordination_role"]
        return _ToolEventExecutor(role, self.calls, self.activity)


async def test_deep_research_persists_visible_turn_to_parent_thread(monkeypatch):
    import backend.application.agent_workflows.coordinated_agent_workflow as workflow_module

    class FakeRuntimeToolInvoker:
        def __init__(self, **kwargs):
            pass

        async def invoke(self, tool_name, args):
            if tool_name != "save_deep_research_report":
                raise AssertionError(f"unexpected tool: {tool_name}")
            return {"title": "深度研究报告", "artifact_path": "report.docx"}

    async def fake_artifact_metadata(**kwargs):
        return {"id": "file-1", "title": "深度研究报告", "url": "/download/report"}

    monkeypatch.setattr(workflow_module, "RuntimeToolInvoker", FakeRuntimeToolInvoker)
    monkeypatch.setattr(
        workflow_module, "build_registered_artifact_metadata", fake_artifact_metadata
    )

    delegate = _PersistingDelegate()
    profile = _profile()
    context = _context(profile, "deep_research")
    request = AgentRequest(
        question="research topic",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="deep_research",
        use_search=False,
    )

    events = [
        event
        async for event in CoordinatedAgentWorkflow(
            name="deep_research",
            delegate=delegate,
            checkpointer=object(),
        ).run(request, context)
    ]

    assert any(event.type in {"message.delta", "message.replace"} for event in events)
    assert delegate.graph.updates
    update = delegate.graph.updates[-1]
    assert update["config"]["configurable"]["thread_id"] == context.internal_thread_id
    saved_messages = update["values"]["messages"]
    assert saved_messages[0].type == "human"
    assert saved_messages[0].content == "research topic"
    assert saved_messages[1].type == "ai"
    assert "report_writer-answer" in saved_messages[1].content
    assert update["kwargs"]["as_node"] == "model"


async def test_deep_research_builds_fallback_report_when_writer_is_empty(monkeypatch):
    import backend.application.agent_workflows.coordinated_agent_workflow as workflow_module

    class FakeRuntimeToolInvoker:
        def __init__(self, **kwargs):
            pass

        async def invoke(self, tool_name, args):
            raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(workflow_module, "RuntimeToolInvoker", FakeRuntimeToolInvoker)

    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=_EmptyWriterDelegate(),
        runtime_budget=AgentRuntimeBudget(deep_research_reflection_rounds=0),
    )
    results, tasks = await workflow._run_plan_solve(
        AgentRequest(
            question="fallback research topic",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        _context(_profile(), "deep_research"),
    )

    assert len(tasks) == 3
    assert results[-1].role == "report_writer"
    assert results[-1].answer.startswith("# fallback research topic")
    assert "## 摘要" in results[-1].answer
    assert "domain_analyst evidence" not in results[-1].answer
    assert "未检索到可用于该维度的逐项证据条目" in results[-1].answer
    assert "待核验" not in results[-1].answer


async def test_stalled_research_unit_emits_progress_then_times_out(monkeypatch):
    import backend.application.agent_workflows.coordination_runtime_support as budget_module

    monkeypatch.setattr(budget_module, "_UNIT_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(budget_module, "_UNIT_IDLE_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(budget_module, "_UNIT_TOTAL_TIMEOUT_SECONDS", 0.2)

    emitted_events = []

    async def collect_event(event):
        emitted_events.append(event)

    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=_StalledDelegate(),
    )
    result = await workflow._run_unit(
        role="researcher_1",
        question="stalled question",
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=_context(_profile(), "deep_research"),
        event_sink=collect_event,
        progress_title="研究子智能体仍在运行",
        task_id="task-1",
    )

    assert result.answer == ""
    assert "produced no events" in str(result.error)
    assert any(
        event_type == "workflow.step"
        and payload["event"]["metadata"]["runtime_type"] == "task_running"
        for event_type, payload in emitted_events
    )


async def test_research_unit_forwards_inner_full_tool_events_to_task_timeline():
    emitted_events = []

    async def collect_event(event):
        emitted_events.append(event)

    workflow = CoordinatedAgentWorkflow(
        name="plan_solve",
        delegate=_ToolEventDelegate(),
    )
    result = await workflow._run_unit(
        role="hydrogeology_analyst",
        question="query project data",
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="plan_solve",
        ),
        context=_context(_profile(), "plan_solve"),
        event_sink=collect_event,
        task_id="task-project-1",
    )

    assert result.answer == "结论：project evidence confirms one row."
    assert result.handoff["claims"][0]["supporting_refs"] == [
        "[project row#inner-tool-result]"
    ]
    assert [event["kind"] for event in result.process_events] == [
        "tool_start",
        "tool_result",
    ]
    forwarded = [
        payload["event"]
        for event_type, payload in emitted_events
        if event_type == "workflow.step"
    ]
    assert [event["tool"] for event in forwarded] == [
        "query_project_tables",
        "query_project_tables",
    ]
    assert forwarded[0]["metadata"] == {
        "input": {"sql": "SELECT 1"},
        "task_id": "task-project-1",
        "subagent_type": "hydrogeology_analyst",
        "coordination_role": "hydrogeology_analyst",
        "runtime_type": "task_tool_start",
        "unit_event_kind": "tool_start",
    }
    assert forwarded[1]["metadata"]["runtime_type"] == "task_tool_end"
    assert "_coordination_evidence_records" not in forwarded[1]["metadata"]


async def test_non_task_coordination_unit_captures_tool_evidence_for_handoff():
    delegate = _ToolEventDelegate()
    workflow = CoordinatedAgentWorkflow(
        name="multi_agent_parallel",
        delegate=delegate,
    )

    result = await workflow._run_unit(
        role="data_analyst",
        question="inspect project data",
        request=AgentRequest(
            question="analyze",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="multi_agent_parallel",
        ),
        context=_context(_profile(), "multi_agent_parallel"),
    )

    assert result.handoff["claims"][0]["supporting_refs"] == [
        "[project row#inner-tool-result]"
    ]
    assert [event["kind"] for event in result.process_events] == [
        "tool_start",
        "tool_result",
    ]
    assert all(
        event["metadata"]["runtime_type"].startswith("unit_tool_")
        for event in result.process_events
    )
