"""Evidence-backed acceptance metrics for persisted agent run traces.

The evaluator only derives values represented by ``AgentRunRecord`` fields.
Cancellation propagation and worker recovery require process-level telemetry,
so those metrics remain unsampled unless callers inject explicit observations.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from backend.application.agent_trace.models import AgentRunRecord

_START_STATUSES = frozenset({"running", "started"})
_SUCCESS_STATUSES = frozenset({"completed", "success", "succeeded"})
_TERMINAL_STATUSES = _SUCCESS_STATUSES | frozenset(
    {
        "cancelled",
        "canceled",
        "failed",
        "interrupted",
        "timed_out",
        "timeout",
    }
)
_FAILED_HANDOFF_STATUSES = frozenset(
    {"cancelled", "canceled", "failed", "timed_out", "timeout"}
)


@dataclass(frozen=True)
class SampledRate:
    """A ratio plus the explicit denominator used to derive it."""

    value: float | None
    sample_count: int
    matching_count: int


@dataclass(frozen=True)
class SampledCount:
    """A count and the number of independent scopes inspected."""

    value: int
    sample_count: int


@dataclass(frozen=True)
class SampledAverage:
    """An average, number of observed values, and their total."""

    value: float | None
    sample_count: int
    total: float | None


@dataclass(frozen=True)
class AgentTraceAcceptanceObservations:
    """External observations that cannot be recovered from one run trace."""

    cancellation_propagation_latencies_ms: tuple[float, ...] = ()
    worker_recovery_outcomes: tuple[bool, ...] = ()
    multi_worker_concurrency_samples: tuple[int, ...] = ()
    multi_worker_concurrency_limit: int | None = None

    def __post_init__(self) -> None:
        latencies = tuple(
            float(value) for value in self.cancellation_propagation_latencies_ms
        )
        if any(not math.isfinite(value) or value < 0 for value in latencies):
            raise ValueError(
                "cancellation propagation latencies must be finite and non-negative"
            )
        outcomes = tuple(self.worker_recovery_outcomes)
        if any(not isinstance(value, bool) for value in outcomes):
            raise TypeError("worker recovery outcomes must contain only booleans")
        concurrency_samples = tuple(self.multi_worker_concurrency_samples)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in concurrency_samples
        ):
            raise ValueError(
                "multi-worker concurrency samples must be non-negative integers"
            )
        concurrency_limit = self.multi_worker_concurrency_limit
        if concurrency_limit is not None and (
            isinstance(concurrency_limit, bool)
            or not isinstance(concurrency_limit, int)
            or concurrency_limit < 1
        ):
            raise ValueError(
                "multi-worker concurrency limit must be a positive integer"
            )
        if concurrency_samples and concurrency_limit is None:
            raise ValueError(
                "multi-worker concurrency limit is required when samples are provided"
            )
        object.__setattr__(self, "cancellation_propagation_latencies_ms", latencies)
        object.__setattr__(self, "worker_recovery_outcomes", outcomes)
        object.__setattr__(
            self,
            "multi_worker_concurrency_samples",
            concurrency_samples,
        )


@dataclass(frozen=True)
class AgentTraceAcceptanceMetrics:
    """Acceptance metrics with sampling metadata for every inferred value."""

    trace_completeness_rate: SampledRate
    event_sequence_continuity_rate: SampledRate
    event_sequence_gap_count: SampledCount
    duplicate_tool_call_rate: SampledRate
    unsupported_claim_rate: SampledRate
    successful_answer_token_cost: SampledAverage
    successful_answer_tool_cost: SampledAverage
    partial_handoff_success_rate: SampledRate
    cancellation_propagation_latency_ms: SampledAverage
    worker_recovery_rate: SampledRate
    multi_worker_concurrency_violation_rate: SampledRate


class AgentTraceAcceptanceEvaluator:
    """Compute deterministic acceptance indicators from trace records.

    Definitions are intentionally conservative:

    * A trace is complete when a run has both a timestamped start snapshot and
      a timestamped terminal snapshot.
    * Event continuity is evaluated inside ``run + producer + unit`` streams.
      Only gaps between the smallest and largest observed sequence are known.
    * Duplicate tool calls are extra occurrences of the same
      ``tool_name + canonical JSON args`` within one run.
    * A claim is unsupported when its explicit ``supporting_refs`` is empty.
    * Successful-answer costs use successful terminal run snapshots. Token
      cost is unsampled when the recorder only contains its zero default.
    """

    def evaluate(
        self,
        records: Iterable[AgentRunRecord],
        *,
        observations: AgentTraceAcceptanceObservations | None = None,
    ) -> AgentTraceAcceptanceMetrics:
        materialized = tuple(records)
        if any(not isinstance(record, AgentRunRecord) for record in materialized):
            raise TypeError("records must contain only AgentRunRecord instances")

        records_by_run = self._records_by_run(materialized)
        trace_completeness = self._trace_completeness(records_by_run)
        sequence_continuity, sequence_gaps = self._event_sequence_metrics(materialized)
        duplicate_tools = self._duplicate_tool_calls(materialized)
        unsupported_claims = self._unsupported_claims(materialized)
        token_cost, tool_cost = self._successful_answer_costs(records_by_run)
        partial_handoffs = self._partial_handoff_success(materialized)
        cancellation_latency, worker_recovery, concurrency_violations = (
            self._observation_metrics(observations)
        )
        return AgentTraceAcceptanceMetrics(
            trace_completeness_rate=trace_completeness,
            event_sequence_continuity_rate=sequence_continuity,
            event_sequence_gap_count=sequence_gaps,
            duplicate_tool_call_rate=duplicate_tools,
            unsupported_claim_rate=unsupported_claims,
            successful_answer_token_cost=token_cost,
            successful_answer_tool_cost=tool_cost,
            partial_handoff_success_rate=partial_handoffs,
            cancellation_propagation_latency_ms=cancellation_latency,
            worker_recovery_rate=worker_recovery,
            multi_worker_concurrency_violation_rate=concurrency_violations,
        )

    @staticmethod
    def _records_by_run(
        records: tuple[AgentRunRecord, ...],
    ) -> dict[str, list[AgentRunRecord]]:
        grouped: dict[str, list[AgentRunRecord]] = defaultdict(list)
        for record in records:
            run_id = _text(record.payload.get("run_id"))
            if run_id:
                grouped[run_id].append(record)
        return dict(grouped)

    @staticmethod
    def _trace_completeness(
        records_by_run: Mapping[str, list[AgentRunRecord]],
    ) -> SampledRate:
        complete = 0
        for records in records_by_run.values():
            run_payloads = [
                record.payload for record in records if record.kind == "run"
            ]
            has_start = any(
                _status(payload) in _START_STATUSES
                and bool(_text(payload.get("started_at")))
                for payload in run_payloads
            )
            has_terminal = any(
                _status(payload) in _TERMINAL_STATUSES
                and bool(_text(payload.get("ended_at")))
                for payload in run_payloads
            )
            complete += int(has_start and has_terminal)
        return _rate(complete, len(records_by_run))

    @staticmethod
    def _event_sequence_metrics(
        records: tuple[AgentRunRecord, ...],
    ) -> tuple[SampledRate, SampledCount]:
        streams: dict[tuple[str, str, str], set[int]] = defaultdict(set)
        for record in records:
            if record.kind != "event":
                continue
            payload = record.payload
            run_id = _text(payload.get("run_id"))
            sequence = payload.get("sequence")
            if (
                not run_id
                or isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 0
            ):
                continue
            key = (
                run_id,
                _text(payload.get("producer")) or "legacy",
                _text(payload.get("unit_id")),
            )
            streams[key].add(sequence)

        observed_positions = 0
        expected_positions = 0
        gap_count = 0
        for sequences in streams.values():
            span = max(sequences) - min(sequences) + 1
            observed_positions += len(sequences)
            expected_positions += span
            gap_count += span - len(sequences)
        return (
            _rate(observed_positions, expected_positions),
            SampledCount(value=gap_count, sample_count=len(streams)),
        )

    @staticmethod
    def _duplicate_tool_calls(
        records: tuple[AgentRunRecord, ...],
    ) -> SampledRate:
        signatures: list[tuple[str, str, str]] = []
        for record in records:
            if record.kind != "tool_call":
                continue
            payload = record.payload
            run_id = _text(payload.get("run_id"))
            tool_name = _text(payload.get("tool_name"))
            args = payload.get("args_preview")
            if not run_id or not tool_name or not isinstance(args, Mapping):
                continue
            canonical_args = _canonical_json(args)
            if canonical_args is not None:
                signatures.append((run_id, tool_name, canonical_args))

        counts = Counter(signatures)
        duplicate_count = sum(count - 1 for count in counts.values())
        return _rate(duplicate_count, len(signatures))

    @staticmethod
    def _unsupported_claims(
        records: tuple[AgentRunRecord, ...],
    ) -> SampledRate:
        unsupported = 0
        total = 0
        for record in records:
            if record.kind != "handoff":
                continue
            payload = _handoff_payload(record.payload)
            claims = payload.get("claims")
            valid_claims = (
                [claim for claim in claims if isinstance(claim, Mapping)]
                if isinstance(claims, list)
                else []
            )
            if valid_claims:
                total += len(valid_claims)
                unsupported += sum(
                    1
                    for claim in valid_claims
                    if not _has_supporting_refs(claim.get("supporting_refs"))
                )
                continue

            coverage = payload.get("source_coverage")
            if not isinstance(coverage, Mapping):
                continue
            total_claims = _non_negative_int(coverage.get("total_claims"))
            unsupported_claims = _non_negative_int(coverage.get("unsupported_claims"))
            if (
                total_claims is None
                or unsupported_claims is None
                or unsupported_claims > total_claims
            ):
                continue
            total += total_claims
            unsupported += unsupported_claims
        return _rate(unsupported, total)

    @staticmethod
    def _successful_answer_costs(
        records_by_run: Mapping[str, list[AgentRunRecord]],
    ) -> tuple[SampledAverage, SampledAverage]:
        token_values: list[float] = []
        tool_values: list[float] = []
        for records in records_by_run.values():
            successful_run = next(
                (
                    record.payload
                    for record in reversed(records)
                    if record.kind == "run"
                    and _status(record.payload) in _SUCCESS_STATUSES
                ),
                None,
            )
            if successful_run is None:
                continue

            token_count = _positive_number(successful_run.get("total_tokens_estimated"))
            if token_count is not None:
                token_values.append(token_count)

            traced_tool_count = sum(
                1 for record in records if record.kind == "tool_call"
            )
            declared_tool_count = _non_negative_int(
                successful_run.get("total_tool_calls")
            )
            if declared_tool_count is not None and declared_tool_count > 0:
                tool_values.append(float(declared_tool_count))
            elif traced_tool_count:
                tool_values.append(float(traced_tool_count))
            elif declared_tool_count == 0:
                tool_values.append(0.0)

        return (
            _average(token_values),
            _average(tool_values, empty_value=0.0),
        )

    @staticmethod
    def _partial_handoff_success(
        records: tuple[AgentRunRecord, ...],
    ) -> SampledRate:
        candidates = [
            _handoff_payload(record.payload)
            for record in records
            if record.kind == "handoff"
        ]
        if not candidates:
            for record in records:
                if record.kind != "event":
                    continue
                event_type = _text(record.payload.get("event_type"))
                event_payload = record.payload.get("payload")
                if event_type != "coordination.partial_handoff" or not isinstance(
                    event_payload, Mapping
                ):
                    continue
                handoff = event_payload.get("handoff")
                if isinstance(handoff, Mapping):
                    candidates.append(dict(handoff))

        successes = sum(1 for payload in candidates if _handoff_succeeded(payload))
        return _rate(successes, len(candidates))

    @staticmethod
    def _observation_metrics(
        observations: AgentTraceAcceptanceObservations | None,
    ) -> tuple[SampledAverage, SampledRate, SampledRate]:
        if observations is None:
            return _average([]), _rate(0, 0), _rate(0, 0)
        cancellation_latency = _average(
            list(observations.cancellation_propagation_latencies_ms)
        )
        recovery_outcomes = observations.worker_recovery_outcomes
        worker_recovery = _rate(sum(recovery_outcomes), len(recovery_outcomes))
        concurrency_samples = observations.multi_worker_concurrency_samples
        concurrency_limit = observations.multi_worker_concurrency_limit
        concurrency_violations = _rate(
            sum(
                sample > concurrency_limit
                for sample in concurrency_samples
                if concurrency_limit is not None
            ),
            len(concurrency_samples) if concurrency_limit is not None else 0,
        )
        return cancellation_latency, worker_recovery, concurrency_violations


def evaluate_acceptance_metrics(
    records: Iterable[AgentRunRecord],
    *,
    observations: AgentTraceAcceptanceObservations | None = None,
) -> AgentTraceAcceptanceMetrics:
    """Convenience wrapper around :class:`AgentTraceAcceptanceEvaluator`."""

    return AgentTraceAcceptanceEvaluator().evaluate(
        records,
        observations=observations,
    )


def _rate(matching_count: int, sample_count: int) -> SampledRate:
    return SampledRate(
        value=(matching_count / sample_count if sample_count else None),
        sample_count=sample_count,
        matching_count=matching_count,
    )


def _average(
    values: list[float],
    *,
    empty_value: float | None = None,
) -> SampledAverage:
    if not values:
        return SampledAverage(value=empty_value, sample_count=0, total=empty_value)
    total = float(sum(values))
    return SampledAverage(
        value=total / len(values),
        sample_count=len(values),
        total=total,
    )


def _status(payload: Mapping[str, Any]) -> str:
    return _text(payload.get("status")).lower()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical_json(value: Mapping[Any, Any]) -> str | None:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return None


def _handoff_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    nested = payload.get("payload")
    return dict(nested) if isinstance(nested, Mapping) else dict(payload)


def _has_supporting_refs(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(bool(_text(item)) for item in value)
    return False


def _handoff_succeeded(payload: Mapping[str, Any]) -> bool:
    explicit_success = payload.get("success")
    if isinstance(explicit_success, bool):
        return explicit_success
    if _status(payload) in _FAILED_HANDOFF_STATUSES:
        return False
    if bool(_text(payload.get("error"))):
        return False
    return bool(
        _text(payload.get("summary"))
        or _text(payload.get("answer"))
        or payload.get("claims")
        or payload.get("evidence_refs")
    )


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


__all__ = [
    "AgentTraceAcceptanceEvaluator",
    "AgentTraceAcceptanceMetrics",
    "AgentTraceAcceptanceObservations",
    "SampledAverage",
    "SampledCount",
    "SampledRate",
    "evaluate_acceptance_metrics",
]
