"""Segmenter for multi_kb_search results."""

from __future__ import annotations

import time
from typing import Any

from backend.core.agents.context.ledger import parse_multikb_text_evidence
from backend.core.agents.context.objects import (
    ContextObject,
    Segment,
    make_context_object,
    make_segment,
    sanitize_sid,
    sid_content_digest,
    split_text_blocks,
)


class MultiKbSegmenter:
    schema = "multi_kb_search/v1"

    def segment(
        self,
        result: Any,
        *,
        tool_args: dict[str, Any],
        thread_id: str,
        turn_id: str | None,
        user_id: str | None,
    ) -> ContextObject:
        segments: list[Segment] = []
        records: list[dict[str, Any]] = []
        if isinstance(result, dict):
            records = list(result.get("evidence_records") or [])
        elif isinstance(result, str):
            records = list(parse_multikb_text_evidence(result))
        if records:
            for rank, record in enumerate(records, start=1):
                if not isinstance(record, dict):
                    continue
                content = str(record.get("content") or record.get("claim") or "")
                if not content.strip():
                    continue
                source_id = str(record.get("source_id") or "kb")
                # 兜底 chunk_id 掺内容指纹:rank-N 是固定名,两次检索的不同
                # 证据会在台账裸 sid 去重下相互指针化(真实 chunk_id 本身唯一)。
                chunk_id = str(
                    record.get("chunk_id")
                    or f"rank-{rank}-{sid_content_digest(content)}"
                )
                sid = sanitize_sid(f"{source_id}:{chunk_id}")
                segments.append(
                    make_segment(
                        sid=sid,
                        text=content,
                        title=str(
                            record.get("doc_title")
                            or record.get("source_id")
                            or f"{source_id} chunk {chunk_id}"
                        ),
                        locator=str(record.get("chunk_id") or ""),
                        score=float(record.get("score") or 1.0 / rank),
                        tags=["knowledge_base", "raw_chunk"],
                    )
                )
        if not segments:
            text = str(result.get("text") if isinstance(result, dict) else result or "")
            segments = split_text_blocks(text, prefix="kb")
        query = str(tool_args.get("query") or "")
        return make_context_object(
            schema=self.schema,
            origin={
                "tool": "multi_kb_search",
                "args_digest": _args_digest(tool_args),
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_id": user_id,
                "created_at": time.time(),
                "last_seen_at": time.time(),
            },
            index={
                "query": query,
                "stats": {
                    "segments": len(segments),
                    "source": "evidence_records" if records else "text",
                },
            },
            segments=segments,
        )


def _args_digest(args: dict[str, Any]) -> str:
    import hashlib
    import json

    payload = json.dumps(
        {key: value for key, value in args.items() if key not in {"config"}},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = ["MultiKbSegmenter"]
