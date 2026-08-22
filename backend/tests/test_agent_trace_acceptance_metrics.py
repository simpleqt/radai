from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from backend.application.agent_trace.acceptance_metrics import (
    AgentTraceAcceptanceEvaluator,
    AgentTraceAcceptanceObservations,
    evaluate_acceptance_metrics,
)
from backend.application.agent_trace.models import (
    AgentRunEvent,
    AgentRunHandoff,
    AgentRunRecord,
    AgentRunToolCall,
    AgentRunTrace,
)
from backend.application.agent_trace.store import AgentTraceStore
from backend.cli import main as cli_main


def _record(kind, value) -> AgentRunRecord:
    return AgentRunRecord.from_dataclass(kind, value)


def test_evaluator_computes_acceptance_metrics_from_current_trace_models() -> None:
    records = [
        _record(
            "run",
            AgentRunTrace(
                run_id="run-1",
                request_id="request-1",
                user_id="user-1",
                thread_id="thread-1",
                agent_code="agent",
                workflow="deep_research",
                status="running",
                started_at="2026-01-01T00:00:00Z",
            ),
        ),
        _record(
            "run",
            AgentRunTrace(
                run_id="run-1",
                request_id="request-1",
                user_id="user-1",
                thread_id="thread-1",
                agent_code="agent",
                workflow="deep_research",
                status="completed",
                started_at="2026-01-01T00:00:00Z",
                ended_at="2026-01-01T00:01:00Z",
                total_tokens_estimated=120,
                total_tool_calls=3,
            ),
        ),
        _record(
            "run",
            AgentRunTrace(
                run_id="run-2",
                request_id="request-2",
                user_id="user-2",
                thread_id="thread-2",
                agent_code="agent",
                workflow="multi_agent_parallel",
                status="running",
                started_at="2026-01-01T00:00:00Z",
            ),
        ),
        _record(
            "event",
            AgentRunEvent(
                run_id="run-1",
                sequence=0,
                event_type="workflow.started",
                producer="workflow",
            ),
        ),
        _record(
            "event",
            AgentRunEvent(
                run_id="run-1",
                sequence=2,
                event_type="workflow.completed",
                producer="workflow",
            ),
        ),
        _record(
            "event",
            AgentRunEvent(
                run_id="run-1",
                sequence=10,
                event_type="sse.message.delta",
                producer="agent-sse",
            ),
        ),
        _record(
            "event",
            AgentRunEvent(
                run_id="run-1",
                sequence=11,
                event_type="sse.done",
                producer="agent-sse",
            ),
        ),
        _record(
            "tool_call",
            AgentRunToolCall(
                tool_call_id="tool-1",
                run_id="run-1",
                tool_name="search",
                status="completed",
                args_preview={"query": "alpha", "filters": {"b": 2, "a": 1}},
            ),
        ),
        _record(
            "tool_call",
            AgentRunToolCall(
                tool_call_id="tool-2",
                run_id="run-1",
                tool_name="search",
                status="completed",
                args_preview={"filters": {"a": 1, "b": 2}, "query": "alpha"},
            ),
        ),
        _record(
            "tool_call",
            AgentRunToolCall(
                tool_call_id="tool-3",
                run_id="run-1",
                tool_name="search",
                status="completed",
                args_preview={"query": "beta"},
            ),
        ),
        _record(
            "tool_call",
            AgentRunToolCall(
                tool_call_id="tool-4",
                run_id="run-2",
                tool_name="search",
                status="completed",
                args_preview={"filters": {"a": 1, "b": 2}, "query": "alpha"},
            ),
        ),
        _record(
            "handoff",
            AgentRunHandoff(
                handoff_id="handoff-1",
                run_id="run-1",
                unit_id="unit-1",
                role="researcher",
                payload={
                    "summary": "usable partial result",
                    "claims": [
                        {"claim": "supported", "supporting_refs": ["kb:1"]},
                        {"claim": "unsupported", "supporting_refs": []},
                    ],
                },
            ),
        ),
        _record(
            "handoff",
            AgentRunHandoff(
                handoff_id="handoff-2",
                run_id="run-2",
                unit_id="unit-2",
                role="researcher",
                payload={
                    "summary": "",
                    "error": "branch failed",
                    "source_coverage": {
                        "total_claims": 3,
                        "unsupported_claims": 2,
                    },
                },
            ),
        ),
    ]
    observations = AgentTraceAcceptanceObservations(
        cancellation_propagation_latencies_ms=(12, 18),
        worker_recovery_outcomes=(True, False, True),
        multi_worker_concurrency_samples=(2, 4, 5, 3),
        multi_worker_concurrency_limit=4,
    )

    metrics = AgentTraceAcceptanceEvaluator().evaluate(
        iter(records),
        observations=observations,
    )

    assert metrics.trace_completeness_rate.value == 0.5
    assert metrics.trace_completeness_rate.sample_count == 2
    assert metrics.trace_completeness_rate.matching_count == 1

    assert metrics.event_sequence_continuity_rate.value == 0.8
    assert metrics.event_sequence_continuity_rate.sample_count == 5
    assert metrics.event_sequence_continuity_rate.matching_count == 4
    assert metrics.event_sequence_gap_count.value == 1
    assert metrics.event_sequence_gap_count.sample_count == 2

    assert metrics.duplicate_tool_call_rate.value == 0.25
    assert metrics.duplicate_tool_call_rate.sample_count == 4
    assert metrics.duplicate_tool_call_rate.matching_count == 1

    assert metrics.unsupported_claim_rate.value == 0.6
    assert metrics.unsupported_claim_rate.sample_count == 5
    assert metrics.unsupported_claim_rate.matching_count == 3

    assert metrics.successful_answer_token_cost.value == 120
    assert metrics.successful_answer_token_cost.sample_count == 1
    assert metrics.successful_answer_token_cost.total == 120
    assert metrics.successful_answer_tool_cost.value == 3
    assert metrics.successful_answer_tool_cost.sample_count == 1
    assert metrics.successful_answer_tool_cost.total == 3

    assert metrics.partial_handoff_success_rate.value == 0.5
    assert metrics.partial_handoff_success_rate.sample_count == 2
    assert metrics.partial_handoff_success_rate.matching_count == 1

    assert metrics.cancellation_propagation_latency_ms.value == 15
    assert metrics.cancellation_propagation_latency_ms.sample_count == 2
    assert metrics.worker_recovery_rate.value == pytest.approx(2 / 3)
    assert metrics.worker_recovery_rate.sample_count == 3
    assert metrics.multi_worker_concurrency_violation_rate.value == 0.25
    assert metrics.multi_worker_concurrency_violation_rate.sample_count == 4


