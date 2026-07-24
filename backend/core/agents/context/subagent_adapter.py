"""Structured, bounded handoff contracts between coordinated agents."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from backend.core.agents.context.summary import generate_summary, normalize_text
from backend.core.agents.context.types import (
    ClaimRecord,
    EvidenceRecord,
    SourceCoverage,
    SubagentResult,
    SubagentResultV2Model,
)

HANDOFF_SCHEMA_VERSION = "2.0"


class SubagentResultValidationError(ValueError):
    """Raised when a handoff does not satisfy the strict V2 runtime contract."""

    def __init__(self, details: Iterable[str]) -> None:
        normalized = tuple(
            str(detail).strip() for detail in details if str(detail).strip()
        )
        self.details = normalized or ("unknown validation error",)
        super().__init__("Invalid SubagentResult V2: " + "; ".join(self.details))


def validate_subagent_result_strict(result: Any) -> SubagentResultV2Model:
    """Validate a complete V2 handoff without Pydantic type coercion.

    The compatibility normalizer remains intentionally forgiving. Call this
    function at a trusted boundary after normalization when malformed types,
    unknown fields, incomplete envelopes, or inconsistent coverage counters
    must be rejected. Citation-quality warnings remain part of the valid V2
    payload and can be handled by the existing verifier policy.
    """

    try:
        return SubagentResultV2Model.model_validate(result, strict=True)
    except ValidationError as exc:
        details: list[str] = []
        for error in exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            location = ".".join(str(part) for part in error.get("loc") or ()) or "$"
            details.append(f"{location}: {error.get('msg') or 'invalid value'}")
        raise SubagentResultValidationError(details) from exc


def build_subagent_result(
    *,
    role: str,
    objective: str,
    answer: str,
    error: str | None = None,
    task_id: str | None = None,
    evidence_refs: list[str] | None = None,
    evidence_records: list[EvidenceRecord] | None = None,
    raw_artifact_refs: list[str] | None = None,
) -> SubagentResult:
    text = normalize_text(answer or error or "无有效结果")
    record_refs = [_record_ref(record) for record in evidence_records or []]
    refs = _unique([*(evidence_refs or []), *record_refs, *_extract_refs(text)])[:20]
    claims = _extract_claims(
        text,
        evidence_refs=evidence_refs,
        evidence_records=evidence_records,
    )[:12]
    result = SubagentResult(
        schema_version=HANDOFF_SCHEMA_VERSION,
        task_id=str(task_id or ""),
        subagent_type=role,
        description=generate_summary(objective, max_chars=600),
        summary=generate_summary(text, max_chars=5000),
        claims=claims,
        evidence_refs=refs,
        confidence=_estimate_confidence(text, evidence_refs=evidence_refs),
        conflicts=_extract_section_bullets(text, ("冲突", "争议", "矛盾", "conflict"))[:10],
        missing=_extract_section_bullets(text, ("缺口", "不足", "未知", "missing", "gap"))[:10],
        risks=_extract_section_bullets(text, ("风险", "限制", "不确定", "risk", "limitation"))[:10],
        suggested_next_actions=_extract_section_bullets(
            text,
            ("下一步", "建议", "待办", "next", "recommendation"),
        )[:10],
        raw_artifact_ref=(raw_artifact_refs or [None])[0],
        raw_artifact_refs=list(raw_artifact_refs or []),
    )
    result["source_coverage"] = _source_coverage(result)
    result["validation_warnings"] = validate_subagent_result(result)
    return result


def normalize_subagent_result(
    payload: Any,
    *,
    role: str,
    objective: str,
    answer_fallback: str = "",
    error: str | None = None,
    task_id: str | None = None,
    evidence_refs: list[str] | None = None,
    evidence_records: list[EvidenceRecord] | None = None,
) -> SubagentResult:
    """Normalize model JSON handoffs with fallback to text extraction."""

    fallback = build_subagent_result(
        role=role,
        objective=objective,
        answer=answer_fallback,
        error=error,
        task_id=task_id,
        evidence_refs=evidence_refs,
        evidence_records=evidence_records,
    )
    parsed = _parse_payload_dict(payload)
    if parsed is None:
        return fallback

    result = dict(fallback)
    for key in (
        "summary",
        "evidence_refs",
        "claims",
        "confidence",
        "conflicts",
        "missing",
        "risks",
        "suggested_next_actions",
        "raw_artifact_refs",
    ):
        if key in parsed:
            result[key] = parsed[key]
    result["schema_version"] = HANDOFF_SCHEMA_VERSION
    result["task_id"] = str(parsed.get("task_id") or task_id or "")
    result["subagent_type"] = str(parsed.get("subagent_type") or role)
    result["description"] = generate_summary(
        str(parsed.get("description") or objective),
        max_chars=600,
    )
    result["summary"] = generate_summary(str(result.get("summary") or ""), max_chars=5000)
    result["evidence_refs"] = _unique(str(ref) for ref in _as_list(result.get("evidence_refs")))[:20]
    result["raw_artifact_refs"] = _unique(
        str(ref) for ref in _as_list(result.get("raw_artifact_refs"))
    )[:20]
    result["raw_artifact_ref"] = (result.get("raw_artifact_refs") or [None])[0]
    result["claims"] = _normalize_claims(result.get("claims"), result["evidence_refs"])
    # Model JSON often omits supporting_refs even when retrieval already
    # captured matching evidence_records. Rebind those empty claims so
    # structured convergence does not discard recoverable facts.
    result["claims"] = _rebind_claims_supporting_refs(
        result["claims"],
        evidence_records=evidence_records or [],
        answer_text=str(answer_fallback or result.get("summary") or ""),
    )
    rebound_refs = [
        str(ref)
        for claim in result["claims"]
        for ref in claim.get("supporting_refs") or []
        if str(ref).strip()
    ]
    if rebound_refs:
        result["evidence_refs"] = _unique(
            [*result["evidence_refs"], *rebound_refs]
        )[:20]
    result["confidence"] = _bounded_float(result.get("confidence"), default=fallback["confidence"])
    for key in ("conflicts", "missing", "risks", "suggested_next_actions"):
        result[key] = [generate_summary(str(item), max_chars=300) for item in _as_list(result.get(key))][:10]
    typed = SubagentResult(**result)
    typed["source_coverage"] = _source_coverage(typed)
    typed["validation_warnings"] = validate_subagent_result(typed)
    return typed


def validate_subagent_result(result: SubagentResult) -> list[str]:
    warnings: list[str] = []
    if result.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        warnings.append("schema_version is not 2.0")
    if not str(result.get("summary") or "").strip():
        warnings.append("summary is empty")
    claims = result.get("claims") or []
    if not claims:
        warnings.append("claims are empty")
    unsupported = [
        str(claim.get("claim") or "")
        for claim in claims
        if not claim.get("supporting_refs")
    ]
    if unsupported:
        warnings.append(f"{len(unsupported)} claim(s) have no supporting_refs")
    confidence = _bounded_float(result.get("confidence"), default=0.0)
    if confidence < 0.2 and not result.get("missing"):
        warnings.append("low confidence handoff should explain missing evidence")
    return warnings


def format_subagent_handoff(
    result: SubagentResult,
    *,
    max_chars: int = 6000,
    structured_only: bool = False,
) -> str:
    """Render a complete handoff envelope within the requested soft budget.

    The previous implementation truncated the serialized XML/JSON block as one
    opaque string.  Large results could therefore lose the closing tags or
    contain invalid JSON.  Keep the byte-for-byte shape for normal results, but
    compact individual structured fields before serialization when the block is
    too large.  Structural integrity takes precedence when ``max_chars`` is
    smaller than the minimum valid envelope.
    """

    rendered = _render_subagent_handoff(result, structured_only=structured_only)
    target = max(1, int(max_chars))
    if len(rendered) <= target:
        return rendered

    for profile in _HANDOFF_COMPACTION_PROFILES:
        compact = _compact_handoff_result(result, **profile)
        rendered = _render_compact_subagent_handoff(
            compact,
            structured_only=structured_only,
        )
        if len(rendered) <= target:
            return rendered

    # A complete minimum envelope may be longer than an unrealistically small
    # caller budget.  Returning it intact is safer than emitting corrupt JSON.
    return rendered


def _render_subagent_handoff(
    result: SubagentResult,
    *,
    structured_only: bool = False,
) -> str:
    role = result.get("subagent_type") or "unknown"
    lines = [f'<agent_handoff role="{role}">']
    lines.extend(
        [
            "<subagent_result_json>",
            json.dumps(_json_safe_handoff(result), ensure_ascii=False, sort_keys=True),
            "</subagent_result_json>",
        ]
    )
    if structured_only:
        lines.append("</agent_handoff>")
        return "\n".join(lines)
    if result.get("description"):
        lines.extend(["<objective>", str(result["description"]), "</objective>"])
    lines.extend(["<result>", str(result.get("summary") or "无有效结果"), "</result>"])
    if result.get("evidence_refs"):
        lines.extend(["<evidence_refs>", *[f"- {ref}" for ref in result["evidence_refs"]], "</evidence_refs>"])
    if result.get("claims"):
        lines.append("<claims>")
        for claim in result["claims"]:
            refs = ", ".join(claim.get("supporting_refs") or [])
            confidence = claim.get("confidence", result.get("confidence", 0.5))
            lines.append(f"- claim: {claim.get('claim')}; confidence: {confidence}; refs: {refs}")
            if claim.get("conflicts"):
                lines.append(f"  conflicts: {'; '.join(claim['conflicts'])}")
            if claim.get("missing"):
                lines.append(f"  missing: {'; '.join(claim['missing'])}")
        lines.append("</claims>")
    if result.get("risks"):
        lines.extend(["<risks>", *[f"- {risk}" for risk in result["risks"]], "</risks>"])
    if result.get("suggested_next_actions"):
        lines.extend(
            [
                "<suggested_next_actions>",
                *[f"- {item}" for item in result["suggested_next_actions"]],
                "</suggested_next_actions>",
            ]
        )
    lines.append("</agent_handoff>")
    return "\n".join(lines)


_HANDOFF_COMPACTION_PROFILES: tuple[dict[str, int], ...] = (
    {
        "summary_chars": 1200,
        "description_chars": 300,
        "claim_count": 6,
        "claim_chars": 220,
        "ref_count": 12,
        "ref_chars": 220,
        "list_count": 5,
        "list_chars": 180,
    },
    {
        "summary_chars": 650,
        "description_chars": 180,
        "claim_count": 3,
        "claim_chars": 160,
        "ref_count": 6,
        "ref_chars": 160,
        "list_count": 3,
        "list_chars": 120,
    },
    {
        "summary_chars": 240,
        "description_chars": 100,
        "claim_count": 1,
        "claim_chars": 120,
        "ref_count": 2,
        "ref_chars": 100,
        "list_count": 1,
        "list_chars": 80,
    },
    {
        "summary_chars": 80,
        "description_chars": 40,
        "claim_count": 1,
        "claim_chars": 60,
        "ref_count": 0,
        "ref_chars": 40,
        "list_count": 0,
        "list_chars": 40,
    },
)


def _bounded_items(value: Any, *, count: int, chars: int) -> list[str]:
    if count <= 0:
        return []
    return [
        generate_summary(str(item), max_chars=chars)
        for item in _as_list(value)[:count]
        if str(item).strip()
    ]


def _compact_handoff_result(
    result: SubagentResult,
    *,
    summary_chars: int,
    description_chars: int,
    claim_count: int,
    claim_chars: int,
    ref_count: int,
    ref_chars: int,
    list_count: int,
    list_chars: int,
) -> SubagentResult:
    summary = generate_summary(
        str(result.get("summary") or "无有效结果"),
        max_chars=summary_chars,
    )
    evidence_refs = _bounded_items(
        result.get("evidence_refs"),
        count=ref_count,
        chars=ref_chars,
    )
    raw_artifact_refs = _bounded_items(
        result.get("raw_artifact_refs"),
        count=ref_count,
        chars=ref_chars,
    )
    compact_claims: list[ClaimRecord] = []
    raw_claims = list(result.get("claims") or [])[: max(1, claim_count)]
    if not raw_claims:
        raw_claims = [{"claim": summary, "supporting_refs": []}]
    for raw_claim in raw_claims:
        claim = raw_claim if isinstance(raw_claim, dict) else {"claim": raw_claim}
        claim_text = generate_summary(
            str(claim.get("claim") or summary),
            max_chars=claim_chars,
        )
        compact_claims.append(
            ClaimRecord(
                claim=claim_text or "无有效结果",
                supporting_refs=_bounded_items(
                    claim.get("supporting_refs"),
                    count=min(4, ref_count),
                    chars=ref_chars,
                ),
                confidence=_bounded_float(
                    claim.get("confidence"),
                    default=_bounded_float(result.get("confidence"), default=0.5),
                ),
                conflicts=_bounded_items(
                    claim.get("conflicts"),
                    count=list_count,
                    chars=list_chars,
                ),
                missing=_bounded_items(
                    claim.get("missing"),
                    count=list_count,
                    chars=list_chars,
                ),
                source_ids=_bounded_items(
                    claim.get("source_ids"),
                    count=list_count,
                    chars=list_chars,
                ),
            )
        )

    compact = SubagentResult(
        schema_version=HANDOFF_SCHEMA_VERSION,
        task_id=generate_summary(
            str(result.get("task_id") or "legacy-handoff"),
            max_chars=max(40, list_chars),
        ),
        subagent_type=generate_summary(
            str(result.get("subagent_type") or "unknown"),
            max_chars=max(40, list_chars),
        ),
        description=generate_summary(
            str(result.get("description") or "未提供任务描述"),
            max_chars=description_chars,
        ),
        summary=summary,
        claims=compact_claims,
        evidence_refs=evidence_refs,
        confidence=_bounded_float(result.get("confidence"), default=0.5),
        conflicts=_bounded_items(
            result.get("conflicts"), count=list_count, chars=list_chars
        ),
        missing=_bounded_items(
            result.get("missing"), count=list_count, chars=list_chars
        ),
        risks=_bounded_items(
            result.get("risks"), count=list_count, chars=list_chars
        ),
        suggested_next_actions=_bounded_items(
            result.get("suggested_next_actions"),
            count=list_count,
            chars=list_chars,
        ),
        raw_artifact_ref=raw_artifact_refs[0] if raw_artifact_refs else None,
        raw_artifact_refs=raw_artifact_refs,
    )
    compact["source_coverage"] = _source_coverage(compact)
    compact["validation_warnings"] = validate_subagent_result(compact)
    return compact


def _render_compact_subagent_handoff(
    result: SubagentResult,
    *,
    structured_only: bool = False,
) -> str:
    role = result.get("subagent_type") or "unknown"
    lines = [
        f'<agent_handoff role="{role}">',
        "<subagent_result_json>",
        json.dumps(_json_safe_handoff(result), ensure_ascii=False, sort_keys=True),
        "</subagent_result_json>",
    ]
    if not structured_only:
        lines.extend(
            [
                "<result>",
                str(result.get("summary") or "无有效结果"),
                "</result>",
            ]
        )
    lines.append("</agent_handoff>")
    return "\n".join(lines)


def format_handoff_bundle(
    results: Iterable[SubagentResult],
    *,
    max_chars: int = 16000,
    structured_only: bool = False,
) -> str:
    blocks: list[str] = []
    used = 0
    for result in results:
        remaining = max_chars - used
        if remaining < 300:
            break
        block = format_subagent_handoff(
            result,
            max_chars=min(6000, remaining),
            structured_only=structured_only,
        )
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)


def extract_evidence_refs(text: str) -> list[str]:
    return _unique(_extract_refs(text))[:20]


def _json_safe_handoff(result: SubagentResult) -> dict[str, Any]:
    return {
        "schema_version": result.get("schema_version") or HANDOFF_SCHEMA_VERSION,
        "task_id": result.get("task_id") or "",
        "subagent_type": result.get("subagent_type") or "unknown",
        "description": result.get("description") or "",
        "summary": result.get("summary") or "",
        "claims": result.get("claims") or [],
        "evidence_refs": result.get("evidence_refs") or [],
        "confidence": result.get("confidence", 0.5),
        "conflicts": result.get("conflicts") or [],
        "missing": result.get("missing") or [],
        "risks": result.get("risks") or [],
        "suggested_next_actions": result.get("suggested_next_actions") or [],
        "raw_artifact_refs": result.get("raw_artifact_refs") or [],
        "source_coverage": result.get("source_coverage") or _source_coverage(result),
        "validation_warnings": result.get("validation_warnings") or [],
    }


def _source_coverage(result: SubagentResult) -> SourceCoverage:
    claims = result.get("claims") or []
    refs = _unique([*(result.get("evidence_refs") or []), *(result.get("raw_artifact_refs") or [])])
    claims_with_refs = sum(1 for claim in claims if claim.get("supporting_refs"))
    return SourceCoverage(
        total_claims=len(claims),
        claims_with_refs=claims_with_refs,
        unsupported_claims=max(0, len(claims) - claims_with_refs),
        kb_refs=sum(1 for ref in refs if _is_kb_ref(ref)),
        web_refs=sum(1 for ref in refs if str(ref).startswith(("http://", "https://"))),
        project_data_refs=sum(1 for ref in refs if str(ref).startswith("[project-db:")),
        raw_artifact_refs=sum(1 for ref in refs if str(ref).startswith("artifact://context/")),
    )


def _is_kb_ref(ref: str) -> bool:
    text = str(ref)
    return text.startswith("[") and not text.startswith("[project-db:") and "#" in text


def _parse_payload_dict(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, str):
        return None
    text = payload.strip()
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    elif not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_claims(value: Any, _fallback_refs: list[str]) -> list[ClaimRecord]:
    claims: list[ClaimRecord] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            claim_text = generate_summary(str(item.get("claim") or ""), max_chars=260)
            if not claim_text:
                continue
            refs = _unique(str(ref) for ref in _as_list(item.get("supporting_refs")))[:8]
            missing = [str(entry) for entry in _as_list(item.get("missing"))][:10]
            if not refs and not missing:
                missing = ["缺少与该结论直接匹配的证据引用"]
            claims.append(
                ClaimRecord(
                    claim=claim_text,
                    supporting_refs=refs,
                    confidence=_bounded_float(item.get("confidence"), default=0.5),
                    conflicts=[str(entry) for entry in _as_list(item.get("conflicts"))][:10],
                    missing=missing,
                    source_ids=[str(entry) for entry in _as_list(item.get("source_ids"))][:10],
                )
            )
        else:
            claim_text = generate_summary(str(item), max_chars=260)
            if claim_text:
                claims.append(
                    ClaimRecord(
                        claim=claim_text,
                        supporting_refs=[],
                        confidence=0.5,
                        conflicts=[],
                        missing=["缺少与该结论直接匹配的证据引用"],
                        source_ids=[],
                    )
                )
    return claims[:12]


def _rebind_claims_supporting_refs(
    claims: list[ClaimRecord],
    *,
    evidence_records: list[EvidenceRecord],
    answer_text: str = "",
) -> list[ClaimRecord]:
    """Fill empty supporting_refs from captured evidence when text overlap allows.

    Structured convergence models frequently emit claim text without copying
    supporting_refs. The text-extraction path already knows how to bind
    records; reuse that matcher instead of discarding recoverable facts.
    """

    if not claims:
        return claims
    citation_refs = _extract_citation_ref_map(answer_text)
    rebound: list[ClaimRecord] = []
    for claim in claims:
        existing_refs = [
            str(ref).strip()
            for ref in claim.get("supporting_refs") or []
            if str(ref).strip()
        ]
        if existing_refs:
            rebound.append(claim)
            continue
        claim_text = str(claim.get("claim") or "").strip()
        if not claim_text:
            rebound.append(claim)
            continue
        matched_records = _match_claim_records(claim_text, evidence_records)
        refs = _unique(
            [
                *[_record_ref(record) for record in matched_records],
                *_resolve_citation_refs(claim_text, citation_refs),
                *_extract_refs(claim_text),
            ]
        )[:8]
        if not refs:
            rebound.append(claim)
            continue
        missing = [
            str(item)
            for item in claim.get("missing") or []
            if "缺少与该结论直接匹配的证据引用" not in str(item)
        ]
        source_ids = _unique(
            [
                str(record.get("source_id") or record.get("id") or "")
                for record in matched_records
            ]
        )
        rebound.append(
            ClaimRecord(
                claim=claim_text,
                supporting_refs=refs,
                confidence=max(
                    _bounded_float(claim.get("confidence"), default=0.5),
                    _estimate_confidence(claim_text, evidence_refs=refs),
                ),
                conflicts=[str(item) for item in claim.get("conflicts") or []][:10],
                missing=missing[:10],
                source_ids=source_ids[:10],
            )
        )
    return rebound


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _extract_refs(text: str) -> list[str]:
    urls = [item.rstrip(").,;:，。；：") for item in re.findall(r"https?://\S+", text)]
    source_lines = []
    for line in text.splitlines():
        stripped = line.strip(" -*\t")
        if re.search(r"(来源|出处|文献|source|citation|file|文件)", stripped, re.IGNORECASE):
            source_lines.append(generate_summary(stripped, max_chars=300))
    return [*urls, *source_lines]


def _extract_claims(
    text: str,
    *,
    evidence_refs: list[str] | None = None,
    evidence_records: list[EvidenceRecord] | None = None,
) -> list[ClaimRecord]:
    candidates: list[str] = []
    for line in text.splitlines():
        stripped = re.sub(r"^[-*•\d.\s]+", "", line.strip())
        if len(stripped) < 12:
            continue
        is_table_row = (
            stripped.startswith("|")
            and stripped.endswith("|")
            and not re.fullmatch(r"\|?[\s:|-]+\|?", stripped)
        )
        if is_table_row or re.search(r"(结论|表明|显示|证明|认为|指出|应当|可以|需要|风险|限制|不足|confirms|shows|indicates)", stripped, re.IGNORECASE):
            candidates.append(generate_summary(stripped, max_chars=260))
    if not candidates:
        sentences = re.split(r"(?<=[。！？.!?])\s*", text)
        candidates = [generate_summary(sentence, max_chars=260) for sentence in sentences if len(sentence.strip()) >= 20]
    claims: list[ClaimRecord] = []
    citation_refs = _extract_citation_ref_map(text)
    for claim in _unique(candidates)[:12]:
        matched_records = _match_claim_records(claim, evidence_records or [])
        refs = _unique(
            [
                *[_record_ref(record) for record in matched_records],
                *_resolve_citation_refs(claim, citation_refs),
                *_extract_refs(claim),
            ]
        )[:8]
        source_ids = _unique(
            [
                str(record.get("source_id") or record.get("id") or "")
                for record in matched_records
            ]
        )
        confidence = _estimate_confidence(claim, evidence_refs=refs)
        claims.append(
            ClaimRecord(
                claim=claim,
                supporting_refs=refs,
                confidence=confidence,
                conflicts=[],
                missing=[] if refs else ["缺少与该结论直接匹配的证据引用"],
                source_ids=source_ids,
            )
        )
    return claims


def _extract_citation_ref_map(text: str) -> dict[str, list[str]]:
    """Resolve Markdown footnote definitions back to traceable URLs.

    Research models often write ``[^1]`` beside a claim and place the real URL
    in a reference definition at the end. Treating the marker and definition
    independently loses the claim/source relationship even though the source
    is present in the retrieved answer.
    """

    citation_refs: dict[str, list[str]] = {}
    for line in str(text or "").splitlines():
        match = re.match(
            r"^\s*(?:[-*]\s*)?\[\^?([A-Za-z0-9_.-]{1,40})\]\s*[:：]\s*(.+)$",
            line,
        )
        if match is None:
            continue
        key, definition = match.groups()
        refs = [
            item.rstrip(").,;:，。；：")
            for item in re.findall(r"https?://\S+", definition)
        ]
        if refs:
            citation_refs[key] = _unique(refs)
    return citation_refs


def _resolve_citation_refs(
    claim: str,
    citation_refs: dict[str, list[str]],
) -> list[str]:
    resolved: list[str] = []
    for key in re.findall(r"\[\^?([A-Za-z0-9_.-]{1,40})\]", claim):
        resolved.extend(citation_refs.get(key) or [])
    return _unique(resolved)


def _match_claim_records(claim: str, records: list[EvidenceRecord]) -> list[EvidenceRecord]:
    claim_terms = _match_terms(claim)
    if not claim_terms:
        return []
    claim_numbers = _match_numbers(claim)
    scored: list[tuple[float, EvidenceRecord]] = []
    for record in records:
        record_text = " ".join(
            [
                str(record.get("claim") or ""),
                str(record.get("content") or ""),
                str(record.get("doc_title") or ""),
            ]
        )
        if claim_numbers and not claim_numbers.issubset(_match_numbers(record_text)):
            continue
        record_terms = _match_terms(record_text)
        overlap = len(claim_terms & record_terms) / max(1, len(claim_terms))
        if overlap >= 0.18:
            scored.append((overlap, record))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return []
    minimum_score = max(0.25, scored[0][0] * 0.8)
    return [record for score, record in scored[:4] if score >= minimum_score]


def _match_terms(text: str) -> set[str]:
    lowered = str(text or "").lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_./:%+-]{1,}", lowered))
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        terms.update(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return terms


def _match_numbers(text: str) -> set[str]:
    # Calendar years often appear only in claim prose and must not block an
    # otherwise strong overlap against retrieved chunks that omit the year.
    return {
        number
        for number in re.findall(r"(?<!\w)\d+(?:\.\d+)?%?(?!\w)", str(text or ""))
        if not re.fullmatch(r"(?:19|20)\d{2}", number.rstrip("%"))
    }


def _record_ref(record: EvidenceRecord) -> str:
    raw_ref = str(record.get("raw_ref") or "")
    if raw_ref.startswith(("http://", "https://")):
        return raw_ref
    title = str(record.get("doc_title") or record.get("source_id") or "").strip()
    chunk_id = str(record.get("chunk_id") or "").strip()
    if title and chunk_id:
        return f"[{title}#{chunk_id}]"
    return f"[{title}]" if title else ""


def _estimate_confidence(text: str, *, evidence_refs: list[str] | None = None) -> float:
    refs = evidence_refs or _extract_refs(text)
    confidence = 0.45
    if refs:
        confidence += 0.25
    if re.search(r"(不确定|不足|缺少|无法确认|unknown|uncertain|insufficient)", text, re.IGNORECASE):
        confidence -= 0.15
    if re.search(r"(数据|标准|文献|来源|出处|http|doi|reference)", text, re.IGNORECASE):
        confidence += 0.15
    return max(0.05, min(0.95, confidence))


def _extract_section_bullets(text: str, headings: tuple[str, ...]) -> list[str]:
    results: list[str] = []
    active = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            active = any(term.lower() in stripped.lower() for term in headings)
            continue
        if active and re.match(r"^[-*•]\s+", stripped):
            results.append(re.sub(r"^[-*•]\s+", "", stripped))
    return results


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
