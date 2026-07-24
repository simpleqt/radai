"""Deterministic Claim-Source graph and evidence-quality analysis."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class EvidenceCarrier(Protocol):
    role: str
    error: str | None
    handoff: Mapping[str, Any] | None


@dataclass(frozen=True)
class SourceNode:
    source_id: str
    ref: str
    source_kind: str
    authority_class: str
    claim_count: int
    branches: tuple[str, ...]


@dataclass(frozen=True)
class ClaimNode:
    claim_id: str
    claim: str
    confidence: float
    source_ids: tuple[str, ...]
    branches: tuple[str, ...]
    conflicts: tuple[str, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class ClaimConflict:
    left_claim_id: str
    right_claim_id: str
    reason: str


@dataclass(frozen=True)
class EvidenceQualityReport:
    claim_count: int
    source_count: int
    multi_source_claims: int
    high_authority_sources: int
    conflicted_claims: int
    semantic_review_candidates: int
    corroboration_rate: float
    high_authority_source_ratio: float
    source_kind_counts: dict[str, int]
    authority_class_counts: dict[str, int]
    claims: tuple[ClaimNode, ...]
    sources: tuple[SourceNode, ...]
    conflicts: tuple[ClaimConflict, ...]

    def to_dict(self, *, include_nodes: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        if not include_nodes:
            payload.pop("claims", None)
            payload.pop("sources", None)
            payload.pop("conflicts", None)
        return payload


def build_claim_source_graph(
    branch_results: Iterable[EvidenceCarrier],
) -> EvidenceQualityReport:
    """Build a bounded graph from usable, source-bound handoff claims."""

    raw_claims: list[dict[str, Any]] = []
    for result in branch_results:
        if str(getattr(result, "error", "") or "").strip():
            continue
        branch = str(
            getattr(result, "task_key", None)
            or getattr(result, "role", "")
            or "unknown"
        )
        handoff = result.handoff or {}
        for claim in handoff.get("claims") or []:
            text = str(claim.get("claim") or "").strip()
            refs = _unique_strings(claim.get("supporting_refs"), limit=8)
            fingerprint = _claim_fingerprint(text)
            if not text or not refs or not fingerprint:
                continue
            raw_claims.append(
                {
                    "claim": text,
                    "fingerprint": fingerprint,
                    "confidence": _normalize_confidence(
                        claim.get("confidence") or handoff.get("confidence")
                    ),
                    "refs": refs,
                    "branches": {branch},
                    "conflicts": set(
                        _unique_strings(
                            claim.get("conflicts") or handoff.get("conflicts"),
                            limit=10,
                        )
                    ),
                    "missing": set(
                        _unique_strings(
                            claim.get("missing") or handoff.get("missing"),
                            limit=10,
                        )
                    ),
                }
            )

    merged: list[dict[str, Any]] = []
    for candidate in raw_claims:
        equivalent = next(
            (
                existing
                for existing in merged
                if _claims_equivalent(
                    candidate["claim"],
                    candidate["fingerprint"],
                    existing["claim"],
                    existing["fingerprint"],
                )
            ),
            None,
        )
        if equivalent is None:
            merged.append(candidate)
            continue
        equivalent["refs"] = list(
            dict.fromkeys([*equivalent["refs"], *candidate["refs"]])
        )[:12]
        equivalent["branches"].update(candidate["branches"])
        equivalent["conflicts"].update(candidate["conflicts"])
        equivalent["missing"].update(candidate["missing"])
        equivalent["confidence"] = max(
            equivalent["confidence"], candidate["confidence"]
        )

    source_work: dict[str, dict[str, Any]] = {}
    claim_nodes: list[ClaimNode] = []
    for index, claim in enumerate(merged, start=1):
        source_ids: list[str] = []
        for ref in claim["refs"]:
            source_id = canonical_source_identity(ref)
            if not source_id:
                continue
            source_ids.append(source_id)
            entry = source_work.setdefault(
                source_id,
                {
                    "ref": ref,
                    "source_kind": source_kind(ref),
                    "authority_class": authority_class(ref),
                    "claim_ids": set(),
                    "branches": set(),
                },
            )
            entry["claim_ids"].add(f"c{index}")
            entry["branches"].update(claim["branches"])
        claim_nodes.append(
            ClaimNode(
                claim_id=f"c{index}",
                claim=claim["claim"],
                confidence=round(claim["confidence"], 4),
                source_ids=tuple(dict.fromkeys(source_ids)),
                branches=tuple(sorted(claim["branches"])),
                conflicts=tuple(sorted(claim["conflicts"]))[:10],
                missing=tuple(sorted(claim["missing"]))[:10],
            )
        )

    detected_conflicts: list[ClaimConflict] = []
    for left_index, left in enumerate(claim_nodes):
        for right in claim_nodes[left_index + 1 :]:
            reason = _conflict_reason(left.claim, right.claim)
            if reason:
                detected_conflicts.append(
                    ClaimConflict(
                        left_claim_id=left.claim_id,
                        right_claim_id=right.claim_id,
                        reason=reason,
                    )
                )

    source_nodes = tuple(
        SourceNode(
            source_id=source_id,
            ref=entry["ref"],
            source_kind=entry["source_kind"],
            authority_class=entry["authority_class"],
            claim_count=len(entry["claim_ids"]),
            branches=tuple(sorted(entry["branches"])),
        )
        for source_id, entry in sorted(source_work.items())
    )
    conflict_claim_ids = {
        claim_id
        for conflict in detected_conflicts
        for claim_id in (conflict.left_claim_id, conflict.right_claim_id)
    }
    high_authority = {
        "institutional",
        "scholarly_index",
        "selected_knowledge_base",
        "project_data",
    }
    high_authority_count = sum(
        source.authority_class in high_authority for source in source_nodes
    )
    multi_source_claims = sum(len(claim.source_ids) >= 2 for claim in claim_nodes)
    semantic_candidates = sum(
        claim.claim_id in conflict_claim_ids
        or claim.confidence < 0.5
        or bool(claim.conflicts)
        or bool(claim.missing)
        for claim in claim_nodes
    )
    return EvidenceQualityReport(
        claim_count=len(claim_nodes),
        source_count=len(source_nodes),
        multi_source_claims=multi_source_claims,
        high_authority_sources=high_authority_count,
        conflicted_claims=len(conflict_claim_ids),
        semantic_review_candidates=semantic_candidates,
        corroboration_rate=round(multi_source_claims / max(1, len(claim_nodes)), 4),
        high_authority_source_ratio=round(
            high_authority_count / max(1, len(source_nodes)), 4
        ),
        source_kind_counts=_counts(source.source_kind for source in source_nodes),
        authority_class_counts=_counts(
            source.authority_class for source in source_nodes
        ),
        claims=tuple(claim_nodes),
        sources=source_nodes,
        conflicts=tuple(detected_conflicts),
    )


def canonical_source_identity(ref: str) -> str:
    """Collapse URL tracking variants while preserving document identity."""

    value = str(ref or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        return re.sub(r"\s+", " ", value).casefold()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return value.casefold()
    host = (parts.hostname or "").casefold().removeprefix("www.")
    netloc = f"{host}:{port}" if port else host
    path = re.sub(r"/{2,}", "/", parts.path or "/").rstrip("/") or "/"
    tracking_keys = {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "referrer",
        "source",
        "from",
    }
    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in tracking_keys
    ]
    query = urlencode(sorted(query_items), doseq=True)
    return urlunsplit((parts.scheme.casefold(), netloc, path, query, ""))


def source_kind(ref: str) -> str:
    value = str(ref or "")
    if value.startswith(("http://", "https://")):
        return "web"
    if value.startswith("[project-db:"):
        return "project_data"
    if value.startswith("artifact://"):
        return "artifact"
    if value.startswith("[") and "#" in value:
        return "knowledge_base"
    return "reference"


def authority_class(ref: str) -> str:
    value = str(ref or "")
    kind = source_kind(value)
    if kind == "knowledge_base":
        return "selected_knowledge_base"
    if kind == "project_data":
        return "project_data"
    if not value.startswith(("http://", "https://")):
        return "unclassified"
    try:
        host = (urlsplit(value).hostname or "").casefold()
    except ValueError:
        return "unclassified"
    if host in {"doi.org", "arxiv.org"} or host.endswith(".doi.org"):
        return "scholarly_index"
    if host.endswith((".gov", ".gov.cn", ".edu", ".edu.cn")):
        return "institutional"
    return "general_web"


def _claims_equivalent(
    left_text: str,
    left: set[str],
    right_text: str,
    right: set[str],
) -> bool:
    overlap = len(left & right) / max(1, min(len(left), len(right)))
    return (
        overlap >= 0.75
        and _numbers(left_text) == _numbers(right_text)
        and bool(_negations(left_text)) == bool(_negations(right_text))
    )


def _conflict_reason(left: str, right: str) -> str:
    left_fingerprint = _claim_fingerprint(left)
    right_fingerprint = _claim_fingerprint(right)
    overlap = len(left_fingerprint & right_fingerprint) / max(
        1, min(len(left_fingerprint), len(right_fingerprint))
    )
    if overlap < 0.55:
        return ""
    left_numbers = _numbers(left)
    right_numbers = _numbers(right)
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return "numeric_mismatch"
    if bool(_negations(left)) != bool(_negations(right)):
        return "negation_mismatch"
    return ""


def _claim_fingerprint(text: str) -> set[str]:
    lowered = str(text or "").casefold()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_./:%+-]{1,}", lowered))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    terms.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {term for term in terms if len(term) >= 2}


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"(?<!\w)\d+(?:\.\d+)?%?(?!\w)", str(text or "")))


def _negations(text: str) -> set[str]:
    return set(
        re.findall(
            r"(?:不能|无法|并非|没有|不存在|不|未|not|no|cannot|never)",
            str(text or "").casefold(),
        )
    )


def _normalize_confidence(value: Any, *, default: float = 0.5) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    if not isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        return default
    return confidence


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


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


__all__ = [
    "ClaimConflict",
    "ClaimNode",
    "EvidenceQualityReport",
    "SourceNode",
    "authority_class",
    "build_claim_source_graph",
    "canonical_source_identity",
    "source_kind",
]