def test_missing_trace_fields_are_unsampled_instead_of_inferred() -> None:
    records = [
        AgentRunRecord(
            kind="run",
            payload={
                "run_id": "run-incomplete",
                "status": "completed",
                "ended_at": "2026-01-01T00:01:00Z",
            },
        ),
        AgentRunRecord(
            kind="event",
            payload={"run_id": "run-incomplete", "event_type": "done"},
        ),
        AgentRunRecord(
            kind="tool_call",
            payload={"run_id": "run-incomplete", "tool_name": "search"},
        ),
    ]

    metrics = evaluate_acceptance_metrics(records)

    assert metrics.trace_completeness_rate.value == 0
    assert metrics.trace_completeness_rate.sample_count == 1
    assert metrics.event_sequence_continuity_rate.value is None
    assert metrics.event_sequence_continuity_rate.sample_count == 0
    assert metrics.event_sequence_gap_count.value == 0
    assert metrics.event_sequence_gap_count.sample_count == 0
    assert metrics.duplicate_tool_call_rate.value is None
    assert metrics.duplicate_tool_call_rate.sample_count == 0
    assert metrics.unsupported_claim_rate.value is None
    assert metrics.unsupported_claim_rate.sample_count == 0
    assert metrics.successful_answer_token_cost.value is None
    assert metrics.successful_answer_token_cost.sample_count == 0
    assert metrics.successful_answer_tool_cost.value == 1
    assert metrics.successful_answer_tool_cost.sample_count == 1
    assert metrics.partial_handoff_success_rate.value is None
    assert metrics.partial_handoff_success_rate.sample_count == 0
    assert metrics.cancellation_propagation_latency_ms.value is None
    assert metrics.cancellation_propagation_latency_ms.sample_count == 0
    assert metrics.worker_recovery_rate.value is None
    assert metrics.worker_recovery_rate.sample_count == 0
    assert metrics.multi_worker_concurrency_violation_rate.value is None
    assert metrics.multi_worker_concurrency_violation_rate.sample_count == 0


