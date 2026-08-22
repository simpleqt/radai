"""上下文台账:证据记录构建与 multi_kb 文本证据解析(拆自 context/ledger.py)。"""

from __future__ import annotations

import logging

import re
import time
import uuid

from backend.core.agents.context.summary import generate_summary, normalize_text
from backend.core.agents.context.types import (
    ContextCategory,
    ContextItem,
    EvidenceRecord,
)


logger = logging.getLogger(__name__)


def context_item_to_record(item: ContextItem) -> EvidenceRecord:
    kind = str(item.get("kind") or "generic")
    category: ContextCategory = "tool_result"
    if kind == "retrieval_evidence":
        category = "retrieval"
    elif kind == "skill_rule":
        category = "skills"
    elif kind == "artifact_ref":
        category = "artifact"
    elif kind == "subagent_result":
        category = "handoff"
    return EvidenceRecord(
        id=str(item.get("id") or f"ev_{uuid.uuid4().hex[:12]}"),
        category=category,
        source_id=str(item.get("source_tool") or ""),
        chunk_id=str(item.get("id") or ""),
        doc_title=str(item.get("source_tool") or ""),
        score=float(item.get("importance") or 0.5),
        timestamp=float(item.get("created_at") or time.time()),
        kind=kind,
        claim=str(item.get("summary") or ""),
        source_tool=str(item.get("source_tool") or ""),
        reliability=float(item.get("importance") or 0.5),
        raw_ref=item.get("raw_ref"),
        tags=list(item.get("tags") or []),
        content=generate_summary(
            item.get("prompt_content") or item.get("summary") or "",
            max_chars=2400,
        ),
        turn_id=item.get("turn_id"),
    )


def make_evidence_record(
    *,
    source_tool: str,
    content: object,
    category: ContextCategory = "tool_result",
    source_id: str | None = None,
    chunk_id: str | None = None,
    doc_title: str | None = None,
    score: float | None = None,
    kind: str | None = None,
    raw_ref: str | None = None,
    tags: list[str] | None = None,
    turn_id: str | None = None,
    retrieval_seq: int | None = None,
) -> EvidenceRecord:
    text = normalize_text(content)
    stable_key = "|".join(
        [
            source_tool,
            source_id or "",
            chunk_id or "",
            extract_claim_summary(text, max_chars=160),
        ]
    )
    stable_id = f"ev_{uuid.uuid5(uuid.NAMESPACE_URL, stable_key).hex[:12]}"
    return EvidenceRecord(
        id=stable_id,
        category=category,
        source_id=source_id or source_tool,
        chunk_id=chunk_id or "",
        doc_title=doc_title or source_id or source_tool,
        score=float(score if score is not None else 0.5),
        timestamp=time.time(),
        kind=kind or category,
        claim=extract_claim_summary(text, max_chars=800),
        source_tool=source_tool,
        reliability=0.85 if category in {"retrieval", "handoff"} else 0.65,
        raw_ref=raw_ref,
        tags=tags or [],
        content=generate_summary(text, max_chars=2400),
        turn_id=turn_id,
        retrieval_seq=int(retrieval_seq or 0),
    )


def parse_multikb_text_evidence(
    text: str,
    *,
    source_tool: str = "multi_kb_search",
    raw_ref: str | None = None,
) -> list[EvidenceRecord]:
    content = normalize_text(text)
    records: list[EvidenceRecord] = []
    pattern = re.compile(r"^###\s*片段\s*\d+（来自：(.+?)）\s*$", re.MULTILINE)
    matches = list(pattern.finditer(content))
    for idx, match in enumerate(matches, start=1):
        start = match.end()
        end = matches[idx].start() if idx < len(matches) else len(content)
        body = content[start:end].strip()
        if not body:
            continue
        records.append(
            make_evidence_record(
                source_tool=source_tool,
                content=body,
                category="retrieval",
                source_id=match.group(1).strip(),
                chunk_id=f"fragment-{idx}",
                doc_title=match.group(1).strip(),
                score=max(0.1, 1.0 / idx),
                kind="retrieval",
                raw_ref=raw_ref,
                tags=["knowledge_base", "parsed_fragment"],
            )
        )
    if records:
        return records
    if content:
        return [
            make_evidence_record(
                source_tool=source_tool,
                content=content,
                category="retrieval",
                kind="retrieval",
                raw_ref=raw_ref,
                tags=["knowledge_base", "fallback_text"],
            )
        ]
    return []


def extract_claim_summary(text: object, *, max_chars: int = 700) -> str:
    content = normalize_text(text).strip()
    if not content:
        return ""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    priority_lines = [
        line
        for line in lines
        if re.search(
            r"(\d|来源|出处|文献|标准|结论|发现|结果|confidence|source)",
            line,
            re.IGNORECASE,
        )
    ]
    selected = priority_lines[:4] or lines[:4]
    summary = "\n".join(selected)
    return generate_summary(summary, max_chars=max_chars)


def _token_overlap(
    query: str,
    text: str,
    *,
    query_terms: list[str] | None = None,
    text_terms: list[str] | None = None,
) -> float:
    query_tokens = set(
        query_terms if query_terms is not None else _simple_terms(query)
    )
    if not query_tokens:
        return 0.0
    text_tokens = set(text_terms if text_terms is not None else _simple_terms(text))
    if not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / max(1, len(query_tokens))


def _simple_terms(text: str) -> list[str]:
    value = text or ""
    terms = [term.lower() for term in re.findall(r"[a-zA-Z_][a-zA-Z0-9_.-]{1,}", value)]
    terms.extend(re.findall(r"\d+(?:\.\d+)?", value))
    cjk_runs = re.findall(r"[\u4e00-\u9fff]{2,}", value)
    for run in cjk_runs:
        terms.append(run)
        for size in (2, 3):
            terms.extend(
                run[idx : idx + size] for idx in range(max(0, len(run) - size + 1))
            )
    return terms


def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _escape_body(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
