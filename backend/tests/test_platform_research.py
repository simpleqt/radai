"""深研/证据收敛/写作阶段契约（拆自 test_agent_platform_standardization.py）。"""

import inspect
import json

from backend.application.agent_executors.stream_handler import NEAgentStreamHandler
from backend.application.agent_executors.writer_stage import (
    EvidenceWriterStage,
    WriterStageConfig,
)
from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.application.agent_workflows.coordination_runtime_lifecycle import (
    CoordinationRuntimeLifecycle,
)
from backend.application.agent_workflows.research_branch_runner import (
    ResearchBranchRunner,
)
from backend.application.tools.runtime_invoker import RuntimeToolInvoker
from backend.domain.agents.profile import AgentProfile
from langchain_core.messages import AIMessage
from types import SimpleNamespace


def test_subagent_feature_injects_bounded_task_tool():
    from backend.core.agents.factory import _assemble_from_features
    from backend.core.agents.features import RuntimeFeatures
    from backend.core.agents.middlewares.subagent_limit import SubagentLimitMiddleware

    middleware, tools = _assemble_from_features(
        RuntimeFeatures(
            clarification=False,
            subagent=SubagentLimitMiddleware(max_concurrent=3),
            vision=False,
        )
    )

    assert "SubagentLimitMiddleware" in {type(item).__name__ for item in middleware}
    assert {item.name for item in tools} == {"task"}


def test_subagent_limit_preserves_calls_above_the_five_worker_queue():
    from langchain_core.messages import AIMessage

    from backend.core.agents.middlewares.subagent_limit import SubagentLimitMiddleware

    middleware = SubagentLimitMiddleware(max_concurrent=5)
    task_calls = [
        {"name": "task", "args": {}, "id": f"task-{index}", "type": "tool_call"}
        for index in range(6)
    ]
    state = {"messages": [AIMessage(content="", tool_calls=task_calls)]}

    assert middleware.max_concurrent == 5
    assert middleware.after_model(state, runtime=None) is None
    assert len(state["messages"][-1].tool_calls) == 6


async def test_follow_up_stream_uses_main_stream_as_deadline():
    import asyncio

    from backend.application.suggestions.follow_up_service import FollowUpStreamState

    blocker = asyncio.Event()

    async def wait_forever():
        await blocker.wait()
        return [], {}

    task = asyncio.create_task(wait_forever())
    state = FollowUpStreamState(
        task=task,
        fallback=lambda: (
            ["主回答结束时立即使用保底建议"],
            {"source": "fallback", "reason": "stream_finished"},
        ),
    )
    state.note_visible("answer")

    result = await state.take_ready(force=True)
    await asyncio.sleep(0)

    assert result == (
        ["主回答结束时立即使用保底建议"],
        {"source": "fallback", "reason": "stream_finished"},
    )
    assert task.cancelled()


async def test_follow_up_uses_configured_output_token_budget(monkeypatch):
    import backend.application.suggestions.follow_up_service as follow_up
    import backend.core.models as core_models

    bound = {}

    class FakeModel:
        def bind(self, **kwargs):
            bound.update(kwargs)
            return self

        async def ainvoke(self, messages):
            return SimpleNamespace(
                content='{"suggestions":["问题一是什么？","问题二是什么？"]}'
            )

    monkeypatch.setattr(core_models, "create_chat_model", lambda **kwargs: FakeModel())

    async def _fake_async_model(**_kwargs):
        return FakeModel()

    # 生产代码改走 create_chat_model_async（不在事件循环上同步查模型配置库），
    # 因此异步工厂也必须替换，否则会真去连 MySQL。
    monkeypatch.setattr(core_models, "create_chat_model_async", _fake_async_model)
    monkeypatch.setattr(follow_up.settings, "follow_up_suggestion_max_tokens", 4096)

    suggestions, metadata = await follow_up.FollowUpSuggestionService()._generate_with_llm(
        user_q="测试问题",
        answer="",
        tool_summary="",
        context_summary="测试上下文",
        count=2,
        selected_skills=[],
        model_name="minimax-m2.7",
    )

    assert bound["max_tokens"] == 4096
    assert suggestions == ["问题一是什么？", "问题二是什么？"]
    assert metadata["source"] == "llm_context"


