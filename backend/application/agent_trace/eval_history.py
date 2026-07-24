"""JSONL history for offline agent eval runs."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.application.agent_trace.eval import EvalSummary
from backend.application.agent_trace.models import utc_now_iso

DEFAULT_EVAL_HISTORY_ROOT = Path(
    os.getenv(
        "AGENT_EVAL_HISTORY_ROOT",
        str(Path(__file__).resolve().parents[2] / ".runtime" / "agent_eval"),
    )
)


@dataclass(frozen=True)
class EvalRunRecord:
    eval_id: str
    dataset: str
    version: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    created_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_summary(
        cls,
        summary: EvalSummary,
        *,
        version: str,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> "EvalRunRecord":
        created = created_at or utc_now_iso()
        return cls(
            eval_id=_build_eval_id(summary.dataset, version, created),
            dataset=summary.dataset,
            version=str(version or "unversioned"),
            total=summary.total,
            passed=summary.passed,
            failed=summary.failed,
            pass_rate=summary.pass_rate,
            created_at=created,
            metadata=dict(metadata or {}),
        )

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json_dict(cls, raw: dict[str, Any]) -> "EvalRunRecord":
        return cls(
            eval_id=str(raw.get("eval_id") or ""),
            dataset=str(raw.get("dataset") or ""),
            version=str(raw.get("version") or "unversioned"),
            total=int(raw.get("total") or 0),
            passed=int(raw.get("passed") or 0),
            failed=int(raw.get("failed") or 0),
            pass_rate=float(raw.get("pass_rate") or 0.0),
            created_at=str(raw.get("created_at") or ""),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True)
class EvalTrend:
    dataset: str | None
    version: str | None
    records: list[EvalRunRecord]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "version": self.version,
            "records": [record.to_json_dict() for record in self.records],
        }


@dataclass(frozen=True)
class EvalComparison:
    dataset: str | None
    baseline: EvalRunRecord
    candidate: EvalRunRecord
    delta_pass_rate: float
    delta_passed: int
    delta_failed: int

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "baseline": self.baseline.to_json_dict(),
            "candidate": self.candidate.to_json_dict(),
            "delta_pass_rate": self.delta_pass_rate,
            "delta_passed": self.delta_passed,
            "delta_failed": self.delta_failed,
        }


class EvalHistoryStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root or DEFAULT_EVAL_HISTORY_ROOT)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._root / "eval_runs.jsonl"

    def append(
        self,
        summary: EvalSummary,
        *,
        version: str,
        metadata: dict[str, Any] | None = None,
    ) -> EvalRunRecord:
        record = EvalRunRecord.from_summary(
            summary,
            version=version,
            metadata=metadata,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    record.to_json_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            handle.write("\n")
        return record

    def list_records(
        self,
        *,
        dataset: str | None = None,
        version: str | None = None,
        limit: int | None = None,
    ) -> list[EvalRunRecord]:
        if not self.path.is_file():
            return []
        records: list[EvalRunRecord] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("eval history row must be a mapping")
                record = EvalRunRecord.from_json_dict(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid eval history record at {self.path}:{line_number}: {exc}"
                ) from exc
            if dataset and record.dataset != dataset:
                continue
            if version and record.version != version:
                continue
            records.append(record)
        records.sort(key=lambda item: item.created_at)
        if limit is not None and limit > 0:
            return records[-limit:]
        return records

    def trend(
        self,
        *,
        dataset: str | None = None,
        version: str | None = None,
        limit: int | None = None,
    ) -> EvalTrend:
        return EvalTrend(
            dataset=dataset,
            version=version,
            records=self.list_records(dataset=dataset, version=version, limit=limit),
        )

    def compare(
        self,
        *,
        baseline_version: str,
        candidate_version: str,
        dataset: str | None = None,
    ) -> EvalComparison:
        baseline = self._latest_record(version=baseline_version, dataset=dataset)
        candidate = self._latest_record(version=candidate_version, dataset=dataset)
        return EvalComparison(
            dataset=dataset,
            baseline=baseline,
            candidate=candidate,
            delta_pass_rate=round(candidate.pass_rate - baseline.pass_rate, 6),
            delta_passed=candidate.passed - baseline.passed,
            delta_failed=candidate.failed - baseline.failed,
        )

    def _latest_record(
        self,
        *,
        version: str,
        dataset: str | None = None,
    ) -> EvalRunRecord:
        records = self.list_records(dataset=dataset, version=version)
        if not records:
            raise ValueError(f"No eval history record found for version: {version}")
        return records[-1]


def _build_eval_id(dataset: str, version: str, created_at: str) -> str:
    text = f"{Path(dataset).name}:{version}:{created_at}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
