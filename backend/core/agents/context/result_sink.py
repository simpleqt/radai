"""Normalize large tool outputs into structured context records."""

from __future__ import annotations

import logging

from backend.core.agents.context.artifact_store import (
    ContextArtifactStore,
    create_context_artifact_store,
)
from backend.core.agents.context.policy import context_env_int
from backend.core.agents.context.summary import (
    extract_keywords,
    generate_summary,
    normalize_text,
)
from backend.core.agents.context.types import (
    ContextItem,
    ContextKind,
    make_context_item,
)

logger = logging.getLogger(__name__)

RETRIEVAL_TOOLS = frozenset(
    {"multi_kb_search", "web_search", "academic_search", "chat_file_search"}
)


class ResultSink:
    def __init__(
        self,
        artifact_store: ContextArtifactStore | None = None,
        *,
        persist_threshold: int | None = None,
        summary_max_chars: int = 4000,
        artifact_attributes: dict[str, object] | None = None,
    ):
        self._artifact_store = artifact_store or create_context_artifact_store()
        self._artifact_attributes = dict(artifact_attributes or {})
        resolved_threshold = (
            context_env_int(
                "AGENT_LARGE_RESULT_THRESHOLD_CHARS", 3000, minimum=600
            )
            if persist_threshold is None
            else persist_threshold
        )
        self._persist_threshold = max(1, resolved_threshold)
        self._summary_max_chars = max(200, summary_max_chars)

    def ingest(
        self,
        *,
        source_tool: str,
        raw_content: object,
        thread_id: str | None = None,
        turn_id: str | None = None,
        importance_hint: float | None = None,
        tags: list[str] | None = None,
        kind_override: ContextKind | None = None,
    ) -> ContextItem:
        content = normalize_text(raw_content)
        kind = kind_override or self._infer_kind(source_tool)
        raw_ref = None
        if len(content) >= self._persist_threshold:
            artifact_attributes = dict(self._artifact_attributes)
            if thread_id:
                artifact_attributes.setdefault("owner_thread_id", str(thread_id))
            raw_ref = self._artifact_store.store(
                content,
                thread_id=thread_id,
                source_tool=source_tool,
                attributes=artifact_attributes,
            )
        item = make_context_item(
            kind=kind,
            source_tool=source_tool,
            summary=generate_summary(content, max_chars=self._summary_max_chars),
            keywords=extract_keywords(content),
            importance=importance_hint if importance_hint is not None else self._default_importance(kind),
            raw_ref=raw_ref,
            turn_id=turn_id,
            tags=sorted({*(tags or []), *self._default_tags(kind)}),
        )
        logger.info(
            "[ResultSink] ingested tool=%s kind=%s id=%s chars=%d persisted=%s",
            source_tool,
            kind,
            item["id"],
            len(content),
            bool(raw_ref),
        )
        return item

    @staticmethod
    def _infer_kind(source_tool: str) -> ContextKind:
        if source_tool in RETRIEVAL_TOOLS:
            return "retrieval_evidence"
        if source_tool in {"task", "subagent"}:
            return "subagent_result"
        return "analysis_result"

    @staticmethod
    def _default_importance(kind: ContextKind) -> float:
        if kind == "retrieval_evidence":
            return 0.9
        if kind == "subagent_result":
            return 0.85
        return 0.7

    @staticmethod
    def _default_tags(kind: ContextKind) -> list[str]:
        return [kind, "tool_result"]
