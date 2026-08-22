"""Segmenter protocol and registry."""

from __future__ import annotations

from typing import Any, Protocol

from backend.core.agents.context.objects import ContextObject


class Segmenter(Protocol):
    schema: str

    def segment(
        self,
        result: Any,
        *,
        tool_args: dict[str, Any],
        thread_id: str,
        turn_id: str | None,
        user_id: str | None,
    ) -> ContextObject: ...


class SegmenterRegistry:
    def __init__(
        self,
        segmenters: dict[str, Segmenter] | None = None,
        *,
        fallback: Segmenter | None = None,
    ):
        self._segmenters = dict(segmenters or {})
        self._fallback = fallback

    def register(self, tool_name: str, segmenter: Segmenter) -> None:
        self._segmenters[str(tool_name)] = segmenter

    def get(self, tool_name: str) -> Segmenter | None:
        return self._segmenters.get(str(tool_name or ""))

    @property
    def fallback(self) -> Segmenter | None:
        return self._fallback

    def segment(
        self,
        *,
        tool_name: str,
        result: Any,
        tool_args: dict[str, Any],
        thread_id: str,
        turn_id: str | None = None,
        user_id: str | None = None,
    ) -> ContextObject | None:
        segmenter = self.get(tool_name)
        if segmenter is None:
            segmenter = self._fallback
        if segmenter is None:
            return None
        effective_args = dict(tool_args or {})
        if segmenter is self._fallback:
            effective_args.setdefault("_tool_name", tool_name)
        return segmenter.segment(
            result,
            tool_args=effective_args,
            thread_id=str(thread_id or ""),
            turn_id=turn_id,
            user_id=user_id,
        )


__all__ = ["Segmenter", "SegmenterRegistry"]
