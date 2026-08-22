"""Fallback segmenter for any tool result without a dedicated adapter."""

from __future__ import annotations

import time
from typing import Any

from backend.core.agents.context.objects import (
    ContextObject,
    make_context_object,
    split_text_blocks,
)


class FallbackSegmenter:
    schema = "text/v1"

    def __init__(self, *, max_chars_per_block: int = 1500):
        self._max_chars_per_block = max(200, int(max_chars_per_block))

    def segment(
        self,
        result: Any,
        *,
        tool_args: dict[str, Any],
        thread_id: str,
        turn_id: str | None,
        user_id: str | None,
    ) -> ContextObject:
        text = str(result if isinstance(result, str) else result or "")
        segments = split_text_blocks(
            text,
            max_chars=self._max_chars_per_block,
            prefix="blk",
        )
        return make_context_object(
            schema=self.schema,
            origin={
                "tool": str(tool_args.get("_tool_name") or "unknown"),
                "args_digest": "",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_id": user_id,
                "created_at": time.time(),
                "last_seen_at": time.time(),
            },
            index={"stats": {"segments": len(segments)}},
            segments=segments,
        )


__all__ = ["FallbackSegmenter"]
