import json
import re
from dataclasses import replace
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from backend.core.agents.context.artifact_store import ArtifactStore, MinioContextArtifactStore
from backend.core.agents.context.engine import ContextEngine
from backend.core.agents.context.ledger import make_evidence_record
from backend.core.agents.context.policy import ContextPolicy
from backend.core.agents.context.state_adapter import ContextStateAdapter
from backend.core.agents.context.subagent_adapter import (
    build_subagent_result,
    format_handoff_bundle,
    format_subagent_handoff,
    normalize_subagent_result,
)
from backend.core.agents.middlewares.context_pruning import ContextPruningMiddleware


def test_context_state_adapter_is_the_only_legacy_state_boundary(monkeypatch):
    monkeypatch.delenv("JIT_MAX_EVIDENCE_BLOCKS", raising=False)
    policy = replace(
        ContextPolicy.from_env(None),
        retrieval_fallback_blocks=1,
        retrieval_fallback_chars=200,
    )
    externalized = make_evidence_record(
        source_tool="read_local_skill_file",
        content="bounded skill summary",
        category="skills",
        source_id="hydro",
        kind="skill_rule",
        tags=["skill_rule", "hydro"],
    )

    records = ContextStateAdapter(policy).records(
        {
            "context_records": [externalized],
            "loaded_skills": {
                "hydro": "duplicate full skill",
                "calculator": "calculator rules",
            },
            "retrieved_evidence": ["obsolete evidence", "latest evidence"],
        }
    )

    skill_records = [record for record in records if record.get("category") == "skills"]
    retrieval_text = "\n".join(
        str(record.get("content") or "")
        for record in records
        if record.get("category") == "retrieval"
    )
    assert [record.get("source_id") for record in skill_records].count("hydro") == 1
    assert any(record.get("source_id") == "calculator" for record in skill_records)
    assert "latest evidence" in retrieval_text
    assert "obsolete evidence" not in retrieval_text


def test_artifact_store_writes_metadata_and_sanitizes_paths(tmp_path):
    store = ArtifactStore(tmp_path)
    raw_ref = store.store(
        "payload",
        thread_id="../thread one",
        source_tool="tool/name",
        suffix="../json",
        attributes={"trace_id": "trace-1"},
    )

    assert raw_ref.startswith("artifact://context/")
    assert store.read(raw_ref) == "payload"
    metadata = store.metadata(raw_ref)
    assert metadata is not None
    assert metadata.thread_id == "thread_one"
    assert metadata.source_tool == "tool_name"
    assert metadata.suffix == ".json"
    assert metadata.attributes["trace_id"] == "trace-1"
    assert store.read("artifact://context/../../etc/passwd") is None


def test_artifact_store_supports_bounded_reads(tmp_path):
    store = ArtifactStore(tmp_path)
    raw_ref = store.store("abcdef", thread_id="t1", source_tool="tool")

    assert store.read(raw_ref, max_chars=3) == "abc"


def test_context_engine_raw_materialization_fails_closed_across_users(tmp_path):
    store = ArtifactStore(tmp_path)
    raw_ref = store.store(
        "tenant-a-secret " * 100,
        thread_id="thread-1",
        source_tool="multi_kb_search",
        attributes={
            "owner_user_id": "user-a",
            "owner_thread_id": "thread-1",
        },
    )
    record = make_evidence_record(
        source_tool="multi_kb_search",
        content="bounded summary",
        category="retrieval",
        raw_ref=raw_ref,
    )
    engine = ContextEngine.create(
        model_name=None,
        user_id="user-b",
        thread_id="thread-1",
        artifact_store=store,
    )

    compiled = engine.compile_request(
        [record],
        messages=[HumanMessage(content="read it")],
    )

    assert "tenant-a-secret" not in compiled["pack"]["rendered"]
    assert raw_ref in compiled["pack"]["capabilities"]["artifact_refs"]