async def test_follow_up_persists_to_latest_assistant_message():
    from backend.application.suggestions.follow_up_service import (
        persist_follow_up_to_checkpoint,
    )

    assistant = AIMessage(content="answer")
    captured = {}

    class FakeCheckpointer:
        async def aget(self, config):
            return {"channel_values": {"messages": [assistant]}}

    class FakeGraph:
        nodes = {"model": object()}

        async def aget_state(self, config):
            return SimpleNamespace(next=("model",), created_by=None)

        async def aupdate_state(self, config, **kwargs):
            captured.update(kwargs)

    await persist_follow_up_to_checkpoint(
        checkpointer=FakeCheckpointer(),
        graph=FakeGraph(),
        internal_thread_id="user-1:thread-1",
        suggestions=[{"id": "sug_1", "text": "继续了解什么？"}],
    )

    overwrite = captured["values"]["messages"]
    saved_messages = overwrite.value
    assert saved_messages[-1].additional_kwargs["follow_up_suggestions"] == [
        {"id": "sug_1", "text": "继续了解什么？"}
    ]
    assert captured["as_node"] == "model"


def test_deep_research_uses_plan_solve_coordination():
    workflow = CoordinatedAgentWorkflow(name="deep_research")

    assert isinstance(workflow._runtime_lifecycle, CoordinationRuntimeLifecycle)
    assert workflow._runtime_lifecycle._host is workflow
    assert isinstance(workflow._research_branch_runner, ResearchBranchRunner)
    assert workflow._research_branch_runner._host is workflow

    lifecycle_source = inspect.getsource(CoordinationRuntimeLifecycle._run_impl)
    branch_source = inspect.getsource(ResearchBranchRunner)
    assert 'self.name in {"plan_solve", "deep_research"}' in lifecycle_source
    assert "_run_plan_solve" in lifecycle_source
    assert "_run_research_task" in branch_source

    invoker_default = inspect.signature(
        CoordinatedAgentWorkflow._invoke_runtime_tool_structured
    ).parameters["invoker_type"].default
    assert invoker_default is RuntimeToolInvoker
    assert "save_deep_research_report_direct" not in (
        lifecycle_source + branch_source
    )
    # 分支检索不再经过「阻塞线程 + 一次性事件循环」桥：原生 async 在主循环
    # 上复用 keep-alive 客户端与按循环并发帽,超时取消可真正终止底层调用。
    assert "run_in_fresh_loop" not in branch_source
    assert "run_blocking" not in branch_source
    assert inspect.iscoroutinefunction(
        CoordinatedAgentWorkflow._invoke_runtime_tool_structured
    )


async def test_invoke_runtime_tool_structured_isolates_tool_context():
    import contextvars

    probe: contextvars.ContextVar[str] = contextvars.ContextVar(
        "probe_runtime_tool", default="outer"
    )
    probe.set("outer")

    class FakeInvoker:
        def __init__(self, **kwargs):
            pass

        async def invoke_structured(self, tool_name, args):
            # 工具侧上下文写入必须留在子任务的上下文拷贝里(旧 fresh-loop 桥
            # 的 copy_context 隔离语义),不回渗调用方。
            probe.set("inner")
            return {"text": "ok", "evidence_records": [], "stats": {}}

    result = await CoordinatedAgentWorkflow._invoke_runtime_tool_structured(
        None,
        "deep_research",
        "multi_kb_search",
        {},
        FakeInvoker,
    )

    assert result["text"] == "ok"
    assert probe.get() == "outer"


async def test_invoke_runtime_tool_structured_timeout_cancels_underlying_call():
    import asyncio

    import pytest

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingInvoker:
        def __init__(self, **kwargs):
            pass

        async def invoke_structured(self, tool_name, args):
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {}

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await asyncio.wait_for(
            CoordinatedAgentWorkflow._invoke_runtime_tool_structured(
                None,
                "deep_research",
                "multi_kb_search",
                {},
                HangingInvoker,
            ),
            timeout=0.05,
        )

    assert started.is_set()
    # 旧线程桥超时后底层调用仍会跑完;现在必须被联动取消。
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)


