"""Segmenter for web_search results."""

from __future__ import annotations

import json
import time
from typing import Any

from backend.core.agents.context.objects import (
    ContextObject,
    Segment,
    make_context_object,
    make_segment,
    stable_url_sid,
)


class WebSearchSegmenter:
    schema = "web_search/v1"

    def segment(
        self,
        result: Any,
        *,
        tool_args: dict[str, Any],
        thread_id: str,
        turn_id: str | None,
        user_id: str | None,
    ) -> ContextObject:
        payload = result
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                payload = {}
        segments: list[Segment] = []
        if isinstance(payload, dict) and not payload.get("error"):
            for index, page in enumerate(payload.get("results") or [], start=1):
                if not isinstance(page, dict):
                    continue
                url = str(
                    page.get("url") or page.get("href") or page.get("link") or ""
                ).strip()
                if not url:
                    continue
                title = str(page.get("title") or url).strip()
                body = str(
                    page.get("content")
                    or page.get("markdown")
                    or page.get("description")
                    or ""
                ).strip()
                text = "\n".join(
                    part
                    for part in (f"标题：{title}", f"页面：{url}", body)
                    if part
                )
                segments.append(
                    make_segment(
                        sid=stable_url_sid(url),
                        text=text,
                        title=title,
                        locator=url,
                        score=max(0.55, 1.0 - index * 0.08),
                        tags=["web", "page"],
                    )
                )
        query = str(
            (payload.get("query") if isinstance(payload, dict) else "")
            or tool_args.get("query")
            or ""
        )
        return make_context_object(
            schema=self.schema,
            origin={
                "tool": "web_search",
                "args_digest": "",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_id": user_id,
                "created_at": time.time(),
                "last_seen_at": time.time(),
            },
            index={"query": query, "stats": {"segments": len(segments)}},
            segments=segments,
        )


__all__ = ["WebSearchSegmenter"]
