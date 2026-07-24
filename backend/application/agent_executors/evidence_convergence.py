"""Generic evidence-convergence policy and tool-event controller."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from backend.application.agent_executors.evidence_evaluator import (
    EvidenceCompletenessEvaluator,
)

logger = logging.getLogger(__name__)

EVIDENCE_CONVERGENCE_MAX_QUERIES_ENV = "EVIDENCE_CONVERGENCE_QUERY_LIMIT"
EVIDENCE_CONVERGENCE_MAX_INCOMPLETE_ENV = (
    "EVIDENCE_CONVERGENCE_MAX_INCOMPLETE_EVALUATIONS"
)
LEGACY_WRITER_HANDOFF_QUERY_LIMIT_ENV = (
    "GROUNDWATER_WRITER_HANDOFF_QUERY_LIMIT"
)


class EvidenceConvergenceState(Protocol):
    """Evidence state exposed by a streaming handler."""

    @property
    def project_evidence_text(self) -> str: ...

    @property
    def retrieved_evidence_text(self) -> str: ...

    def should_force_evidence_convergence(
        self,
        *,
        max_queries: int,
    ) -> bool: ...

    def should_evaluate_evidence(
        self,
        *,
        min_retrieval_blocks: int,
    ) -> bool: ...

    def evidence_fingerprint(self) -> str: ...

    def mark_evidence_evaluated(self, fingerprint: str) -> None: ...


class EvidenceEvaluation(Protocol):
    complete: bool
    missing_evidence: list[str]
    next_action: str


class EvidenceEvaluator(Protocol):
    async def evaluate(
        self,
        *,
        question: str,
        project_evidence: str,
        retrieved_evidence: str,
    ) -> EvidenceEvaluation: ...


def _coerce_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_int(value: object, *, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _policy_overrides(
    request_metadata: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    metadata = request_metadata or {}
    configured = metadata.get("evidence_convergence")
    return configured if isinstance(configured, Mapping) else {}


@dataclass(frozen=True)
class EvidenceConvergencePolicy:
    """Configuration for deciding when retrieval should hand off to writing."""

    enabled: bool = False
    max_queries: int = 6
    min_retrieval_blocks: int = 1
    max_incomplete_evaluations: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_queries",
            max(1, int(self.max_queries)),
        )
        object.__setattr__(
            self,
            "min_retrieval_blocks",
            max(1, int(self.min_retrieval_blocks)),
        )
        object.__setattr__(
            self,
            "max_incomplete_evaluations",
            max(1, int(self.max_incomplete_evaluations)),
        )

    @classmethod
    def from_runtime(
        cls,
        *,
        prompt_domain: str,
        request_metadata: Mapping[str, Any] | None = None,
    ) -> EvidenceConvergencePolicy:
        """Resolve explicit policy, generic env, then legacy compatibility."""

        overrides = _policy_overrides(request_metadata)
        legacy_default_enabled = (
            str(prompt_domain or "").strip().lower() == "groundwater"
        )
        if "enabled" in overrides:
            enabled = _coerce_bool(
                overrides.get("enabled"),
                default=legacy_default_enabled,
            )
        else:
            enabled = _coerce_bool(
                os.getenv("EVIDENCE_CONVERGENCE_ENABLED"),
                default=legacy_default_enabled,
            )

        if "max_queries" in overrides or "max_project_queries" in overrides:
            configured_query_limit = (
                overrides.get("max_queries")
                if "max_queries" in overrides
                else overrides.get("max_project_queries")
            )
            max_queries = _coerce_int(
                configured_query_limit,
                default=6,
            )
        else:
            generic_limit = os.getenv(EVIDENCE_CONVERGENCE_MAX_QUERIES_ENV)
            if generic_limit is not None:
                max_queries = _coerce_int(
                    generic_limit,
                    default=6,
                )
            else:
                # The old setting clamped to four; preserve that behavior only
                # on the compatibility path while generic policies may use a
                # smaller, explicit threshold.
                max_queries = _coerce_int(
                    os.getenv(LEGACY_WRITER_HANDOFF_QUERY_LIMIT_ENV, "6"),
                    default=6,
                    minimum=4,
                )

        min_retrieval_blocks = _coerce_int(
            overrides.get(
                "min_retrieval_blocks",
                os.getenv("EVIDENCE_CONVERGENCE_MIN_RETRIEVAL_BLOCKS", "1"),
            ),
            default=1,
        )
        max_incomplete_evaluations = _coerce_int(
            overrides.get(
                "max_incomplete_evaluations",
                os.getenv(EVIDENCE_CONVERGENCE_MAX_INCOMPLETE_ENV, "3"),
            ),
            default=3,
        )
        return cls(
            enabled=enabled,
            max_queries=max_queries,
            min_retrieval_blocks=min_retrieval_blocks,
            max_incomplete_evaluations=max_incomplete_evaluations,
        )


@dataclass(frozen=True)
class EvidenceConvergenceUpdate:
    """One user-visible convergence update emitted during evaluation."""

    message: str
    converged: bool = False
    reason: str = "progress"


class EvidenceConvergenceController:
    """Turn one tool completion into zero or more convergence updates."""

    def __init__(
        self,
        *,
        policy: EvidenceConvergencePolicy,
        evaluator: EvidenceEvaluator,
        label: str = "agent-runtime",
    ) -> None:
        self.policy = policy
        self._evaluator = evaluator
        self._label = str(label or "agent-runtime")
        self._incomplete_evaluation_count = 0

    @classmethod
    def from_runtime(
        cls,
        *,
        model_name: str,
        prompt_domain: str,
        request_metadata: Mapping[str, Any] | None = None,
        policy: EvidenceConvergencePolicy | None = None,
        label: str = "agent-runtime",
    ) -> EvidenceConvergenceController:
        return cls(
            policy=policy
            or EvidenceConvergencePolicy.from_runtime(
                prompt_domain=prompt_domain,
                request_metadata=request_metadata,
            ),
            evaluator=EvidenceCompletenessEvaluator(model_name=model_name),
            label=label,
        )

    async def on_tool_event(
        self,
        *,
        question: str,
        state: EvidenceConvergenceState,
    ) -> AsyncIterator[EvidenceConvergenceUpdate]:
        """Evaluate convergence once after a tool event.

        This is an async iterator so the existing "evaluating evidence" status
        remains visible before the model-based evaluator finishes.
        """

        if not self.policy.enabled:
            return

        if state.should_force_evidence_convergence(
            max_queries=self.policy.max_queries
        ):
            yield EvidenceConvergenceUpdate(
                message="已完成多轮项目数据查询，正在直接生成阶段性分析结论...",
                converged=True,
                reason="query_limit",
            )
            return

        if not state.should_evaluate_evidence(
            min_retrieval_blocks=self.policy.min_retrieval_blocks
        ):
            return

        fingerprint = state.evidence_fingerprint()
        yield EvidenceConvergenceUpdate(
            message="正在评估现有证据是否足以回答问题...",
            reason="evaluating",
        )
        try:
            evaluation = await self._evaluator.evaluate(
                question=question,
                project_evidence=state.project_evidence_text,
                retrieved_evidence=state.retrieved_evidence_text,
            )
        except Exception:
            state.mark_evidence_evaluated(fingerprint)
            logger.warning(
                "[%s] 证据完整性评估失败，继续检索流程",
                self._label,
                exc_info=True,
            )
            yield EvidenceConvergenceUpdate(
                message="证据完整性暂无法确认，继续查询所需信息...",
                reason="evaluation_failed",
            )
            return

        state.mark_evidence_evaluated(fingerprint)
        if evaluation.complete:
            yield EvidenceConvergenceUpdate(
                message="证据完整性评估通过，正在交给撰写智能体生成答案...",
                converged=True,
                reason="complete",
            )
            return

        missing_items = getattr(evaluation, "missing_evidence", None) or []
        missing_items = [
            str(item).strip()
            for item in list(missing_items)[:6]
            if str(item).strip()
        ]
        missing = "；".join(
            missing_items[:3]
        )
        next_action = str(getattr(evaluation, "next_action", "") or "").strip()
        record_gap = getattr(state, "record_evidence_gap", None)
        if callable(record_gap):
            record_gap(missing_items, next_action)

        self._incomplete_evaluation_count += 1
        detail = missing or next_action or "仍需补充直接证据"
        if (
            self._incomplete_evaluation_count
            >= self.policy.max_incomplete_evaluations
        ):
            yield EvidenceConvergenceUpdate(
                message=(
                    "多轮查询后关键证据仍未补齐，正在基于现有证据作答并明确数据缺口..."
                ),
                converged=True,
                reason="insufficient_limit",
            )
            return
        yield EvidenceConvergenceUpdate(
            message=f"现有证据不足，继续查询：{detail}",
            reason="insufficient",
        )


__all__ = [
    "EVIDENCE_CONVERGENCE_MAX_QUERIES_ENV",
    "EVIDENCE_CONVERGENCE_MAX_INCOMPLETE_ENV",
    "LEGACY_WRITER_HANDOFF_QUERY_LIMIT_ENV",
    "EvidenceConvergenceController",
    "EvidenceConvergencePolicy",
    "EvidenceConvergenceState",
    "EvidenceConvergenceUpdate",
]
