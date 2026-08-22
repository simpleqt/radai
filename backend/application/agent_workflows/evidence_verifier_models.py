"""证据核验模型层:证据载体、引用核验报告、答案诊断、声明指纹、置信度归一与证据矩阵渲染。(拆自 evidence_verifier.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from math import isfinite
from typing import Any
from typing import Protocol
from urllib.parse import urlsplit
import re

logger = get_logger(__name__)


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


_URL_RE = re.compile(r"https?://[^\s<>\[\]()（）\"'，。；、]+")


_BRACKET_REF_RE = re.compile(r"\[([A-Za-z0-9_\-./:]{1,80})\]")


class EvidenceCarrier(Protocol):
    role: str
    error: str | None
    handoff: Mapping[str, Any] | None


@dataclass(frozen=True)
class CitationVerificationReport:
    branch_count: int = 0
    successful_branches: int = 0
    failed_branches: int = 0
    total_claims: int = 0
    claims_with_refs: int = 0
    evidence_refs: int = 0
    captured_evidence_refs: int = 0
    independent_sources: int = 0
    low_confidence_claims: int = 0
    multi_source_claims: int = 0
    high_authority_sources: int = 0
    conflicted_claims: int = 0
    semantic_review_candidates: int = 0
    corroboration_rate: float = 0.0
    high_authority_source_ratio: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def citation_coverage(self) -> float:
        return self.claims_with_refs / self.total_claims if self.total_claims else 0.0

    @property
    def extraction_yield(self) -> float:
        """Source-bound claims produced per actually captured source."""

        return (
            self.claims_with_refs / self.captured_evidence_refs
            if self.captured_evidence_refs
            else 0.0
        )

    @property
    def strict_pass(self) -> bool:
        return (
            self.successful_branches > 0
            and self.failed_branches == 0
            and self.total_claims > 0
            and self.claims_with_refs == self.total_claims
            and self.low_confidence_claims == 0
            and self.independent_sources > 0
            and self.conflicted_claims == 0
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["citation_coverage"] = round(self.citation_coverage, 4)
        payload["extraction_yield"] = round(self.extraction_yield, 4)
        payload["strict_pass"] = self.strict_pass
        return payload


@dataclass(frozen=True)
class AnswerVerificationDiagnostics:
    """Claim-level support classes for one synthesized answer."""

    candidate_claims: int = 0
    supported_claims: int = 0
    ambiguous_claims: int = 0
    unsupported_claims: int = 0
    numeric_conflicts: int = 0
    negation_conflicts: int = 0
    reference_conflicts: int = 0
    unsupported_references: int = 0

    @property
    def verified_claim_rate(self) -> float:
        return (
            self.supported_claims / self.candidate_claims
            if self.candidate_claims
            else 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["verified_claim_rate"] = round(self.verified_claim_rate, 4)
        return payload


@dataclass(frozen=True)
class _AnswerSupportAnalysis:
    diagnostics: AnswerVerificationDiagnostics
    unsupported_claims: tuple[str, ...] = ()
    unsupported_refs: frozenset[str] = frozenset()


def format_evidence_matrix(
    branch_results: Iterable[EvidenceCarrier],
    *,
    max_chars: int = 24000,
) -> str:
    lines = ["<evidence_matrix>"]
    used = len(lines[0])

    def append_line(value: str) -> None:
        nonlocal used
        lines.append(value)
        # ``join`` inserts one newline before every appended item.
        used += 1 + len(value)

    for result in branch_results:
        handoff = result.handoff or {}
        claims = handoff.get("claims") or []
        if not claims:
            continue
        branch_error = str(getattr(result, "error", "") or "").strip()
        branch_status = "degraded" if branch_error else "usable"
        header = f'<agent_claims role="{result.role}" status="{branch_status}">'
        closing_reserve = len("\n</agent_claims>\n</evidence_matrix>")
        if used + 1 + len(header) + closing_reserve > max_chars:
            break
        append_line(header)
        if branch_error:
            error_line = f"- branch_error: {branch_error}"
            if used + 1 + len(error_line) + closing_reserve <= max_chars:
                append_line(error_line)
        for claim in claims:
            claim_text = str(claim.get("claim") or "").strip()
            refs = "; ".join(str(ref) for ref in claim.get("supporting_refs") or [])
            confidence = normalize_confidence(
                claim.get("confidence") or handoff.get("confidence"),
            )
            conflicts = "; ".join(
                str(item)
                for item in claim.get("conflicts") or handoff.get("conflicts") or []
            )
            missing = "; ".join(
                str(item)
                for item in claim.get("missing") or handoff.get("missing") or []
            )
            block = (
                f"- claim: {claim_text}\n"
                f"  supporting_refs: {refs or '无'}\n"
                f"  confidence: {confidence:.2f}\n"
                f"  conflicts: {conflicts or '无'}\n"
                f"  missing: {missing or '无'}"
            )
            if used + 1 + len(block) + closing_reserve > max_chars:
                append_line("</agent_claims>")
                append_line("</evidence_matrix>")
                return "\n".join(lines)
            append_line(block)
        append_line("</agent_claims>")
    append_line("</evidence_matrix>")
    return "\n".join(lines)


def claim_fingerprint(text: str) -> set[str]:
    lowered = text.lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_./:%+-]{1,}", lowered))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    terms.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {term for term in terms if len(term) >= 2}


def _source_key(ref: str) -> str:
    """Collapse references into independent source identities."""

    value = str(ref or "").strip().rstrip(").,;:，。；：")
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        parsed = urlsplit(value)
        return (parsed.hostname or value).lower().removeprefix("www.")
    return value


def normalize_confidence(value: Any, *, default: float = 0.5) -> float:
    """Return a bounded confidence value for untrusted model handoffs."""

    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    if not isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        return default
    return confidence