async def test_evidence_convergence_controller_evaluates_and_converges():
    from backend.application.agent_executors.evidence_convergence import (
        EvidenceConvergenceController,
        EvidenceConvergencePolicy,
    )
    from backend.application.agent_executors.stream_handler import NEAgentStreamHandler

    handler = NEAgentStreamHandler(
        is_deep_research=False,
        thread_id="thread-test",
    )
    async for _ in handler._on_tool_end(
        {
            "name": "query_project_tables",
            "data": {"output": '{"rows": [{"point_code": "GW18"}]}'},
        }
    ):
        pass
    async for _ in handler._on_tool_end(
        {
            "name": "multi_kb_search",
            "data": {"output": "标准知识库证据"},
        }
    ):
        pass

    class CompleteEvaluator:
        async def evaluate(self, **kwargs):
            assert kwargs["question"] == "如何评价 GW18？"
            assert "GW18" in kwargs["project_evidence"]
            assert "标准知识库证据" in kwargs["retrieved_evidence"]
            return SimpleNamespace(
                complete=True,
                missing_evidence=[],
                next_action="",
            )

    controller = EvidenceConvergenceController(
        policy=EvidenceConvergencePolicy(
            enabled=True,
            max_queries=20,
            min_retrieval_blocks=1,
        ),
        evaluator=CompleteEvaluator(),
        label="test",
    )
    updates = [
        update
        async for update in controller.on_tool_event(
            question="如何评价 GW18？",
            state=handler,
        )
    ]

    assert [update.message for update in updates] == [
        "正在评估现有证据是否足以回答问题...",
        "证据完整性评估通过，正在交给撰写智能体生成答案...",
    ]
    assert updates[-1].converged is True
    assert updates[-1].reason == "complete"
    assert not handler.should_evaluate_evidence(min_retrieval_blocks=1)


async def test_project_query_loop_forces_evidence_convergence():
    from backend.application.agent_executors.stream_handler import NEAgentStreamHandler

    handler = NEAgentStreamHandler(
        is_deep_research=False,
        thread_id="thread-test",
        question="根据项目数据布设监测点",
    )
    for _ in range(8):
        async for _ in handler._on_tool_end(
            {
                "name": "query_project_tables",
                "data": {"output": '{"rows": [{"point_code": "GW18"}]}'},
            }
        ):
            pass

    assert handler.should_force_evidence_convergence(max_queries=8)


async def test_project_only_evidence_can_converge_and_keeps_distinct_query_topics():
    from backend.application.agent_executors.stream_handler import NEAgentStreamHandler

    handler = NEAgentStreamHandler(
        is_deep_research=False,
        thread_id="thread-test",
        question="根据项目数据布设监测井",
    )
    handler.answer_fragments = ["我先加载项目数据。"]
    topics = ["points", "geology_layers", "constructed_wells", "water_levels"]
    for index, topic in enumerate(topics):
        run_id = f"call-{topic}"
        async for _ in handler._on_tool_start(
            {
                "name": "query_project_tables",
                "run_id": run_id,
                "data": {"input": {"sql": f"SELECT * FROM {topic}"}},
            }
        ):
            pass
        output = json.dumps(
            {
                "project_id": "gw-test",
                "data_version": 4,
                "row_count": 1,
                "columns": ["topic", "value"],
                "rows": [{"topic": topic, "value": index}],
                "query_kind": "data_query",
                "counts_toward_query_budget": True,
            }
        )
        async for _ in handler._on_tool_end(
            {
                "name": "query_project_tables",
                "run_id": run_id,
                "data": {"output": output},
            }
        ):
            pass

    # Reordered JSON keys contain the same result and must not displace a
    # distinct earlier topic from the writer evidence buffer.
    duplicate = json.dumps(
        {
            "rows": [{"value": 3, "topic": "water_levels"}],
            "columns": ["value", "topic"],
            "row_count": 1,
            "data_version": 4,
            "project_id": "gw-test",
            "query_kind": "data_query",
            "counts_toward_query_budget": True,
        }
    )
    async for _ in handler._on_tool_end(
        {"name": "query_project_tables", "data": {"output": duplicate}}
    ):
        pass

    assert len(handler._project_evidence_fragments) == 4
    for topic in topics:
        assert topic in handler.project_evidence_text
    assert "SELECT * FROM geology_layers" in handler.project_evidence_text
    assert handler.should_evaluate_evidence(min_retrieval_blocks=1)
    assert handler.retrieved_evidence_text == ""


