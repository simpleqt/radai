"""Evidence-matrix rendering and deterministic answer verification."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any, Protocol
from urllib.parse import urlsplit

from backend.application.agent_workflows.evidence_quality import (
    build_claim_source_graph,
    canonical_source_identity,
)

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


# Sentence-level fingerprint overlap required to treat a rewrite as supported.
_CLAIM_OVERLAP_THRESHOLD = 0.14
# Matches below this stronger boundary remain usable for deterministic output
# compatibility, but are surfaced as semantic-review candidates.
_SEMANTIC_REVIEW_OVERLAP_THRESHOLD = 0.28
# Above this many inline “待核验：” prefixes, collapse to a document-level warning.
_MAX_INLINE_DOWNGRADES = 3
# If more than this fraction of explicit candidates fail, avoid a “待核验墙”.
_WALL_RATIO_THRESHOLD = 0.5


def answer_claim_candidates(answer: str) -> list[str]:
    markers = (
        "结论",
        "表明",
        "显示",
        "认为",
        "指出",
        "发现",
        "风险",
        "限制",
        "因此",
        "所以",
        "支持",
        "证明",
        "risk",
        "shows",
        "indicates",
        "suggests",
        "concludes",
    )
    candidates: list[str] = []
    uncertainty_markers = (
        "待核验",
        "当前证据未提供",
        "无法确认",
        "尚不确定",
        "可能",
        "或许",
        "缺乏",
        "未提供",
        "证据不足",
        "未检索到",
        "尚无",
        "无法从",
        "待补充",
        "仍需核验",
        "uncertain",
        "unknown",
        "insufficient evidence",
        "not verified",
        "may",
        "might",
    )
    structural_markers = ("以下内容", "下面将", "本文将", "回答如下", "evidence matrix")
    diagnostic_markers = (
        "总结论数",
        "有文献支撑的结论",
        "引用覆盖率",
        "低置信度结论",
        "total_claims",
        "claims_with_refs",
        "citation_coverage",
        "low_confidence_claims",
        "strict_pass",
        "successful_branches",
    )
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
    recommendation_headings = (
        "建议",
        "下一步",
        "行动项",
        "后续工作",
        "recommendation",
        "next step",
    )
    in_reference_section = False
    in_recommendation_section = False
    for raw_line in answer.splitlines():
        stripped_line = raw_line.strip()
        if stripped_line.startswith("#"):
            heading = stripped_line.lstrip("#").strip().lower()
            in_reference_section = any(
                marker in heading for marker in reference_headings
            )
            in_recommendation_section = any(
                marker in heading for marker in recommendation_headings
            )
            continue
        if in_reference_section or in_recommendation_section:
            continue
        # Never inject prose prefixes into Markdown tables. Prefixing a row
        # with “待核验：” destroys the GFM table and turns it into plain text.
        if stripped_line.startswith("|") and stripped_line.endswith("|"):
            continue
        # Query strings and fragments frequently contain ``?``/``!``. Protect
        # complete Markdown links and raw URLs before sentence splitting so a
        # citation cannot be mistaken for a second factual sentence or be
        # rewritten in the middle of its href.
        protected_refs: list[str] = []

        def protect_ref(match: re.Match[str]) -> str:
            protected_refs.append(match.group(0))
            return f"\ue000REF{len(protected_refs) - 1}\ue001"

        protected_line = _MARKDOWN_LINK_RE.sub(protect_ref, raw_line)
        protected_line = _URL_RE.sub(protect_ref, protected_line)
        for raw_fragment in re.split(r"[。！？!?]\s*", protected_line):
            for ref_index, protected_ref in enumerate(protected_refs):
                raw_fragment = raw_fragment.replace(
                    f"\ue000REF{ref_index}\ue001", protected_ref
                )
            fragment = re.sub(
                r"^\s{0,3}(?:[-*]|\d+[.)]|#{1,6})\s*",
                "",
                raw_fragment,
            ).strip()
            if not fragment or fragment.startswith(">") or len(fragment) < 12:
                continue
            if re.match(
                r"^(?:来源|资料来源|参考来源|参考资料|source|sources|references?)\s*[:：]",
                fragment,
                flags=re.IGNORECASE,
            ):
                # A bibliography/source line is validated as a reference
                # below; it is not itself a factual claim.
                continue
            if re.match(
                r"^\[\^?[A-Za-z0-9_.-]{1,40}\]\s*[:：]",
                fragment,
            ):
                continue
            if _MARKDOWN_LINK_RE.fullmatch(fragment) or _URL_RE.fullmatch(fragment):
                continue
            # Several adjacent citations are still a reference tail, not a
            # second factual assertion that needs sentence verification.
            citation_remainder = _MARKDOWN_LINK_RE.sub("", fragment)
            citation_remainder = _URL_RE.sub("", citation_remainder)
            if not citation_remainder.strip(" ,，;；、"):
                continue
            lowered = fragment.lower()
            # Explicit uncertainty and evidence-gap statements already tell
            # the reader that no verified fact is being asserted.  Prefixing
            # them again produces the large, misleading “待核验” walls seen in
            # deep-research reports.
            if any(marker in lowered for marker in uncertainty_markers):
                continue
            if any(marker in lowered for marker in diagnostic_markers):
                continue
            if any(marker in lowered for marker in structural_markers):
                continue
            if re.match(
                r"^(?:建议|应当|应优先|需要|下一步|可考虑|recommend|should|must)",
                fragment,
                flags=re.IGNORECASE,
            ):
                continue
            has_ref = bool(_refs(fragment))
            has_number = bool(_factual_numbers(fragment))
            is_explicit_claim = (
                has_ref or has_number or any(marker in lowered for marker in markers)
            )
            # Only explicit factual assertions enter sentence-level checks.
            # Soft narrative / background prose must not become a “待核验墙”.
            if is_explicit_claim:
                candidates.append(fragment)
    return candidates


def _attach_document_warning(answer: str, warning: str) -> str:
    """Place a single document-level warning without rewriting every sentence."""

    if not warning:
        return answer
    if answer.lstrip().startswith("#"):
        heading, _separator, remainder = answer.partition("\n")
        return f"{heading}\n\n> {warning}\n\n{remainder.lstrip()}".rstrip()
    return f"> {warning}\n\n{answer}".rstrip()


def _collect_authorized_claims(
    branch_results: Iterable[EvidenceCarrier],
) -> tuple[list[tuple[str, set[str], set[str]]], set[str]]:
    claim_entries: list[tuple[str, set[str], set[str]]] = []
    supported_refs: set[str] = set()
    for result in branch_results:
        # A timed-out or contract-violating branch may contain useful clues,
        # but it must never authorize a final claim or citation.
        if str(getattr(result, "error", "") or "").strip():
            continue
        handoff = result.handoff or {}
        supported_refs.update(
            normalized
            for ref in handoff.get("evidence_refs") or []
            if (normalized := _normalize_ref(str(ref)))
        )
        for claim in handoff.get("claims") or []:
            fingerprint = claim_fingerprint(str(claim.get("claim") or ""))
            claim_refs = {
                normalized
                for ref in claim.get("supporting_refs") or []
                if (normalized := _normalize_ref(str(ref)))
            }
            # A branch-level bibliography does not prove every sentence in
            # that branch. Only source-bound claims may authorize final facts.
            if fingerprint and claim_refs:
                claim_entries.append(
                    (
                        str(claim.get("claim") or ""),
                        fingerprint,
                        claim_refs,
                    )
                )
            supported_refs.update(claim_refs)
    return claim_entries, supported_refs


def _analyze_answer_support(
    answer: str,
    claim_entries: list[tuple[str, set[str], set[str]]],
    supported_refs: set[str],
) -> _AnswerSupportAnalysis:
    citation_aliases = _numbered_citation_aliases(answer)
    unsupported_refs = frozenset(
        _refs(answer, citation_aliases=citation_aliases) - supported_refs
    )
    candidates = answer_claim_candidates(answer)
    if not claim_entries:
        diagnostics = AnswerVerificationDiagnostics(
            candidate_claims=len(candidates),
            unsupported_claims=len(candidates),
            unsupported_references=len(unsupported_refs),
        )
        return _AnswerSupportAnalysis(
            diagnostics=diagnostics,
            unsupported_claims=tuple(candidates),
            unsupported_refs=unsupported_refs,
        )

    unsupported_claims: list[str] = []
    supported_count = 0
    ambiguous_count = 0
    numeric_conflicts = 0
    negation_conflicts = 0
    reference_conflicts = 0
    for sentence in candidates:
        sentence_fingerprint = claim_fingerprint(sentence)
        if not sentence_fingerprint:
            continue
        scored_claims = [
            (
                claim_text,
                len(sentence_fingerprint & fingerprint)
                / max(1, min(len(sentence_fingerprint), len(fingerprint), 16)),
                claim_refs,
            )
            for claim_text, fingerprint, claim_refs in claim_entries
        ]
        matching_claims = [
            item for item in scored_claims if item[1] >= _CLAIM_OVERLAP_THRESHOLD
        ]
        sentence_numbers = _factual_numbers(sentence)
        best_match = max(
            matching_claims,
            key=lambda item: item[1],
            default=None,
        )
        # Permit one synthesis sentence to combine several matrix claims, but
        # only when additional matches contribute a value in this sentence.
        aligned_claims = [
            item
            for item in matching_claims
            if item is best_match
            or (
                _factual_numbers(item[0])
                and _factual_numbers(item[0]).issubset(sentence_numbers)
            )
        ]
        matching_numbers = {
            number
            for claim_text, _overlap, _refs_for_claim in aligned_claims
            for number in _factual_numbers(claim_text)
        }
        matching_refs = {
            ref
            for _claim_text, _overlap, refs_for_claim in aligned_claims
            for ref in refs_for_claim
        }
        numeric_conflict = not sentence_numbers.issubset(matching_numbers)
        sentence_has_negation = bool(_negations(sentence))
        negation_conflict = bool(aligned_claims) and not any(
            bool(_negations(claim_text)) == sentence_has_negation
            for claim_text, _overlap, _refs_for_claim in aligned_claims
        )
        sentence_refs = _refs(sentence, citation_aliases=citation_aliases)
        ref_conflict = bool(sentence_refs) and not sentence_refs.issubset(matching_refs)
        if not aligned_claims or numeric_conflict or negation_conflict or ref_conflict:
            unsupported_claims.append(sentence)
            numeric_conflicts += int(numeric_conflict)
            negation_conflicts += int(negation_conflict)
            reference_conflicts += int(ref_conflict)
        elif (
            best_match is not None
            and best_match[1] < _SEMANTIC_REVIEW_OVERLAP_THRESHOLD
        ):
            ambiguous_count += 1
        else:
            supported_count += 1

    diagnostics = AnswerVerificationDiagnostics(
        candidate_claims=len(candidates),
        supported_claims=supported_count,
        ambiguous_claims=ambiguous_count,
        unsupported_claims=len(unsupported_claims),
        numeric_conflicts=numeric_conflicts,
        negation_conflicts=negation_conflicts,
        reference_conflicts=reference_conflicts,
        unsupported_references=len(unsupported_refs),
    )
    return _AnswerSupportAnalysis(
        diagnostics=diagnostics,
        unsupported_claims=tuple(unsupported_claims),
        unsupported_refs=unsupported_refs,
    )


def analyze_answer_support(
    answer: str,
    branch_results: Iterable[EvidenceCarrier],
) -> AnswerVerificationDiagnostics:
    """Return diagnostics without rewriting the synthesized answer."""

    claim_entries, supported_refs = _collect_authorized_claims(branch_results)
    return _analyze_answer_support(answer, claim_entries, supported_refs).diagnostics


def verify_answer(answer: str, branch_results: Iterable[EvidenceCarrier]) -> str:
    if not answer:
        return answer
    claim_entries, supported_refs = _collect_authorized_claims(branch_results)
    if not claim_entries and not supported_refs:
        unverified_answer = re.sub(
            r"https?://\S+",
            "（未核验来源）",
            answer,
        )
        warning = (
            "待核验：当前研究分支没有形成可核验的证据矩阵，以下内容仅作为未验证草稿。"
        )
        return _attach_document_warning(unverified_answer, warning)

    analysis = _analyze_answer_support(answer, claim_entries, supported_refs)
    unsupported_refs = set(analysis.unsupported_refs)
    unsupported_claims = list(analysis.unsupported_claims)
    candidate_count = max(1, analysis.diagnostics.candidate_claims)

    # Branch bibliography alone cannot authorize sentence-level facts. Prefer
    # one document warning over prefixing every explicit claim.
    if not claim_entries:
        verified_answer = _replace_unsupported_refs(answer, unsupported_refs)
        warning = (
            "待核验：当前证据矩阵未提供充分支持；"
            "缺少逐项绑定来源的结论，正文关键事实尚未被授权。"
        )
        if unsupported_refs:
            warning += f" 同时移除 {len(unsupported_refs)} 个未出现在研究分支证据矩阵中的引用。"
        verified_answer = _attach_document_warning(verified_answer, warning)
        return verified_answer

    if not unsupported_refs and not unsupported_claims:
        return answer

    verified_answer = _replace_unsupported_refs(answer, unsupported_refs)
    wall_collapse = (
        len(unsupported_claims) > _MAX_INLINE_DOWNGRADES
        and len(unsupported_claims) / candidate_count >= _WALL_RATIO_THRESHOLD
    )
    notes: list[str] = []
    if unsupported_refs:
        notes.append(
            f"已移除 {len(unsupported_refs)} 个未出现在研究分支证据矩阵中的引用。"
        )

    if unsupported_claims and wall_collapse:
        warning = (
            f"待核验：当前证据矩阵未提供充分支持；"
            f"共 {len(unsupported_claims)} 处显式结论未能与 evidence matrix 对齐，"
            f"已改为文档级提示以避免逐句降级。"
        )
        if unsupported_refs:
            warning += f" 同时移除 {len(unsupported_refs)} 个未出现在研究分支证据矩阵中的引用。"
        verified_answer = _attach_document_warning(verified_answer, warning)
        # The title-adjacent warning is the complete document-level result.
        # Repeating the same ratio in a trailing diagnostic creates the
        # confusing head/tail duplication reported by users.
        return verified_answer
    elif unsupported_claims:
        # Keep a small number of inline markers for the strongest mismatches;
        # never prefix dozens of synthesis sentences.
        inline_claims = sorted(unsupported_claims, key=len, reverse=True)[
            :_MAX_INLINE_DOWNGRADES
        ]
        for claim in inline_claims:
            # Put the warning before the original sentence. Appending it after a
            # bare URL lets Markdown linkifiers absorb the Chinese warning into
            # the href and produces a percent-encoded broken link.
            downgraded = f"待核验：当前证据矩阵未提供充分支持；{claim}"
            verified_answer = verified_answer.replace(claim, downgraded, 1)
        notes.append(
            f"已将 {len(inline_claims)} 个未能与 evidence matrix 形成足够支撑的关键结论"
            f"降级为待核验表述"
            + (
                f"（另有 {len(unsupported_claims) - len(inline_claims)} 处未逐句标注）。"
                if len(unsupported_claims) > len(inline_claims)
                else "。"
            )
        )

    if not notes:
        return verified_answer
    return verified_answer.rstrip() + "\n\n> 证据核验提示：" + " ".join(notes)


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"(?<!\w)\d+(?:\.\d+)?%?(?!\w)", str(text or "")))


def _factual_numbers(text: str) -> set[str]:
    """Numbers that should participate in conflict checks.

    Calendar years (1900–2099) commonly appear in synthesis prose and must not
    alone force a claim mismatch against an evidence-matrix entry that omits
    the year.
    """

    # Paper-style citations such as ``[1]`` identify sources rather than
    # factual values.  Counting their sequence numbers as claim data makes an
    # otherwise exact evidence sentence fail numeric-consistency checks.
    value = re.sub(r"\[\d{1,4}\]", "", str(text or ""))
    return {
        number
        for number in _numbers(value)
        if not re.fullmatch(r"(?:19|20)\d{2}", number.rstrip("%"))
    }


def _negations(text: str) -> set[str]:
    return set(
        re.findall(
            r"(?:不|未|无|不能|无法|并非|没有|not|no|cannot|never)",
            str(text or "").lower(),
        )
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
    if not unsupported_refs:
        return text

    def replace_markdown_link(match: re.Match[str]) -> str:
        destination = _normalize_ref(match.group(2))
        return "（未核验来源）" if destination in unsupported_refs else match.group(0)

    replaced = _MARKDOWN_LINK_RE.sub(replace_markdown_link, text)

    def replace_url(match: re.Match[str]) -> str:
        ref = _normalize_ref(match.group(0))
        return "（未核验来源）" if ref in unsupported_refs else match.group(0)

    replaced = _URL_RE.sub(replace_url, replaced)
    for marker, resolved_ref in _numbered_citation_aliases(text).items():
        if resolved_ref in unsupported_refs:
            replaced = replaced.replace(marker, "（未核验来源）")
    for ref in sorted(
        (
            item
            for item in unsupported_refs
            if item.startswith("[") and item.endswith("]")
        ),
        key=len,
        reverse=True,
    ):
        replaced = replaced.replace(ref, "（未核验来源）")
    return replaced


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
