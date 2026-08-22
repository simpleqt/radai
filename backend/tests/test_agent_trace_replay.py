import json

import pytest

from backend.application.agent_trace import (
    AgentRunCompaction,
    AgentRunContextPack,
    AgentRunEvent,
    AgentRunHandoff,
    AgentRunReplay,
    AgentRunToolCall,
    AgentRunTrace,
    AgentRunUnit,
    AgentTraceStore,
    AgentTraceRecorder,
    EvalHistoryStore,
)
from backend.domain.agents.events import AgentEvent
from backend.cli import main


def _write_sample_trace(store: AgentTraceStore, run_id: str = "run-1") -> None:
    store.append_run(
        AgentRunTrace(
            run_id=run_id,
            request_id="req-1",
            user_id="user-1",
            thread_id="thread-1",
            agent_code="knowledge-qna",
            workflow="plan_solve",
            status="completed",
        )
    )
    store.append_unit(
        AgentRunUnit(
            unit_id="unit-1",
            run_id=run_id,
            role="researcher",
            agent_type="deep-research",
            task_title="核验标准",
            task_question="标准 A 的限值是多少？",
            status="completed",
        )
    )
    store.append_unit(
        AgentRunUnit(
            unit_id="unit-2",
            run_id=run_id,
            role="analyst",
            agent_type="general-purpose",
            task_title="汇总结论",
            task_question="汇总标准 A 的限值证据。",
            status="completed",
        )
    )
    store.append_event(
        AgentRunEvent(
            run_id=run_id,
            sequence=1,
            event_type="handoff.created",
            unit_id="unit-1",
            payload={"ok": True},
        )
    )
    store.append_event(
        AgentRunEvent(
            run_id=run_id,
            sequence=2,
            event_type="unit.completed",
            unit_id="unit-2",
            payload={"ok": True},
        )
    )
    store.append_tool_call(
        AgentRunToolCall(
            tool_call_id="tool-1",
            run_id=run_id,
            tool_name="multi_kb_search",
            status="completed",
            unit_id="unit-1",
            args_preview={"query": "标准 A"},
            result_preview="标准 A 的限值为 17 mg/L。",
        )
    )
    store.append_context_pack(
        AgentRunContextPack(
            pack_id="pack-1",
            run_id=run_id,
            unit_id="unit-1",
            model_call_index=1,
            base_tokens=100,
            dynamic_token_budget=200,
            dynamic_tokens_used=80,
            items_total=3,
            items_injected=2,
            selected_context_ids=["ctx-1", "ctx-2"],
            usage_by_category={"handoff": 40, "evidence": 40},
        )
    )
    store.append_compaction(
        AgentRunCompaction(
            compaction_id="compact-1",
            run_id=run_id,
            unit_id="unit-1",
            model_call_index=1,
            trigger="auto",
            window_id="ctxwin-1",
            previous_window_id=None,
            tokens_before=1200,
            tokens_after=420,
            summary_tokens=130,
            retained_user_messages=1,
            removed_messages=4,
        )
    )
    store.append_handoff(
        AgentRunHandoff(
            handoff_id="handoff-1",
            run_id=run_id,
            unit_id="unit-1",
            role="researcher",
            payload={"schema_version": "2.0", "summary": "标准 A 已核验。"},
        )
    )


def test_agent_trace_store_round_trips_and_replay_counts(tmp_path):
    store = AgentTraceStore(tmp_path)
    _write_sample_trace(store)

    records = store.read_records("run-1")

    assert [record.kind for record in records] == [
        "run",
        "unit",
        "unit",
        "event",
        "event",
        "tool_call",
        "context_pack",
        "compaction",
        "handoff",
    ]
    assert store.list_run_ids() == ["run-1"]

    summary = AgentRunReplay(store).dry_run("run-1")

    assert summary.run_id == "run-1"
    assert summary.mode == "dry-run"
    assert summary.status == "completed"
    assert summary.events == 2
    assert summary.units == 2
    assert summary.tool_calls == 1
    assert summary.context_packs == 1
    assert summary.compactions == 1
    assert summary.handoffs == 1
    assert summary.run["workflow"] == "plan_solve"

    cached = AgentRunReplay(store).replay("run-1", mode="tool-cache")
    assert cached.mode == "tool-cache"
    assert cached.cached_tool_results[0]["tool_name"] == "multi_kb_search"
    assert "model calls are not executed" in cached.replay_notes[0]


def test_trace_recorder_persists_v2_event_envelope_index_fields(tmp_path):
    store = AgentTraceStore(tmp_path)
    recorder = AgentTraceRecorder(
        run_id="run-envelope",
        workflow_name="plan_solve",
        enabled=True,
        store=store,
        request_id="request-1",
        thread_id="thread-1",
    )

    recorder.record_agent_event(
        AgentEvent(
            type="subagent.completed",
            request_id="request-1",
            thread_id="thread-1",
            sequence=3,
            payload={"unit_id": "unit-1", "ok": True},
        )
    )

    payload = store.read_records("run-envelope")[0].payload
    assert payload["schema_version"] == "2.0"
    assert payload["event_id"].startswith("evt_")
    assert payload["correlation_id"] == "run-envelope"
    assert payload["unit_id"] == "unit-1"
    assert payload["producer"] == "agent-workflow:plan_solve"
    assert payload["idempotency_key"] == payload["event_id"]