async def test_evidence_evaluator_uses_model_semantic_verdict(monkeypatch):
    import backend.core.models as core_models
    from backend.application.agent_executors.evidence_evaluator import (
        EvidenceCompletenessEvaluator,
    )

    class FakeModel:
        async def ainvoke(self, messages):
            prompt = messages[-1].content
            assert "不使用固定点位、固定字段或固定指标规则" in prompt
            assert "用户提出的问题" in prompt
            return AIMessage(
                content=json.dumps(
                    {
                        "complete": False,
                        "missing_evidence": ["缺少能直接回答问题的实测结果"],
                        "next_action": "继续查询项目数据",
                        "rationale": "当前只有数据清单",
                    },
                    ensure_ascii=False,
                )
            )

    monkeypatch.setattr(core_models, "create_chat_model", lambda **kwargs: FakeModel())

    async def _fake_async_model(**_kwargs):
        return FakeModel()

    # 同上：evaluate() 走异步工厂，避免在主事件流循环上同步查库
    monkeypatch.setattr(core_models, "create_chat_model_async", _fake_async_model)

    result = await EvidenceCompletenessEvaluator(model_name="fake-model").evaluate(
        question="用户提出的问题",
        project_evidence="项目清单",
        retrieved_evidence="标准名称",
    )

    assert result.complete is False
    assert result.missing_evidence == ["缺少能直接回答问题的实测结果"]
    assert result.next_action == "继续查询项目数据"


async def test_groundwater_writer_stage_streams_thinking_and_persists_answer(monkeypatch):
    import backend.core.models as core_models

    class FakeGraph:
        def __init__(self):
            self.updates = []

        async def aupdate_state(self, config, values, **kwargs):
            self.updates.append((config, values, kwargs))

    class FakeModel:
        async def astream(self, messages):
            yield SimpleNamespace(content="<think>内部思考</think>答案正文")

    monkeypatch.setattr(core_models, "create_chat_model", lambda **kwargs: FakeModel())

    graph = FakeGraph()
    writer_stage = EvidenceWriterStage(
        WriterStageConfig(
            label="ProfiledGroundwaterAgent",
            graph=graph,
            model_name="fake-model",
        )
    )
    handler = NEAgentStreamHandler(is_deep_research=False, thread_id="thread-test", question="问题")
    handler.answer_fragments = ["我先加载项目数据。"]
    handler._project_evidence_fragments = ["项目证据"]
    handler._retrieved_evidence_fragments = ["标准证据"]

    events = [
        event
        async for event in writer_stage.stream(
            question="问题",
            effective_config={"configurable": {"thread_id": "thread-test"}},
            handler=handler,
        )
    ]

    assert any(event["type"] == "thinking" and "内部思考" in event["content"] for event in events)
    assert any(event["type"] == "chunk" and event["content"] == "答案正文" for event in events)
    assert "".join(handler.answer_fragments) == "答案正文"
    assert any(
        event["type"] == "message.replace"
        and event["payload"]["content"] == "答案正文"
        for event in events
    )
    assert graph.updates
    stored_message = graph.updates[-1][1]["messages"][0]
    assert isinstance(stored_message, AIMessage)
    assert stored_message.content == "答案正文"


async def test_groundwater_writer_stage_recovers_text_from_aggregated_stream_end(monkeypatch):
    from langchain_core.messages import AIMessageChunk

    import backend.core.models as core_models

    class FakeGraph:
        def __init__(self):
            self.updates = []

        async def aupdate_state(self, config, values, **kwargs):
            self.updates.append((config, values, kwargs))

    class EndOnlyHandler(NEAgentStreamHandler):
        async def _on_model_stream(self, _event):
            for item in ():
                yield item

    class FakeModel:
        async def astream(self, messages):
            yield AIMessageChunk(content="由模型结束事件恢复的正文")

    monkeypatch.setattr(core_models, "create_chat_model", lambda **kwargs: FakeModel())

    graph = FakeGraph()
    writer_stage = EvidenceWriterStage(
        WriterStageConfig(
            label="ProfiledGroundwaterAgent",
            graph=graph,
            model_name="fake-model",
        )
    )
    handler = EndOnlyHandler(is_deep_research=False, thread_id="thread-test", question="问题")
    handler._project_evidence_fragments = ["项目证据"]
    handler._retrieved_evidence_fragments = ["标准证据"]

    events = [
        event
        async for event in writer_stage.stream(
            question="问题",
            effective_config={"configurable": {"thread_id": "thread-test"}},
            handler=handler,
        )
    ]

    chunks = [event["content"] for event in events if event["type"] == "chunk"]
    assert "".join(chunks) == "由模型结束事件恢复的正文"
    assert "".join(handler.answer_fragments) == "由模型结束事件恢复的正文"
    assert graph.updates[-1][1]["messages"][0].content == "由模型结束事件恢复的正文"


