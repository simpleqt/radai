"""答案支撑分析:答案声明候选抽取、授权声明收集、事实数字/否定词比对与答案核验。(拆自 evidence_verifier.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from collections.abc import Iterable
import re
from backend.application.agent_workflows.evidence_verifier_citations import (
    _normalize_ref,
    _numbered_citation_aliases,
    _refs,
    _replace_unsupported_refs,
)
from backend.application.agent_workflows.evidence_verifier_models import (
    AnswerVerificationDiagnostics,
    EvidenceCarrier,
    _AnswerSupportAnalysis,
    _MARKDOWN_LINK_RE,
    _URL_RE,
    claim_fingerprint,
)

logger = get_logger(__name__)


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
        citation_aliases = _numbered_citation_aliases(answer)
        unverified_answer = _replace_unsupported_refs(
            answer,
            _refs(answer, citation_aliases=citation_aliases),
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