def test_agent_trace_replay_can_start_from_unit(tmp_path):
    store = AgentTraceStore(tmp_path)
    _write_sample_trace(store)

    summary = AgentRunReplay(store).dry_run("run-1", from_unit="unit-1")

    assert summary.selected_unit_id == "unit-1"
    assert summary.status == "completed"
    assert summary.units == 1
    assert summary.events == 1
    assert summary.tool_calls == 1
    assert summary.context_packs == 1
    assert summary.compactions == 1
    assert summary.handoffs == 1


def test_agent_trace_replay_distinguishes_completed_and_skipped_compactions(tmp_path):
    store = AgentTraceStore(tmp_path)
    _write_sample_trace(store)
    store.append_compaction(
        AgentRunCompaction(
            compaction_id="compact-skip-1",
            run_id="run-1",
            unit_id="unit-1",
            model_call_index=2,
            trigger="request_budget",
            window_id="ctxwin-1",
            previous_window_id="ctxwin-1",
            tokens_before=1000,
            tokens_after=1000,
            summary_tokens=0,
            retained_user_messages=0,
            removed_messages=0,
            status="skipped",
            error="no_token_savings",
        )
    )

    summary = AgentRunReplay(store).dry_run("run-1")

    assert summary.compactions == 1
    assert summary.compaction_attempts == 2
    assert summary.compaction_skipped == 1


def test_agent_trace_store_rejects_empty_or_traversal_run_ids(tmp_path):
    store = AgentTraceStore(tmp_path)

    with pytest.raises(ValueError, match="run_id"):
        store.path_for_run("")

    assert store.path_for_run("../run-1").parent == tmp_path


