"""引用核验:交接引用核验器、编号引用别名解析与未支撑引用替换。(拆自 evidence_verifier.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.application.agent_workflows.evidence_quality import build_claim_source_graph
from backend.application.agent_workflows.evidence_quality import canonical_source_identity
from collections.abc import Iterable
from collections.abc import Mapping
import re
from backend.application.agent_workflows.evidence_verifier_models import (
    CitationVerificationReport,
    EvidenceCarrier,
    _BRACKET_REF_RE,
    _MARKDOWN_LINK_RE,
    _URL_RE,
    _source_key,
    normalize_confidence,
)

logger = get_logger(__name__)


class CitationVerifier:
    """Validate branch handoff citations before writer synthesis."""

    def verify_handoffs(
        self,
        branch_results: Iterable[EvidenceCarrier],
    ) -> CitationVerificationReport:
        materialized = tuple(branch_results)
        branch_count = 0
        successful_branches = 0
        failed_branches = 0
        total_claims = 0
        claims_with_refs = 0
        evidence_refs: set[str] = set()
        captured_evidence_refs: set[str] = set()
        independent_sources: set[str] = set()
        low_confidence_claims = 0
        warnings: list[str] = []
        for result in materialized:
            branch_count += 1
            role = str(getattr(result, "role", "") or "unknown")
            handoff = result.handoff or {}
            role_refs = {
                str(ref) for ref in handoff.get("evidence_refs") or [] if str(ref)
            }
            captured_evidence_refs.update(
                str(ref).strip()
                for ref in (getattr(result, "captured_evidence_refs", ()) or role_refs)
                if str(ref).strip()
            )
            branch_error = str(getattr(result, "error", "") or "").strip()
            if branch_error:
                failed_branches += 1
                warnings.append(f"{role}: branch failed or degraded: {branch_error}")
                # Degraded output can explain a gap, but must not improve the
                # citation or source-diversity score used by the supervisor.
                continue
            successful_branches += 1
            evidence_refs.update(role_refs)
            claims = handoff.get("claims") or []
            if not claims:
                warnings.append(f"{role}: no claims in handoff")
                continue
            for index, claim in enumerate(claims, start=1):
                total_claims += 1
                refs = {
                    str(ref) for ref in claim.get("supporting_refs") or [] if str(ref)
                }
                evidence_refs.update(refs)
                independent_sources.update(
                    _source_key(ref) for ref in refs if _source_key(ref)
                )
                if refs:
                    claims_with_refs += 1
                else:
                    warnings.append(f"{role}: claim[{index}] missing supporting_refs")
                confidence = normalize_confidence(
                    claim.get("confidence") or handoff.get("confidence"),
                )
                if confidence < 0.5:
                    low_confidence_claims += 1
                    warnings.append(f"{role}: claim[{index}] confidence below 0.5")
        if total_claims == 0:
            warnings.append("no verifiable claims before writer")
        elif claims_with_refs < total_claims:
            warnings.append(
                f"{total_claims - claims_with_refs}/{total_claims} claims lack supporting refs"
            )
        quality = build_claim_source_graph(materialized)
        if quality.conflicted_claims:
            warnings.append(
                f"{quality.conflicted_claims} source-bound claims have deterministic conflicts"
            )
        if quality.semantic_review_candidates:
            warnings.append(
                f"{quality.semantic_review_candidates} claims require semantic or authority review"
            )
        return CitationVerificationReport(
            branch_count=branch_count,
            successful_branches=successful_branches,
            failed_branches=failed_branches,
            total_claims=total_claims,
            claims_with_refs=claims_with_refs,
            evidence_refs=len(evidence_refs),
            captured_evidence_refs=len(captured_evidence_refs),
            independent_sources=len(independent_sources),
            low_confidence_claims=low_confidence_claims,
            multi_source_claims=quality.multi_source_claims,
            high_authority_sources=quality.high_authority_sources,
            conflicted_claims=quality.conflicted_claims,
            semantic_review_candidates=quality.semantic_review_candidates,
            corroboration_rate=quality.corroboration_rate,
            high_authority_source_ratio=quality.high_authority_source_ratio,
            warnings=warnings,
        )


def _numbered_citation_aliases(text: str) -> dict[str, str]:
    """Resolve paper-style ``[n]`` citations through the bibliography."""

    reference_headings = (
        "参考文献",
        "参考资料",
        "资料来源",
        "信息来源",
        "来源列表",
        "references",
        "sources",
        "bibliography",
    )
    aliases: dict[str, str] = {}
    in_reference_section = False
    for raw_line in str(text or "").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            in_reference_section = any(
                marker in heading for marker in reference_headings
            )
            continue
        if not in_reference_section:
            continue
        match = re.match(r"^\[(\d{1,4})\]\s*(?:[:：]\s*)?(.+?)\s*$", stripped)
        if not match:
            continue
        raw_ref = match.group(2).strip()
        markdown_link = _MARKDOWN_LINK_RE.search(raw_ref)
        url = _URL_RE.search(raw_ref)
        if markdown_link:
            normalized = _normalize_ref(markdown_link.group(2))
        elif url:
            normalized = _normalize_ref(url.group(0))
        else:
            normalized = _normalize_ref(raw_ref)
        if normalized:
            aliases[f"[{match.group(1)}]"] = normalized
    return aliases


def _refs(
    text: str,
    *,
    citation_aliases: Mapping[str, str] | None = None,
) -> set[str]:
    value = str(text or "")
    refs = {
        normalized
        for match in _URL_RE.finditer(value)
        if (normalized := _normalize_ref(match.group(0)))
    }
    for match in _BRACKET_REF_RE.finditer(value):
        # ``[label](https://...)`` is a Markdown link label, not an
        # independent evidence reference. Its destination is already found
        # by ``_URL_RE``.
        if value[match.end() :].lstrip().startswith("("):
            continue
        token = match.group(1)
        if token.lower().startswith(("http://", "https://")):
            continue
        bracketed = f"[{token}]"
        if token.isdigit() and citation_aliases and bracketed in citation_aliases:
            refs.add(citation_aliases[bracketed])
        else:
            refs.add(bracketed)
    return refs


def _normalize_ref(ref: str) -> str:
    value = str(ref or "").strip().rstrip(").,;:，。；：）")
    if value.startswith(("http://", "https://")):
        return canonical_source_identity(value)
    return value


def _replace_unsupported_refs(text: str, unsupported_refs: set[str]) -> str:
    """Remove unauthorized citations without creating a placeholder wall.

    The document-level verification warning already explains that references
    were removed. Replacing every bad bibliography item and every in-text
    citation with ``（未核验来源）`` duplicates that warning dozens of times and
    leaves unusable reference entries behind.
    """

    if not unsupported_refs:
        return text

    citation_aliases = _numbered_citation_aliases(text)
    unsupported_markers = {
        marker
        for marker, resolved_ref in citation_aliases.items()
        if resolved_ref in unsupported_refs
    }
    reference_headings = (
        "参考文献",
        "参考资料",
        "资料来源",
        "信息来源",
        "来源列表",
        "references",
        "sources",
        "bibliography",
    )
    kept_lines: list[str] = []
    in_reference_section = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            in_reference_section = any(
                marker in heading for marker in reference_headings
            )
            kept_lines.append(raw_line)
            continue
        if in_reference_section:
            definition = re.match(r"^\[(\d{1,4})\]\s*(?:[:：]\s*)?", stripped)
            if definition and f"[{definition.group(1)}]" in unsupported_markers:
                continue
            line_refs = _refs(raw_line, citation_aliases=citation_aliases)
            if line_refs and line_refs.issubset(unsupported_refs):
                continue
        kept_lines.append(raw_line)
    replaced = "\n".join(kept_lines)

    def replace_markdown_link(match: re.Match[str]) -> str:
        destination = _normalize_ref(match.group(2))
        return "" if destination in unsupported_refs else match.group(0)

    replaced = _MARKDOWN_LINK_RE.sub(replace_markdown_link, replaced)

    def replace_url(match: re.Match[str]) -> str:
        ref = _normalize_ref(match.group(0))
        return "" if ref in unsupported_refs else match.group(0)

    replaced = _URL_RE.sub(replace_url, replaced)
    for marker in unsupported_markers:
        replaced = replaced.replace(marker, "")
    for ref in sorted(
        (
            item
            for item in unsupported_refs
            if item.startswith("[") and item.endswith("]")
        ),
        key=len,
        reverse=True,
    ):
        replaced = replaced.replace(ref, "")
    # Citation removal can leave trailing spaces on many lines. Keep the
    # original paragraph structure while avoiding visible formatting debris.
    return "\n".join(line.rstrip() for line in replaced.splitlines())
