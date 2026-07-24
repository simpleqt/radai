"""Compatibility adapter from public string tool outputs to typed metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolResultMetadata:
    source: str = "tool_output"
    target_document_match: bool | None = None
    budget_exhausted: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = dict(self.attributes)
        payload["source"] = self.source
        if self.target_document_match is not None:
            payload["target_document_match"] = self.target_document_match
        if self.budget_exhausted:
            payload["budget_exhausted"] = True
        return payload


def parse_tool_result_metadata(tool_name: str | None, raw_output: Any) -> ToolResultMetadata:
    text = str(raw_output or "")
    if tool_name != "multi_kb_search":
        return ToolResultMetadata()
    target_match: bool | None = None
    if "<target_document_match>false</target_document_match>" in text:
        target_match = False
    elif "<target_document_match>true</target_document_match>" in text:
        target_match = True
    return ToolResultMetadata(
        target_document_match=target_match,
        budget_exhausted="知识库检索次数已达上限" in text,
    )