def test_agent_replay_cli_outputs_summary(tmp_path, capsys):
    store = AgentTraceStore(tmp_path)
    _write_sample_trace(store, run_id="cli-run")

    main(
        [
            "agent",
            "replay",
            "--run-id",
            "cli-run",
            "--trace-root",
            str(tmp_path),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output["run_id"] == "cli-run"
    assert output["mode"] == "dry-run"
    assert output["status"] == "completed"
    assert output["events"] == 2
    assert output["handoffs"] == 1


def test_agent_eval_cli_scores_trace_dataset(tmp_path, capsys):
    store = AgentTraceStore(tmp_path)
    _write_sample_trace(store, run_id="eval-run")
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "case_id": "case-1",
                "run_id": "eval-run",
                "expected_status": "completed",
                "required_tools": ["multi_kb_search"],
                "min_handoffs": 1,
                "min_context_packs": 1,
                "min_compactions": 1,
                "max_compaction_skips": 0,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    main(
        [
            "agent",
            "eval",
            "--dataset",
            str(dataset),
            "--trace-root",
            str(tmp_path),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output["total"] == 1
    assert output["passed"] == 1
    assert output["pass_rate"] == 1.0


def test_agent_eval_history_store_tracks_trends_and_compares_versions(tmp_path):
    store = AgentTraceStore(tmp_path / "traces")
    _write_sample_trace(store, run_id="eval-run")
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "case_id": "case-1",
                "run_id": "eval-run",
                "expected_status": "completed",
                "required_tools": ["multi_kb_search"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    from backend.application.agent_trace import AgentTraceEvaluator

    summary = AgentTraceEvaluator(store).evaluate_dataset(dataset)
    history = EvalHistoryStore(tmp_path / "history")
    history.append(summary, version="v1")

    failing_dataset = tmp_path / "dataset_failing.jsonl"
    failing_dataset.write_text(
        json.dumps(
            {
                "case_id": "case-1",
                "run_id": "eval-run",
                "expected_status": "failed",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    failing_summary = AgentTraceEvaluator(store).evaluate_dataset(failing_dataset)
    history.append(failing_summary, version="v2")

    trend = history.trend(limit=2)
    comparison = history.compare(baseline_version="v1", candidate_version="v2")

    assert [record.version for record in trend.records] == ["v1", "v2"]
    assert comparison.baseline.pass_rate == 1.0
    assert comparison.candidate.pass_rate == 0.0
    assert comparison.delta_pass_rate == -1.0


def test_agent_eval_cli_records_history_and_compares_versions(tmp_path, capsys):
    trace_root = tmp_path / "traces"
    history_root = tmp_path / "history"
    store = AgentTraceStore(trace_root)
    _write_sample_trace(store, run_id="cli-eval-run")
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "case_id": "case-1",
                "run_id": "cli-eval-run",
                "expected_status": "completed",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    main(
        [
            "agent",
            "eval",
            "--dataset",
            str(dataset),
            "--trace-root",
            str(trace_root),
            "--record",
            "--version",
            "v1",
            "--history-root",
            str(history_root),
        ]
    )
    recorded = json.loads(capsys.readouterr().out)

    main(
        [
            "agent",
            "eval-history",
            "--history-root",
            str(history_root),
        ]
    )
    history = json.loads(capsys.readouterr().out)

    main(
        [
            "agent",
            "eval-compare",
            "--history-root",
            str(history_root),
            "--baseline-version",
            "v1",
            "--candidate-version",
            "v1",
        ]
    )
    comparison = json.loads(capsys.readouterr().out)

    assert recorded["history_record"]["version"] == "v1"
    assert history["records"][0]["version"] == "v1"
    assert comparison["delta_pass_rate"] == 0.0


async def test_agent_runtime_trace_debug_endpoints_return_authorized_trace(tmp_path, monkeypatch):
    from backend.api.dependencies.auth import ApiKeyAuth
    from backend.api.routers import agent_runtime
    from backend.api.routers import agent_runtime_observability

    trace_root = tmp_path / "traces"
    store = AgentTraceStore(trace_root)
    _write_sample_trace(store, run_id="api-run")
    auth = ApiKeyAuth(api_key="test", user_id="user-1")
    monkeypatch.setattr(
        agent_runtime_observability,
        "AgentTraceStore",
        lambda root=None: AgentTraceStore(trace_root),
    )

    run = await agent_runtime.get_agent_run_trace(
        "api-run",
        auth=auth,
    )
    events = await agent_runtime.list_agent_run_events(
        "api-run",
        auth=auth,
    )
    units = await agent_runtime.list_agent_run_units(
        "api-run",
        auth=auth,
    )
    tool_calls = await agent_runtime.list_agent_run_tool_calls(
        "api-run",
        auth=auth,
    )
    context_packs = await agent_runtime.list_agent_run_context_packs(
        "api-run",
        auth=auth,
    )
    compactions = await agent_runtime.list_agent_run_compactions(
        "api-run",
        auth=auth,
    )
    handoffs = await agent_runtime.list_agent_run_handoffs(
        "api-run",
        auth=auth,
    )
    replay = await agent_runtime.replay_agent_run_trace(
        "api-run",
        mode="tool-cache",
        auth=auth,
    )

    assert run["run"]["run_id"] == "api-run"
    assert run["counts"]["events"] == 2
    assert run["counts"]["compactions"] == 1
    assert run["counts"]["compaction_attempts"] == 1
    assert run["counts"]["compaction_skipped"] == 0
    assert events["events"][0]["event_type"] == "handoff.created"
    assert units["units"][0]["role"] == "researcher"
    assert tool_calls["tool_calls"][0]["tool_name"] == "multi_kb_search"
    assert context_packs["context_packs"][0]["items_injected"] == 2
    assert compactions["compactions"][0]["window_id"] == "ctxwin-1"
    assert handoffs["handoffs"][0]["schema_version"] == "2.0"
    assert replay["mode"] == "tool-cache"
    assert replay["cached_tool_results"][0]["tool_name"] == "multi_kb_search"


async def test_agent_runtime_admin_inventory_filters_and_paginates(tmp_path, monkeypatch):
    from backend.api.dependencies.auth import ApiKeyAuth
    from backend.api.routers import agent_runtime
    from backend.api.routers import agent_runtime_observability

    trace_root = tmp_path / "traces"
    store = AgentTraceStore(trace_root)
    _write_sample_trace(store, run_id="inventory-run")
    monkeypatch.setattr(
        agent_runtime_observability,
        "AgentTraceStore",
        lambda root=None: AgentTraceStore(trace_root),
    )

    result = await agent_runtime.list_admin_agent_runs(
        limit=1,
        offset=0,
        status="completed",
        agent_code="knowledge-qna",
        user_id="user-1",
        q="inventory",
        auth=ApiKeyAuth(api_key="test", user_id="admin-1", role="admin"),
    )

    assert result["total"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["run_id"] == "inventory-run"
    assert result["items"][0]["counts"]["tool_calls"] == 1


async def test_agent_runtime_trace_debug_endpoints_hide_other_user_trace(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from backend.api.dependencies.auth import ApiKeyAuth
    from backend.api.routers import agent_runtime
    from backend.api.routers import agent_runtime_observability

    trace_root = tmp_path / "traces"
    store = AgentTraceStore(trace_root)
    _write_sample_trace(store, run_id="private-run")
    monkeypatch.setattr(
        agent_runtime_observability,
        "AgentTraceStore",
        lambda root=None: AgentTraceStore(trace_root),
    )

    with pytest.raises(HTTPException) as exc_info:
        await agent_runtime.get_agent_run_trace(
            "private-run",
            auth=ApiKeyAuth(api_key="test", user_id="user-2"),
        )

    assert exc_info.value.status_code == 404