def test_tool_result_is_externalized_before_graph_checkpoint(tmp_path):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        large_result_threshold_chars=20,
        # 本测试断言 legacy 大结果外置路径;env 默认已是 on,显式钉 off。
        context_object_mode="off",
    )
    middleware = ContextPruningMiddleware(
        model_name=None,
        user_id="user-1",
        thread_id="thread-1",
        policy=policy,
    )
    middleware._context_engine = ContextEngine.create(
        model_name=None,
        user_id="user-1",
        thread_id="thread-1",
        policy=policy,
        artifact_store=store,
    )
    middleware._context_ledger = middleware._context_engine.ledger
    middleware._capture_registry = middleware._context_engine.capture_registry
    request = ToolCallRequest(
        tool_call={
            "name": "multi_kb_search",
            "id": "call-1",
            "args": {"query": "标准"},
        },
        tool=None,
        state={"messages": [HumanMessage(content="标准是什么", id="turn-1")]},
        runtime=None,
    )
    raw_content = "### 片段 1（来自：标准文档）\n限值为 17 mg/L。" + ("x" * 200)

    result = middleware.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content=raw_content,
            name="multi_kb_search",
            tool_call_id="call-1",
            artifact={"raw": "must-not-reach-checkpoint"},
        ),
    )

    assert isinstance(result, Command)
    assert isinstance(result.update, dict)
    replacement = result.update["messages"][0]
    assert raw_content not in replacement.content
    assert replacement.content.startswith("[Context Isolation:")
    assert replacement.artifact is None
    assert result.update["context_records"]
    assert result.update["context_records"][0]["turn_id"] == "turn-1"
    assert result.update["context_records"][0]["raw_ref"].startswith(
        "artifact://context/"
    )


def test_command_tool_result_clears_artifact_before_checkpoint(tmp_path):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        large_result_threshold_chars=20,
    )
    middleware = ContextPruningMiddleware(
        model_name=None,
        user_id="user-1",
        thread_id="thread-1",
        policy=policy,
    )
    middleware._context_engine = ContextEngine.create(
        model_name=None,
        user_id="user-1",
        thread_id="thread-1",
        policy=policy,
        artifact_store=store,
    )
    middleware._context_ledger = middleware._context_engine.ledger
    middleware._capture_registry = middleware._context_engine.capture_registry
    request = ToolCallRequest(
        tool_call={"name": "multi_kb_search", "id": "call-1", "args": {}},
        tool=None,
        state={"messages": [HumanMessage(content="question", id="turn-1")]},
        runtime=None,
    )
    raw_content = "retrieval result " + ("x" * 200)

    result = middleware.wrap_tool_call(
        request,
        lambda _request: Command(
            update={
                "messages": [
                    ToolMessage(
                        content=raw_content,
                        name="multi_kb_search",
                        tool_call_id="call-1",
                        artifact={"raw": "must-not-reach-checkpoint"},
                    )
                ],
                "custom": "preserved",
            }
        ),
    )

    assert isinstance(result, Command)
    assert result.update["messages"][0].artifact is None
    assert raw_content not in result.update["messages"][0].content
    assert result.update["custom"] == "preserved"
    assert result.update["context_records"]


def test_context_engine_shares_token_calibration_with_history_compactor():
    engine = ContextEngine.create(model_name="qwen-max-latest")

    assert engine.ledger._counter is engine.history_compactor._counter


def test_context_overflow_recompiles_once_with_larger_safety_factor():
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    middleware = ContextPruningMiddleware(model_name="qwen-max-latest")
    messages = [HumanMessage(content="answer the current question", id="turn-1")]
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=messages,
        system_message=SystemMessage(content="system"),
        tools=[],
        tool_choice=None,
        state={"messages": messages, "context_records": []},
        runtime=None,
    )
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("context_length_exceeded: prompt is too long")
        return ModelResponse(
            result=[
                AIMessage(
                    content="ok",
                    usage_metadata={
                        "input_tokens": 100,
                        "output_tokens": 1,
                        "total_tokens": 101,
                    },
                )
            ]
        )

    response = middleware.wrap_model_call(request, handler)

    from langchain.agents.middleware.types import ExtendedModelResponse

    assert isinstance(response, ExtendedModelResponse)
    assert isinstance(response.model_response, ModelResponse)
    assert calls == 2


def test_artifact_store_records_metadata_when_enabled(tmp_path, monkeypatch):
    captured = []

    def fake_record(metadata, *, preview=None):
        captured.append((metadata, preview))

    monkeypatch.setenv("CONTEXT_ARTIFACT_METADATA_DB", "1")
    monkeypatch.setattr(
        "backend.core.agents.context.artifact_store.record_context_artifact_metadata",
        fake_record,
    )

    store = ArtifactStore(tmp_path)
    raw_ref = store.store(
        "abcdef",
        thread_id="thread-1",
        source_tool="tool",
        attributes={"run_id": "run-1"},
    )

    assert captured
    assert captured[0][0].raw_ref == raw_ref
    assert captured[0][0].attributes["run_id"] == "run-1"
    assert captured[0][1] == "abcdef"


