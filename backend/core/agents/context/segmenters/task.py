"""Segmenter for subagent handoff results."""

from __future__ import annotations

import time
from typing import Any

from backend.core.agents.context.objects import (
    ContextObject,
    Segment,
    make_context_object,
    make_segment,
    sanitize_sid,
    sid_content_digest,
)


class TaskSegmenter:
    schema = "task/v1"

    def segment(
        self,
        result: Any,
        *,
        tool_args: dict[str, Any],
        thread_id: str,
        turn_id: str | None,
        user_id: str | None,
    ) -> ContextObject:
        payload = result if isinstance(result, dict) else {}
        task_id = str(payload.get("task_id") or tool_args.get("task_id") or "task")
        segments: list[Segment] = []
        claims = payload.get("claims") or []
        if isinstance(claims, list):
            for index, claim in enumerate(claims, start=1):
                if not isinstance(claim, dict):
                    continue
                claim_text = str(claim.get("claim") or "")
                refs = " | ".join(str(item) for item in claim.get("supporting_refs") or [])
                segments.append(
                    make_segment(
                        sid=sanitize_sid(f"handoff:{task_id}:{index}"),
                        text=claim_text + (f"\nrefs={refs}" if refs else ""),
                        title=f"交接声明 {index}",
                        locator=f"handoff:{task_id}:{index}",
                        score=float(claim.get("confidence") or 0.5),
                        tags=["handoff", "claim"],
                    )
                )
        summary = str(payload.get("summary") or "").strip()
        if summary:
            segments.append(
                make_segment(
                    sid=sanitize_sid(f"handoff:{task_id}:summary"),
                    text=summary,
                    title="交接摘要",
                    locator=f"handoff:{task_id}:summary",
                    score=0.9,
                    tags=["handoff", "summary"],
                )
            )
        if not segments:
            raw_text = str(result or "")
            segments.append(
                make_segment(
                    # task 工具返回字符串,payload 恒空、task_id 恒为 "task":
                    # 固定 sid 会让同请求内多个子代理结果被台账裸 sid 去重互相
                    # 指针化,第二个及后续子代理的产出在模型可见面丢失,
                    # context_read 也只能解析到最后注册的对象。掺内容指纹保证
                    # 每个子代理结果有独立可寻址的 sid。
                    sid=sanitize_sid(
                        f"handoff:{task_id}:raw:{sid_content_digest(raw_text)}"
                    ),
                    text=raw_text,
                    title="交接原始结果",
                    locator="raw",
                    score=0.5,
                )
            )
        derived = [
            str(ref)
            for ref in [
                *(payload.get("raw_artifact_refs") or []),
                payload.get("raw_artifact_ref"),
            ]
            if str(ref or "").startswith("artifact://context/")
        ]
        return make_context_object(
            schema=self.schema,
            origin={
                "tool": "task",
                "args_digest": "",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_id": user_id,
                "created_at": time.time(),
                "last_seen_at": time.time(),
            },
            index={"stats": {"segments": len(segments), "task_id": task_id}},
            segments=segments,
            derived_from=derived,
        )


__all__ = ["TaskSegmenter"]