def test_empty_trace_returns_none_or_zero_with_zero_sample_counts() -> None:
    metrics = evaluate_acceptance_metrics([])

    assert metrics.trace_completeness_rate.value is None
    assert metrics.trace_completeness_rate.sample_count == 0
    assert metrics.event_sequence_continuity_rate.value is None
    assert metrics.event_sequence_gap_count.value == 0
    assert metrics.duplicate_tool_call_rate.value is None
    assert metrics.unsupported_claim_rate.value is None
    assert metrics.successful_answer_token_cost.value is None
    assert metrics.successful_answer_tool_cost.value == 0
    assert metrics.successful_answer_tool_cost.sample_count == 0
    assert metrics.partial_handoff_success_rate.value is None
    assert metrics.multi_worker_concurrency_violation_rate.value is None
    assert metrics.cancellation_propagation_latency_ms.value is None
    assert metrics.worker_recovery_rate.value is None


def test_metrics_and_observations_are_frozen_and_observations_are_validated() -> None:
    observations = AgentTraceAcceptanceObservations(
        cancellation_propagation_latencies_ms=[1, 2],  # type: ignore[arg-type]
        worker_recovery_outcomes=[True],  # type: ignore[arg-type]
    )
    metrics = evaluate_acceptance_metrics([], observations=observations)

    assert observations.cancellation_propagation_latencies_ms == (1.0, 2.0)
    assert observations.worker_recovery_outcomes == (True,)
    with pytest.raises(FrozenInstanceError):
        metrics.trace_completeness_rate.value = 1.0  # type: ignore[misc]
    with pytest.raises(ValueError, match="non-negative"):
        AgentTraceAcceptanceObservations(
            cancellation_propagation_latencies_ms=(-1,),
        )
    with pytest.raises(TypeError, match="booleans"):
        AgentTraceAcceptanceObservations(
            worker_recovery_outcomes=(1,),  # type: ignore[arg-type]
        )


def test_acceptance_metrics_cli_reads_trace_and_external_observations(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    trace_root = tmp_path / "traces"
    store = AgentTraceStore(trace_root)
    store.append_run(
        AgentRunTrace(
            run_id="run-cli",
            request_id="request-cli",
            user_id="user-cli",
            thread_id="thread-cli",
            agent_code="agent",
            workflow="full",
            status="running",
            started_at="2026-01-01T00:00:00Z",
        )
    )
    store.append_run(
        AgentRunTrace(
            run_id="run-cli",
            request_id="request-cli",
            user_id="user-cli",
            thread_id="thread-cli",
            agent_code="agent",
            workflow="full",
            status="completed",
            started_at="2026-01-01T00:00:00Z",
            ended_at="2026-01-01T00:01:00Z",
        )
    )
    observations_path = tmp_path / "observations.json"
    observations_path.write_text(
        '{"worker_recovery_outcomes":[true,true,false],'
        '"multi_worker_concurrency_samples":[2,5],'
        '"multi_worker_concurrency_limit":4}',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_TRACE_BACKEND", "jsonl")

    exit_code = cli_main(
        [
            "agent",
            "acceptance-metrics",
            "--trace-root",
            str(trace_root),
            "--run-id",
            "run-cli",
            "--observations-json",
            str(observations_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_ids"] == ["run-cli"]
    assert payload["metrics"]["trace_completeness_rate"]["value"] == 1
    assert payload["metrics"]["worker_recovery_rate"]["sample_count"] == 3
    assert payload["metrics"]["multi_worker_concurrency_violation_rate"]["value"] == 0.5