def test_unscoped_read_context_artifact_tool_denies_access(tmp_path):
    from backend.application.tools.providers import context_artifact_tool

    store = ArtifactStore(tmp_path)
    raw_ref = store.store("abcdef", thread_id="thread-1", source_tool="tool")

    payload = json.loads(
        context_artifact_tool.read_context_artifact.invoke(
            {"raw_ref": raw_ref, "max_chars": 3}
        )
    )

    assert payload["error"] == "artifact_access_denied"
    assert payload["reason"] == "runtime_identity_missing"
    assert payload["raw_ref"] == raw_ref


def test_read_context_artifact_rejects_web_url_before_store_access(monkeypatch):
    from backend.application.tools.providers import context_artifact_tool

    def unexpected_store_access():
        raise AssertionError("invalid URL must be rejected before artifact-store access")

    monkeypatch.setattr(
        context_artifact_tool,
        "create_context_artifact_store",
        unexpected_store_access,
    )

    payload = json.loads(
        context_artifact_tool.read_context_artifact.invoke(
            {"raw_ref": "https://example.com/source"}
        )
    )

    assert payload["error"] == "invalid_artifact_ref"
    assert "artifact://context/" in payload["instruction"]
    assert "raw_ref" not in payload


def test_read_context_artifact_rejects_context_record_id_as_raw_ref():
    from backend.application.tools.providers import context_artifact_tool

    payload = json.loads(
        context_artifact_tool.read_context_artifact.invoke(
            {"raw_ref": "artifact://context/ev_3e98254cf63c"}
        )
    )

    assert payload["error"] == "invalid_artifact_ref"
    assert payload["reason"] == "context_record_id_is_not_an_artifact_ref"
    assert "artifact://context/" in payload["instruction"]


def test_read_context_artifact_rejects_raw_window_id_as_raw_ref():
    from backend.application.tools.providers import context_artifact_tool

    payload = json.loads(
        context_artifact_tool.read_context_artifact.invoke(
            {"raw_ref": "artifact://context/raw_1d54fab81c89"}
        )
    )

    assert payload["error"] == "invalid_artifact_ref"
    assert payload["reason"] == "context_record_id_is_not_an_artifact_ref"


def test_scoped_artifact_reader_consumes_failed_call_budget():
    from backend.application.tools.providers import context_artifact_tool
    from backend.core.tools.request_limits import (
        get_tool_budget_remaining,
        reset_tool_budgets,
        set_tool_budgets,
    )

    tool = context_artifact_tool.get_scoped_read_context_artifact_tool(None)
    budget_token = set_tool_budgets({"read_context_artifact": 1})
    try:
        first = json.loads(
            tool.invoke({"raw_ref": "artifact://context/raw_1d54fab81c89"})
        )
        assert get_tool_budget_remaining("read_context_artifact") == 0
        second = json.loads(
            tool.invoke({"raw_ref": "artifact://context/raw_1d54fab81c89"})
        )
    finally:
        reset_tool_budgets(budget_token)

    assert first["error"] == "invalid_artifact_ref"
    assert second["error"] == "tool_budget_exhausted"


def test_scoped_read_context_artifact_tool_fails_closed_for_other_user(
    tmp_path, monkeypatch
):
    from backend.application.tools.providers import context_artifact_tool
    from backend.domain.tools.runtime_context import ToolRuntimeContext

    store = ArtifactStore(tmp_path)
    raw_ref = store.store(
        "secret payload",
        thread_id="user-1:thread-1",
        source_tool="tool",
        attributes={
            "owner_user_id": "user-1",
            "owner_thread_id": "user-1:thread-1",
        },
    )
    monkeypatch.setattr(
        context_artifact_tool,
        "create_context_artifact_store",
        lambda: store,
    )

    owner_context = ToolRuntimeContext(
        request_id="req-1",
        user_id="user-1",
        agent_code="ne",
        domain="nuclear-electronics",
        thread_id="user-1:thread-1",
    )
    other_context = ToolRuntimeContext(
        request_id="req-2",
        user_id="user-2",
        agent_code="ne",
        domain="nuclear-electronics",
        thread_id="user-2:thread-1",
    )

    owner_payload = json.loads(
        context_artifact_tool.get_scoped_read_context_artifact_tool(
            owner_context
        ).invoke({"raw_ref": raw_ref})
    )
    other_payload = json.loads(
        context_artifact_tool.get_scoped_read_context_artifact_tool(
            other_context
        ).invoke({"raw_ref": raw_ref})
    )

    assert owner_payload["content"] == "secret payload"
    assert other_payload["error"] == "artifact_access_denied"


