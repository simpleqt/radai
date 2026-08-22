from backend.application.agent_executors.writer_stage import EvidenceWriterStage
from backend.application.tools.providers.context_read_tool import (
    reset_request_capability_ledger,
    set_request_capability_ledger,
)
from backend.core.agents.context.artifact_store import ArtifactStore
from backend.core.agents.context.capability_ledger import CapabilityLedger
from backend.core.agents.context.object_store import ContextObjectStore
from backend.core.agents.context.objects import (
    make_context_object,
    make_segment,
)


def test_method_question_does_not_receive_compliance_template():
    prompt = EvidenceWriterStage._prompt_template(
        question="如何利用抽水试验数据降低渗透系数的不确定性？",
        evidence_pack="项目证据",
        evidence_limitations="- 当前证据未提供降深时间序列",
    )

    assert "不要套用“标准限值、超标倍数、超标判断”格式" in prompt
    assert "方法或方案类问题应先说明如何做" in prompt
    assert "当前证据未提供降深时间序列" in prompt
    assert "能计算时给出点位或层位" not in prompt


def test_explicit_compliance_question_receives_compliance_template():
    prompt = EvidenceWriterStage._prompt_template(
        question="GW18 的锰是否超标，超标倍数是多少？",
        evidence_pack="项目证据与标准证据",
    )

    assert "适用标准依据和超标/达标判断分开写" in prompt
    assert "标准限值、差值和超标倍数" in prompt
    assert "不要套用“标准限值、超标倍数、超标判断”格式" not in prompt


def test_writer_prompt_includes_ledger_and_reference_rules():
    prompt = EvidenceWriterStage._prompt_template(
        question="如何对比原位与异位修复？",
        evidence_pack="证据",
        ledger_block="【证据台账】本轮共 1 条证据。",
    )

    assert "【证据台账】" in prompt
    assert "参考文档：" in prompt
    assert "禁止使用知识库容器名称作为引用" in prompt
    assert "编号与台账完全一致" in prompt


def _writer_stage_with_ledger(
    tmp_path,
    *,
    owner_user="u1",
    owner_thread="t1",
    ledger_user=None,
):
    from backend.application.agent_executors.writer_stage import WriterStageConfig

    store = ContextObjectStore(ArtifactStore(tmp_path))
    obj = make_context_object(
        schema="multi_kb_search/v1",
        origin={
            "tool": "multi_kb_search",
            "turn_id": "turn-1",
            "user_id": owner_user,
            "thread_id": owner_thread,
        },
        index={"query": "修复成本"},
        segments=[
            make_segment(
                sid="kb:cost-1",
                title="修复成本对照",
                locator="p.236",
                text="异位修复约 50~1500 美元/吨，原位修复成本视工艺差异较大。",
                score=0.9,
            )
        ],
    )
    oid = store.put(obj)
    ledger = CapabilityLedger(
        turn_id="turn-1",
        token_pool=50000,
        object_store=store,
        user_id=ledger_user or owner_user,
        thread_id=owner_thread,
    )
    ledger.register_objects([obj])
    ledger.reassign_citation_numbers()
    token = set_request_capability_ledger("req-writer-ledger", ledger)
    stage = EvidenceWriterStage(
        WriterStageConfig(
            label="test-writer",
            graph=None,
            model_name=None,
            request_metadata={"_request_id": "req-writer-ledger"},
        )
    )
    return stage, ledger, oid, token


def test_writer_materializes_ledger_evidence_from_object_store(tmp_path):
    stage, ledger, _oid, token = _writer_stage_with_ledger(tmp_path)
    try:
        rows = stage._ledger_rows_for_request()
        assert rows and rows[0]["sid"] == "kb:cost-1"
        records = stage._ledger_evidence_records(rows)
        assert len(records) == 1
        assert records[0]["chunk_id"] == "kb:cost-1"
        assert "50~1500 美元/吨" in records[0]["content"]
        assert records[0]["category"] == "retrieval"
    finally:
        reset_request_capability_ledger(token, "req-writer-ledger")


def test_writer_materialized_evidence_enters_build_messages(tmp_path):
    stage, ledger, _oid, token = _writer_stage_with_ledger(tmp_path)
    try:
        rows = stage._ledger_rows_for_request()
        records = stage._ledger_evidence_records(rows)
        messages = stage._build_messages(
            question="原位修复成本通常比异位高多少？",
            project_evidence="",
            retrieved_evidence="",
            ledger_rows=rows,
            ledger_evidence=records,
        )
        assert any("50~1500 美元/吨" in str(message.content) for message in messages)
    finally:
        reset_request_capability_ledger(token, "req-writer-ledger")


