"""First-class context objects and projection contracts.

The design goal is to make "one complete tool output" a single immutable,
content-addressed object whose segments can be projected under a token budget
by exactly one renderer (see ``projection.py``).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from typing_extensions import TypedDict

Fidelity = Literal["full", "compressed", "headline", "omitted"]


class Segment(TypedDict, total=False):
    sid: str
    title: str
    locator: str
    score: float
    tags: list[str]
    text: str
    tokens: int


class ContextObject(TypedDict, total=False):
    oid: str
    schema: str
    origin: dict[str, Any]
    owners: list[dict[str, Any]]
    index: dict[str, Any]
    segments: list[Segment]
    derived_from: list[str]


class ProjectionEntry(TypedDict):
    sid: str
    fidelity: Fidelity
    tokens: int


class ProjectionReceipt(TypedDict, total=False):
    oid: str
    handle: str
    turn_id: str
    entries: list[ProjectionEntry]
    tokens_spent: int
    overhead_tokens: int
    omitted_sids: list[str]
    reason: str


def estimate_tokens(text: object) -> int:
    """Deterministic conservative token estimate shared by object writers."""

    content = str(text or "")
    if not content:
        return 0
    cjk = sum(1 for ch in content if "\u4e00" <= ch <= "\u9fff")
    other = max(0, len(content) - cjk)
    return max(1, math.ceil(cjk * 1.5 + other / 3.0))


def _canonical_payload(obj: ContextObject) -> dict[str, Any]:
    """Content identity covers schema, index, segments and derivation only.

    Origin metadata (thread, turn, timestamps) is intentionally excluded so
    identical tool output keeps one content-addressed oid across turns.
    """

    return {
        "schema": str(obj.get("schema") or "text/v1"),
        "index": dict(obj.get("index") or {}),
        "segments": list(obj.get("segments") or []),
        "derived_from": list(obj.get("derived_from") or []),
    }


def content_digest(obj: ContextObject) -> str:
    """Return the content-addressed oid for an object."""

    payload = _canonical_payload(obj)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def make_segment(
    *,
    sid: str,
    text: str,
    title: str = "",
    locator: str = "",
    score: float = 0.0,
    tags: list[str] | None = None,
) -> Segment:
    body = str(text or "").strip()
    return Segment(
        sid=str(sid or "").strip(),
        title=str(title or sid or "").strip(),
        locator=str(locator or "").strip(),
        score=max(0.0, min(1.0, float(score or 0.0))),
        tags=list(tags or []),
        text=body,
        tokens=estimate_tokens(body),
    )


def make_context_object(
    *,
    schema: str,
    origin: dict[str, Any],
    segments: list[Segment],
    index: dict[str, Any] | None = None,
    derived_from: list[str] | None = None,
) -> ContextObject:
    """Build a content-addressed object and fill segment token counts."""

    normalized_segments: list[Segment] = []
    seen_sids: set[str] = set()
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        sid = str(segment.get("sid") or "").strip()
        if not sid or sid in seen_sids:
            continue
        seen_sids.add(sid)
        normalized = make_segment(
            sid=sid,
            text=str(segment.get("text") or ""),
            title=str(segment.get("title") or ""),
            locator=str(segment.get("locator") or ""),
            score=float(segment.get("score") or 0.0),
            tags=list(segment.get("tags") or []),
        )
        normalized_segments.append(normalized)
    obj = ContextObject(
        schema=str(schema or "text/v1").strip(),
        origin=dict(origin or {}),
        index=dict(index or {}),
        segments=normalized_segments,
        derived_from=list(derived_from or []),
    )
    obj["oid"] = content_digest(obj)
    return obj


def find_segment(obj: ContextObject, sid: str) -> Segment | None:
    for segment in obj.get("segments") or []:
        if str(segment.get("sid") or "") == sid:
            return segment
    return None


def sid_content_digest(text: str) -> str:
    """8 位内容指纹,掺入 sid 消除跨对象碰撞。

    固定名 sid(blk:N / tbl:rows:N)在能力台账的请求级裸 sid 去重下,会让
    同一请求内第二次工具调用的不同数据被误判为\u201c重复片段\u201d整体指针化;
    掺入内容指纹后,sid 相同当且仅当文本相同——此时指针化才是正确语义。
    """

    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:8]


def split_text_blocks(
    text: str,
    *,
    max_chars: int = 1500,
    prefix: str = "blk",
) -> list[Segment]:
    """Fallback chunker that keeps segment identity stable for equal input."""

    body = str(text or "").strip()
    if not body:
        return []
    limit = max(200, int(max_chars))
    blocks: list[str] = []
    remainder = body
    while remainder:
        if len(remainder) <= limit:
            blocks.append(remainder)
            break
        boundary = remainder.rfind("\n", 0, limit)
        if boundary < limit // 2:
            boundary = remainder.rfind("。", 0, limit)
        if boundary < limit // 2:
            boundary = limit
        blocks.append(remainder[:boundary].rstrip())
        remainder = remainder[boundary:].lstrip()
    segments: list[Segment] = []
    for index, block in enumerate(blocks, start=1):
        segments.append(
            make_segment(
                sid=f"{prefix}:{index}:{sid_content_digest(block)}",
                text=block,
                title=f"{prefix} {index}",
                locator=f"block-{index}",
                score=max(0.0, 1.0 - index * 0.05),
            )
        )
    return segments


def stable_url_sid(url: str) -> str:
    digest = hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()[:8]
    return f"web:{digest}"


def sanitize_sid(value: str) -> str:
    raw = str(value or "")
    cleaned = re.sub(r"[^a-zA-Z0-9_:.-]", "_", raw).strip("._")
    if len(cleaned) > 160:
        # A bare prefix cut makes two long sids sharing 160 chars collide:
        # the ledger/term-cache/seen-set would then treat different segments
        # as one. Keep the cap but disambiguate with a digest of the input.
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned[:151]}.{digest}"
    return cleaned or "unknown"


__all__ = [
    "ContextObject",
    "Fidelity",
    "ProjectionEntry",
    "ProjectionReceipt",
    "Segment",
    "content_digest",
    "sid_content_digest",
    "estimate_tokens",
    "find_segment",
    "make_context_object",
    "make_segment",
    "sanitize_sid",
    "split_text_blocks",
    "stable_url_sid",
]