def test_minio_context_artifact_store_round_trips_with_metadata(monkeypatch):
    objects: dict[tuple[str, str], bytes] = {}

    def fake_upload(data, object_name, *, content_type=None, bucket_name=None):
        objects[(bucket_name, object_name)] = bytes(data)
        return True

    class FakeResponse:
        def __init__(self, payload: bytes):
            self._payload = payload

        def read(self):
            return self._payload

        def close(self):
            pass

        def release_conn(self):
            pass

    class FakeClient:
        def get_object(self, bucket, object_name):
            return FakeResponse(objects[(bucket, object_name)])

    monkeypatch.setattr("backend.storage.get_user_minio_bucket_name", lambda: "bucket-a")
    monkeypatch.setattr("backend.storage.upload_bytes_to_minio_sync", fake_upload)
    monkeypatch.setattr("backend.storage._get_minio_client", lambda: FakeClient())

    store = MinioContextArtifactStore(prefix="ctx")
    raw_ref = store.store(
        "abcdef",
        thread_id="thread-1",
        source_tool="tool-x",
        attributes={"run_id": "run-1"},
    )

    assert raw_ref.startswith("artifact://context/minio/bucket-a/ctx/thread-1/")
    assert store.read(raw_ref, max_chars=3) == "abc"
    metadata = store.metadata(raw_ref)
    assert metadata is not None
    assert metadata.attributes["storage"] == "minio"
    assert metadata.attributes["run_id"] == "run-1"


def test_handoff_bundle_contains_results_not_execution_transcript():
    handoff = build_subagent_result(
        role="researcher",
        objective="核验标准",
        answer="结论：标准有效。来源：https://example.com/standard",
    )

    rendered = format_handoff_bundle([handoff])

    assert '<agent_handoff role="researcher">' in rendered
    assert "https://example.com/standard" in rendered
    assert "tool_start" not in rendered
    assert "tool_calls" not in rendered


def test_subagent_handoff_v2_contains_machine_readable_contract():
    handoff = build_subagent_result(
        role="researcher",
        task_id="task-1",
        objective="核验限值",
        answer="结论：标准 A 的限值为 17 mg/L。",
        evidence_records=[
            {
                "source_id": "kb-a",
                "chunk_id": "chunk-1",
                "doc_title": "标准 A",
                "claim": "标准 A 的限值为 17 mg/L",
            }
        ],
        raw_artifact_refs=["artifact://context/thread/tool-raw.txt"],
    )

    rendered = format_subagent_handoff(handoff)
    payload_match = re.search(
        r"<subagent_result_json>\s*(\{.*?\})\s*</subagent_result_json>",
        rendered,
        re.S,
    )

    assert handoff["schema_version"] == "2.0"
    assert handoff["task_id"] == "task-1"
    assert handoff["source_coverage"]["total_claims"] >= 1
    assert handoff["source_coverage"]["kb_refs"] >= 1
    assert handoff["source_coverage"]["raw_artifact_refs"] == 1
    assert payload_match is not None
    payload = json.loads(payload_match.group(1))
    assert payload["schema_version"] == "2.0"
    assert payload["task_id"] == "task-1"
    assert payload["source_coverage"]["kb_refs"] >= 1


def test_normalize_subagent_result_accepts_fenced_json_payload():
    handoff = normalize_subagent_result(
        """
```json
{
  "schema_version": "2.0",
  "task_id": "task-json",
  "summary": "已核验标准。",
  "evidence_refs": ["https://example.com/std"],
  "claims": [
    {
      "claim": "标准已发布。",
      "supporting_refs": ["https://example.com/std"],
      "confidence": 0.8
    }
  ],
  "confidence": 0.8
}
```
""",
        role="researcher",
        objective="核验标准",
        answer_fallback="fallback",
    )

    assert handoff["schema_version"] == "2.0"
    assert handoff["task_id"] == "task-json"
    assert handoff["claims"][0]["supporting_refs"] == ["https://example.com/std"]
    assert handoff["source_coverage"]["web_refs"] == 1
    assert handoff["validation_warnings"] == []
