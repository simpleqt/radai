"""Offline evaluation helpers for JSONL agent traces."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.application.agent_trace.store import (
    AgentTraceStoreProtocol,
    create_agent_trace_store,
)


@dataclass(frozen=True)
class EvalCaseResult:
    case_id: str
    run_id: str
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvalSummary:
    dataset: str
    total: int
    passed: int
    failed: int
    cases: list[EvalCaseResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        if self.total <= 0:
            return 0.0
        return round(self.passed / self.total, 6)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": self.pass_rate,
            "cases": [asdict(case) for case in self.cases],
        }


class AgentTraceEvaluator:
    def __init__(self, store: AgentTraceStoreProtocol | None = None) -> None:
        self._store = store or create_agent_trace_store()

    def evaluate_dataset(self, dataset_path: str | Path) -> EvalSummary:
        path = Path(dataset_path)
        cases = [
            _parse_case(line, index)
            for index, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                start=1,
            )
        ]
        results = [self._evaluate_case(case) for case in cases if case]
        passed = sum(1 for result in results if result.passed)
        return EvalSummary(
            dataset=str(path),
            total=len(results),
            passed=passed,
            failed=len(results) - passed,
            cases=results,
        )

    def _evaluate_case(self, case: dict[str, Any]) -> EvalCaseResult:
        run_id = str(case.get("run_id") or "").strip()
        case_id = str(case.get("case_id") or run_id or "case")
        records = self._store.read_records(run_id) if run_id else []
        checks: dict[str, bool] = {}
        notes: list[str] = []
        run_payloads = [record.payload for record in records if record.kind == "run"]
        tool_names = {
            str(record.payload.get("tool_name") or "")
            for record in records
            if record.kind == "tool_call"
        }
        writer_outputs = [
            str(record.payload.get("payload", {}).get("answer_preview") or "")
            for record in records
            if record.kind == "event"
            and str(record.payload.get("event_type") or "") == "writer.output"
        ]
        final_status = str(
            (run_payloads[-1] if run_payloads else {}).get("status") or ""
        )
        expected_status = str(case.get("expected_status") or "").strip()
        if expected_status:
            checks["status"] = final_status == expected_status
        required_tools = {str(item) for item in case.get("required_tools") or []}
        if required_tools:
            checks["required_tools"] = required_tools.issubset(tool_names)
        min_handoffs = int(case.get("min_handoffs") or 0)
        if min_handoffs:
            checks["min_handoffs"] = (
                sum(1 for record in records if record.kind == "handoff") >= min_handoffs
            )
        min_context_packs = int(case.get("min_context_packs") or 0)
        if min_context_packs:
            checks["min_context_packs"] = (
                sum(1 for record in records if record.kind == "context_pack")
                >= min_context_packs
            )
        min_compactions = int(case.get("min_compactions") or 0)
        if min_compactions:
            checks["min_compactions"] = (
                sum(
                    1
                    for record in records
                    if record.kind == "compaction"
                    and str(record.payload.get("status") or "completed")
                    == "completed"
                )
                >= min_compactions
            )
        max_compaction_skips = case.get("max_compaction_skips")
        if max_compaction_skips is not None:
            checks["max_compaction_skips"] = (
                sum(
                    1
                    for record in records
                    if record.kind == "compaction"
                    and str(record.payload.get("status") or "completed") == "skipped"
                )
                <= int(max_compaction_skips)
            )
        expected_answer_contains = str(
            case.get("expected_answer_contains") or ""
        ).strip()
        if expected_answer_contains:
            checks["expected_answer_contains"] = any(
                expected_answer_contains in output for output in writer_outputs
            )
        if not records:
            notes.append("trace records not found")
        if not checks:
            checks["records_exist"] = bool(records)
        return EvalCaseResult(
            case_id=case_id,
            run_id=run_id,
            passed=all(checks.values()),
            checks=checks,
            notes=notes,
        )


def _parse_case(line: str, index: int) -> dict[str, Any]:
    text = line.strip()
    if not text or text.startswith("#"):
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid eval dataset JSONL at line {index}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Eval dataset line {index} must be a JSON object")
    return payload
