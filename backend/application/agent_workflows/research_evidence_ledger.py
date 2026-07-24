"""Compact research scope, coverage, and evidence ledgers.

The coordinated workflow keeps raw branch output for auditability, while these
value objects provide bounded, deduplicated inputs to the supervisor and writer.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
    ResearchTask,
)
from backend.application.agent_workflows.evidence_verifier import (
    CitationVerificationReport,
    claim_fingerprint,
    normalize_confidence,
)
from backend.application.agent_workflows.evidence_quality import (
    authority_class,
    build_claim_source_graph,
    canonical_source_identity,
    source_kind,
)
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext


@dataclass(frozen=True)
class ResearchBrief:
    """Stable research contract shared by planning, supervision, and writing."""

    objective: str
    dimensions: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    source_policy: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CoverageLedger:
    """Deterministic snapshot of research progress after one execution wave."""

    planned_dimensions: tuple[str, ...]
    covered_dimensions: tuple[str, ...]
    unresolved_dimensions: tuple[str, ...]
    degraded_dimensions: tuple[str, ...]
    coverage_score: float
    citation_coverage: float
    independent_sources: int
    source_count: int
    novel_source_ratio: float
    corroboration_rate: float
    high_authority_source_ratio: float
    conflicted_claims: int
    semantic_review_candidates: int
    conflicts: tuple[str, ...]
    missing: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_default_research_brief(
    request: AgentRequest,
    context: AgentRuntimeContext,
) -> ResearchBrief:
    """Build a no-extra-LLM fallback brief from request-level constraints."""

    source_policy: list[str] = []
    if context.kb_ids:
        source_policy.append("关键结论优先使用用户选择的知识库并保留文档级引用")
    if context.use_search:
        source_policy.append("时效性事实使用联网来源交叉核验")
    if not source_policy:
        source_policy.append("无外部检索源时明确标注模型知识边界")
    return ResearchBrief(
        objective=request.question.strip(),
        source_policy=tuple(source_policy),
        success_criteria=(
            "覆盖回答原问题所需的主要维度",
            "关键事实逐项绑定可追溯来源",
            "说明冲突、不确定性与适用边界",
        ),
    )


def parse_research_brief(
    planner_answer: str,
    *,
    fallback: ResearchBrief,
) -> ResearchBrief:
    """Read a planner-produced brief without making planning fail on omission."""

    payload = _decode_json_object(planner_answer)
    raw = payload.get("research_brief") if payload else None
    if not isinstance(raw, dict):
        return fallback
    objective = str(raw.get("objective") or fallback.objective).strip()
    return ResearchBrief(
        objective=objective or fallback.objective,
        dimensions=_bounded_strings(raw.get("dimensions"), fallback.dimensions, 12),
        exclusions=_bounded_strings(raw.get("exclusions"), fallback.exclusions, 8),
        source_policy=_bounded_strings(
            raw.get("source_policy"), fallback.source_policy, 8
        ),
        success_criteria=_bounded_strings(
            raw.get("success_criteria"), fallback.success_criteria, 8
        ),
    )


def build_coverage_ledger(
    tasks: Iterable[ResearchTask],
    branch_results: Iterable[CoordinationUnitResult],
    citation_report: CitationVerificationReport,
    *,
    previous_source_ids: frozenset[str] = frozenset(),
) -> CoverageLedger:
    """Summarize coverage and novelty without trusting a model self-assessment."""

    task_list = list(tasks)
    results = list(branch_results)
    result_by_key = {result.task_key: result for result in results if result.task_key}
    planned = tuple(task.title for task in task_list)
    covered: list[str] = []
    degraded: list[str] = []
    source_ids: set[str] = set()
    conflicts: list[str] = []
    missing: list[str] = []

    for task in task_list:
        result = result_by_key.get(task.key)
        if result is None or result.error:
            degraded.append(task.title)
            continue
        handoff = result.handoff or {}
        cited_claims = [
            claim
            for claim in handoff.get("claims") or []
            if claim.get("supporting_refs")
        ]
        if cited_claims:
            covered.append(task.title)
        else:
            degraded.append(task.title)
        for ref in _claim_refs(handoff):
            if identity := canonical_source_identity(ref):
                source_ids.add(identity)
        conflicts.extend(str(item) for item in handoff.get("conflicts") or [])
        missing.extend(str(item) for item in handoff.get("missing") or [])
        for claim in handoff.get("claims") or []:
            conflicts.extend(str(item) for item in claim.get("conflicts") or [])
            missing.extend(str(item) for item in claim.get("missing") or [])

    unresolved = tuple(title for title in planned if title not in covered)
    new_sources = source_ids - previous_source_ids
    novelty_denominator = max(1, len(source_ids))
    return CoverageLedger(
        planned_dimensions=planned,
        covered_dimensions=tuple(covered),
        unresolved_dimensions=unresolved,
        degraded_dimensions=tuple(dict.fromkeys(degraded)),
        coverage_score=round(len(covered) / max(1, len(planned)), 4),
        citation_coverage=round(citation_report.citation_coverage, 4),
        independent_sources=citation_report.independent_sources,
        source_count=len(source_ids),
        novel_source_ratio=round(len(new_sources) / novelty_denominator, 4),
        corroboration_rate=citation_report.corroboration_rate,
        high_authority_source_ratio=citation_report.high_authority_source_ratio,
        conflicted_claims=citation_report.conflicted_claims,
        semantic_review_candidates=citation_report.semantic_review_candidates,
        conflicts=_bounded_strings(conflicts, (), 20),
        missing=_bounded_strings(missing, (), 20),
    )


def collect_source_ids(
    branch_results: Iterable[CoordinationUnitResult],
) -> frozenset[str]:
    source_ids: set[str] = set()
    for result in branch_results:
        if result.error:
            continue
        for ref in _claim_refs(result.handoff or {}):
            if identity := canonical_source_identity(ref):
                source_ids.add(identity)
    return frozenset(source_ids)


def format_compact_evidence_ledger(
    branch_results: Iterable[CoordinationUnitResult],
    *,
    max_chars: int = 28000,
) -> str:
    """Render cited claims once, with a deduplicated source catalog.

    Raw summaries are intentionally excluded: only claim-level source bindings
    are allowed to reach the final writer.
    """

    results = list(branch_results)
    quality = build_claim_source_graph(results)
    claims: list[dict[str, Any]] = []
    seen_claims: set[tuple[str, ...]] = set()
    for result in results:
        if result.error:
            continue
        handoff = result.handoff or {}
        for claim in handoff.get("claims") or []:
            refs = _unique_strings(claim.get("supporting_refs"), limit=8)
            claim_text = str(claim.get("claim") or "").strip()
            fingerprint = tuple(sorted(claim_fingerprint(claim_text)))
            if (
                not claim_text
                or not refs
                or not fingerprint
                or fingerprint in seen_claims
            ):
                continue
            seen_claims.add(fingerprint)
            claims.append(
                {
                    "branch": result.task_key or result.role,
                    "dimension": result.task_title or result.role,
                    "claim": claim_text[:1600],
                    "supporting_refs": refs,
                    "confidence": round(
                        normalize_confidence(
                            claim.get("confidence") or handoff.get("confidence")
                        ),
                        3,
                    ),
                    "conflicts": _unique_strings(claim.get("conflicts"), limit=4),
                    "missing": _unique_strings(claim.get("missing"), limit=4),
                }
            )
    claims.sort(
        key=lambda item: (
            item["confidence"],
            len(item["supporting_refs"]),
            item["dimension"],
        ),
        reverse=True,
    )
    quality_claims = {claim.claim_id: claim.claim for claim in quality.claims}
    detected_conflicts = [
        {
            "left": quality_claims.get(conflict.left_claim_id, "")[:800],
            "right": quality_claims.get(conflict.right_claim_id, "")[:800],
            "reason": conflict.reason,
        }
        for conflict in quality.conflicts[:6]
    ]

    omitted = 0
    while True:
        referenced = _source_catalog(claims)
        payload = {
            "schema_version": "1.0",
            "claim_count": len(claims),
            "omitted_claims": omitted,
            "quality": quality.to_dict(),
            "detected_conflicts": detected_conflicts,
            "sources": referenced,
            "claims": claims,
        }
        rendered = (
            "<evidence_ledger>\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n</evidence_ledger>"
        )
        if len(rendered) <= max_chars or not claims:
            return rendered
        claims.pop()
        omitted += 1


def _source_catalog(claims: list[dict[str, Any]]) -> list[dict[str, str]]:
    catalog: dict[str, str] = {}
    for claim in claims:
        for ref in claim["supporting_refs"]:
            source_id = canonical_source_identity(ref)
            if source_id and source_id not in catalog:
                catalog[source_id] = ref
    return [
        {
            "source_id": source_id,
            "ref": ref,
            "source_kind": source_kind(ref),
            "authority_class": authority_class(ref),
        }
        for source_id, ref in sorted(catalog.items())
    ]


def _claim_refs(handoff: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for claim in handoff.get("claims") or []:
        refs.extend(claim.get("supporting_refs") or [])
    return _unique_strings(refs, limit=200)


def _unique_strings(value: Any, *, limit: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = list(value)
    else:
        raw = [value]
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))[
        :limit
    ]


def _bounded_strings(
    value: Any,
    fallback: tuple[str, ...],
    limit: int,
) -> tuple[str, ...]:
    items = _unique_strings(value, limit=limit)
    return tuple(items) if items else tuple(fallback)


def _decode_json_object(value: str) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidate = fenced.group(1).strip() if fenced else text
    start = candidate.find("{")
    if start < 0:
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(candidate[start:])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