def test_writer_ledger_evidence_rejects_foreign_owner(tmp_path):
    stage, _ledger, _oid, token = _writer_stage_with_ledger(
        tmp_path,
        owner_user="u1",
        owner_thread="t1",
        ledger_user="attacker",
    )
    try:
        rows = stage._ledger_rows_for_request()
        records = stage._ledger_evidence_records(rows)
        assert records == []
    finally:
        reset_request_capability_ledger(token, "req-writer-ledger")


async def test_writer_stream_coalesces_fast_model_chunks(monkeypatch):
    from langchain_core.messages import AIMessageChunk

    import backend.core.models as core_models
    from backend.application.agent_executors.stream_handler import NEAgentStreamHandler
    from backend.application.agent_executors.writer_stage import WriterStageConfig

    class FakeGraph:
        async def aupdate_state(self, config, values, **kwargs):
            return None

    class FastModel:
        async def astream(self, messages):
            for _ in range(120):
                yield AIMessageChunk(content="字")

    monkeypatch.setattr(core_models, "create_chat_model", lambda **kwargs: FastModel())

    stage = EvidenceWriterStage(
        WriterStageConfig(
            label="test-writer",
            graph=FakeGraph(),
            model_name="fake-model",
        )
    )
    handler = NEAgentStreamHandler(
        is_deep_research=False,
        thread_id="thread-test",
        question="如何分析？",
    )
    handler._project_evidence_fragments = ["项目证据"]

    events = [
        event
        async for event in stage.stream(
            question="如何分析？",
            effective_config={"configurable": {"thread_id": "thread-test"}},
            handler=handler,
        )
    ]
    chunks = [event["content"] for event in events if event["type"] == "chunk"]

    assert "".join(chunks) == "字" * 120
    assert len(chunks) <= 4


def _persist_stage(graph):
    from backend.application.agent_executors.writer_stage import WriterStageConfig

    return EvidenceWriterStage(
        WriterStageConfig(
            label="test-writer",
            graph=graph,
            model_name=None,
        )
    )


async def test_persist_answer_returns_true_on_confirmed_write():
    class OkGraph:
        def __init__(self):
            self.update_calls = 0

        async def aupdate_state(self, _config, _update, as_node=None):
            self.update_calls += 1

    graph = OkGraph()
    stage = _persist_stage(graph)

    assert (
        await stage._persist_answer(effective_config={}, answer_text="最终答案")
        is True
    )
    assert graph.update_calls == 1


async def test_persist_answer_failure_returns_false_so_fallback_can_write():
    """落库失败不得报告成功:否则 mark_answer_persisted_to_state 被置位,
    递归上限/超时路径的兜底 aupdate_state 被跳过,答案在刷新后从历史消失。"""

    class FailingGraph:
        async def aupdate_state(self, *_args, **_kwargs):
            raise RuntimeError("update rejected")

        async def aget_state(self, _config):
            raise RuntimeError("state unavailable")

    stage = _persist_stage(FailingGraph())

    assert (
        await stage._persist_answer(effective_config={}, answer_text="最终答案")
        is False
    )


async def test_persist_answer_timeout_verifies_state_before_retry():
    """写入超时后先读回验证:提交可能已在后台完成,盲目重试会把同一答案
    追加成第二条 AIMessage(add_messages 按新 id 追加不去重)。"""

    from langchain_core.messages import AIMessage

    class SlowCommitGraph:
        def __init__(self):
            self.update_calls = 0

        async def aupdate_state(self, _config, _update, as_node=None):
            self.update_calls += 1
            raise TimeoutError

        async def aget_state(self, _config):
            class State:
                values = {"messages": [AIMessage(content="最终答案")]}

            return State()

    graph = SlowCommitGraph()
    stage = _persist_stage(graph)

    assert (
        await stage._persist_answer(effective_config={}, answer_text="最终答案")
        is True
    )
    assert graph.update_calls == 1


async def test_persist_answer_timeout_retries_once_when_state_missing_answer():
    class FlakyGraph:
        def __init__(self):
            self.update_calls = 0

        async def aupdate_state(self, _config, _update, as_node=None):
            self.update_calls += 1
            if self.update_calls == 1:
                raise TimeoutError

        async def aget_state(self, _config):
            class State:
                values = {"messages": []}

            return State()

    graph = FlakyGraph()
    stage = _persist_stage(graph)

    assert (
        await stage._persist_answer(effective_config={}, answer_text="最终答案")
        is True
    )
    assert graph.update_calls == 2