async def test_groundwater_writer_stage_emits_heartbeat_while_waiting(monkeypatch):
    import asyncio

    import backend.application.agent_executors.stream_timeouts as stream_timeouts
    import backend.core.models as core_models

    class FakeGraph:
        async def aupdate_state(self, config, values, **kwargs):
            return None

    class SlowStreamingModel:
        async def astream(self, messages):
            await asyncio.sleep(0.03)
            yield SimpleNamespace(content="延迟后的正文")

    monkeypatch.setattr(core_models, "create_chat_model", lambda **kwargs: SlowStreamingModel())
    monkeypatch.setattr(stream_timeouts, "STREAM_HEARTBEAT_INTERVAL_SECONDS", 0.01)

    writer_stage = EvidenceWriterStage(
        WriterStageConfig(
            label="ProfiledGroundwaterAgent",
            graph=FakeGraph(),
            model_name="fake-model",
        )
    )
    handler = NEAgentStreamHandler(is_deep_research=False, thread_id="thread-test", question="问题")
    handler._project_evidence_fragments = ["项目证据"]
    handler._retrieved_evidence_fragments = ["标准证据"]

    events = [
        event
        async for event in writer_stage.stream(
            question="问题",
            effective_config={"configurable": {"thread_id": "thread-test"}},
            handler=handler,
        )
    ]

    assert any(event["type"] == "ping" for event in events)
    assert any(event["type"] == "chunk" and event["content"] == "延迟后的正文" for event in events)
    assert "".join(handler.answer_fragments) == "延迟后的正文"


async def test_deep_research_finalize_persists_fallback_answer(monkeypatch):
    import backend.application.artifacts.artifact_utils as artifact_utils
    import backend.application.tools.runtime_invoker as runtime_invoker
    from backend.application.agent_executors.profiled_executor import (
        ProfiledAgentExecutor,
    )

    class FakeGraph:
        def __init__(self):
            self.updates = []

        async def aupdate_state(self, config, values, **kwargs):
            self.updates.append((config, values, kwargs))

    class FakeHandler:
        def __init__(self):
            self.answer_fragments = []
            self.chunk_count = 0
            self.report_saved = False

        def build_live_markdown_content(self, *, result_content, title):
            return f"# {title}\n\n{result_content}"

        def build_live_markdown_artifact(self, *, content, title, is_streaming):
            return {"content": content, "title": title, "is_streaming": is_streaming}

    class FakeRuntimeToolInvoker:
        def __init__(self, **kwargs):
            pass

        async def invoke(self, tool_name, args):
            assert tool_name == "save_deep_research_report"
            return {
                "artifact_path": "/tmp/report.docx",
                "title": "深度研究报告",
                "minio_object": "reports/report.docx",
            }

    async def fake_artifact_metadata(**kwargs):
        return {"artifact_path": kwargs["artifact_path"], "title": kwargs["title"]}

    monkeypatch.setattr(runtime_invoker, "RuntimeToolInvoker", FakeRuntimeToolInvoker)
    monkeypatch.setattr(artifact_utils, "build_registered_artifact_metadata", fake_artifact_metadata)

    executor = ProfiledAgentExecutor.__new__(ProfiledAgentExecutor)
    executor._graph = FakeGraph()
    executor.profile = AgentProfile(
        code="ne",
        name="NE",
        domain="nuclear-electronics",
        tool_policy="ne_default",
        guardrail_policy="ne_domain_scope",
    )
    executor._domain = "nuclear-electronics"
    executor._request_metadata = {}
    executor._user_id = "user-1"
    executor._external_thread_id = "thread-test"
    executor._model_name = None
    executor._kb_ids = []
    executor._selected_skills = set()
    executor._use_search = False
    executor._subagent_enabled = False
    executor._thinking_enabled = False
    executor._runtime_permissions = {}
    executor._guardrail_instructions = ""
    executor._time_context = ""
    executor._tool_runtime_context = None
    handler = FakeHandler()

    events = [
        event
        async for event in executor._finalize_deep_research_report(
            question="问题",
            effective_thread_id="thread-test",
            handler=handler,
        )
    ]

    chunks = [event["content"] for event in events if event.get("type") == "chunk"]
    assert chunks == ["主流程未返回完整结论，系统已根据已收集证据完成研究报告归档。"]
    assert handler.answer_fragments == chunks
    assert handler.chunk_count == 1
    assert executor._graph.updates
    stored_message = executor._graph.updates[-1][1]["messages"][0]
    assert isinstance(stored_message, AIMessage)
    assert stored_message.content == chunks[0]


def test_deep_research_workflow_has_no_legacy_background_runtime_dependency():
    source = inspect.getsource(CoordinatedAgentWorkflow)

    assert "ResearchTaskRuntime" not in source
    assert "ResearchOrchestratorExecutor" not in source
    assert "confirmation_status" not in source
