"""Unified construction and request lifecycle for agent context management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.core.agents.context.artifact_store import (
    ContextArtifactStore,
    create_context_artifact_store,
)
from backend.core.agents.context.capture import (
    ContextArtifactReadCaptureHandler,
    ContextCaptureRegistry,
    LargeResultCaptureHandler,
    MultiKbCaptureHandler,
    ProjectBootstrapCaptureHandler,
    ProjectEvidenceCaptureHandler,
    SkillCaptureHandler,
    WebSearchCaptureHandler,
)
from backend.core.agents.context.history_compactor import HistoryCompactor
from backend.core.agents.context.ledger import (
    ContextLedger,
    TokenCounter,
    context_item_to_record,
    make_evidence_record,
)
from backend.core.agents.context.object_store import ContextObjectStore
from backend.core.agents.context.policy import ContextPolicy
from backend.core.agents.context.projection import ProjectionCompiler
from backend.core.agents.context.result_sink import ResultSink
from backend.core.agents.context.segmenters import (
    SegmenterRegistry,
    create_default_segmenter_registry,
)
from backend.core.agents.context.state_adapter import ContextStateAdapter
from backend.core.agents.context.types import (
    CompiledContextRequest,
    ContextCategory,
    ContextPack,
    EvidenceRecord,
)

DEFAULT_PROJECT_EVIDENCE_TOOLS = frozenset(
    {
        "groundwater_project_skill_read",
        "inspect_project_tables",
        "query_project_tables",
        "summarize_project_table",
        "groundwater_prediction",
        "groundwater_quality_check",
    }
)

DEFAULT_SEMANTIC_SUMMARY_SKIP_TOOLS = frozenset(
    {
        "read_local_skill_file",
        "read_skill_reference",
        "list_available_skills",
        "groundwater_project_skill_read",
        "groundwater_project_bootstrap",
        "groundwater_quality_check",
        "multi_kb_search",
        "read_context_artifact",
        "context_read",
        "web_search",
        "task",
    }
)


@dataclass(frozen=True)
class ContextEngine:
    """Own the components that participate in one context lifecycle.

    Full agents, lightweight agents and writer stages should construct these
    components through this factory so model limits, raw windows, persistence
    and compaction cannot silently drift between execution paths.
    """

    policy: ContextPolicy
    artifact_store: ContextArtifactStore
    result_sink: ResultSink
    ledger: ContextLedger
    history_compactor: HistoryCompactor
    capture_registry: ContextCaptureRegistry
    state_adapter: ContextStateAdapter
    semantic_summary_skip_tools: frozenset[str]
    context_object_mode: str = "off"
    object_store: ContextObjectStore | None = None
    projection_compiler: ProjectionCompiler | None = None
    segmenter_registry: SegmenterRegistry | None = None

    @classmethod
    def create(
        cls,
        *,
        model_name: str | None,
        user_id: str | None = None,
        thread_id: str | None = None,
        policy: ContextPolicy | None = None,
        artifact_store: ContextArtifactStore | None = None,
        char_threshold: int | None = None,
        record_top_k: int | None = None,
        project_evidence_tools: set[str] | frozenset[str] | None = None,
    ) -> ContextEngine:
        resolved_policy = (
            policy or ContextPolicy.from_env(model_name)
        ).with_capture_overrides(
            char_threshold=char_threshold,
        )
        context_object_mode = resolved_policy.context_object_mode
        store = artifact_store or create_context_artifact_store()
        normalized_user_id = str(user_id or "").strip() or None
        normalized_thread_id = str(thread_id or "").strip() or None
        sink = ResultSink(
            store,
            persist_threshold=resolved_policy.large_result_threshold_chars,
            summary_max_chars=resolved_policy.context_item_summary_chars,
            persist_large_results=context_object_mode != "on",
            artifact_attributes=(
                {"owner_user_id": normalized_user_id}
                if normalized_user_id
                else None
            ),
        )
        resolved_project_evidence_tools = (
            DEFAULT_PROJECT_EVIDENCE_TOOLS
            if project_evidence_tools is None
            else project_evidence_tools
        )
        token_counter = TokenCounter(model_name or resolved_policy.model_name)
        object_store = (
            ContextObjectStore(
                store,
                object_ttl_seconds=resolved_policy.context_object_ttl_seconds,
            )
            if context_object_mode != "off"
            else None
        )
        segmenter_registry = (
            create_default_segmenter_registry()
            if context_object_mode != "off"
            else None
        )
        projection_compiler = (
            ProjectionCompiler(token_counter=token_counter)
            if context_object_mode != "off"
            else None
        )
        capture_registry = ContextCaptureRegistry(
            [
                SkillCaptureHandler(
                    sink,
                    inline_char_limit=resolved_policy.context_item_summary_chars,
                ),
                MultiKbCaptureHandler(sink),
                WebSearchCaptureHandler(sink),
                ProjectBootstrapCaptureHandler(),
                ProjectEvidenceCaptureHandler(
                    sink,
                    tool_names=set(resolved_project_evidence_tools),
                    char_threshold=resolved_policy.large_result_threshold_chars,
                ),
                ContextArtifactReadCaptureHandler(),
            ],
            fallback_handler=LargeResultCaptureHandler(
                sink,
                char_threshold=resolved_policy.large_result_threshold_chars,
            ),
        )
        return cls(
            policy=resolved_policy,
            artifact_store=store,
            result_sink=sink,
            ledger=ContextLedger(
                model_name=model_name or resolved_policy.model_name,
                budget=resolved_policy.ledger_budget,
                top_k=(
                    resolved_policy.context_record_top_k
                    if record_top_k is None
                    else max(1, int(record_top_k))
                ),
                dynamic_reserve_tokens=resolved_policy.dynamic_context_reserved_tokens,
                artifact_store=store,
                artifact_user_id=normalized_user_id,
                artifact_thread_id=normalized_thread_id,
                enforce_artifact_scope=True,
                token_counter=token_counter,
                raw_ref_char_limit=resolved_policy.raw_ref_injection_chars,
            ),
            history_compactor=HistoryCompactor(
                model_name=model_name or resolved_policy.model_name,
                trigger_tokens=resolved_policy.compaction_trigger_tokens,
                retained_window_tokens=(
                    resolved_policy.compaction_retained_window_tokens
                ),
                summary_input_chars=resolved_policy.compaction_summary_input_chars,
                summary_output_chars=resolved_policy.compaction_summary_output_chars,
                summary_max_chunks=resolved_policy.compaction_summary_max_chunks,
                timeout_seconds=resolved_policy.compaction_timeout_seconds,
                token_counter=token_counter,
            ),
            capture_registry=capture_registry,
            state_adapter=ContextStateAdapter(resolved_policy),
            semantic_summary_skip_tools=DEFAULT_SEMANTIC_SUMMARY_SKIP_TOOLS,
            context_object_mode=context_object_mode,
            object_store=object_store,
            projection_compiler=projection_compiler,
            segmenter_registry=segmenter_registry,
        )

    def compile_request(
        self,
        records: list[EvidenceRecord] | list[dict[str, Any]],
        *,
        messages: list,
        system_content: object | None = None,
        query: str | None = None,
        request_overhead: object | None = None,
    ) -> CompiledContextRequest:
        return self.ledger.compile_request(
            records,
            messages=messages,
            system_content=system_content,
            query=query,
            request_overhead=request_overhead,
        )

    def ingest_content(
        self,
        *,
        source_tool: str,
        content: object,
        thread_id: str,
        category: ContextCategory,
        score: float,
        kind: str,
        tags: list[str],
        source_id: str | None = None,
        turn_id: str | None = None,
        retrieval_seq: int | None = None,
    ) -> list[EvidenceRecord]:
        """Normalize directly obtained content through the shared sink."""

        text = str(content or "").strip()
        if not text:
            return []
        normalized_records = self.capture_registry.normalize_records(
            tool_name=source_tool,
            content=text,
            tool_args={},
            thread_id=thread_id,
        )
        if normalized_records:
            for record in normalized_records:
                if turn_id:
                    record["turn_id"] = turn_id
                if retrieval_seq is not None:
                    record["retrieval_seq"] = retrieval_seq
            return normalized_records
        item = self.result_sink.ingest(
            source_tool=source_tool,
            raw_content=text,
            thread_id=thread_id,
            turn_id=turn_id,
            importance_hint=score,
            tags=tags,
        )
        raw_ref = str(item.get("raw_ref") or "").strip() or None
        summary = str(item.get("summary") or text).strip()
        if not summary:
            return [context_item_to_record(item)]
        return [
            make_evidence_record(
                source_tool=source_tool,
                content=summary,
                category=category,
                source_id=source_id,
                kind=kind,
                raw_ref=raw_ref,
                score=score,
                tags=tags,
                turn_id=turn_id,
                retrieval_seq=retrieval_seq,
            )
        ]

    def records_from_state(self, state: dict[str, Any]) -> list[EvidenceRecord]:
        return self.state_adapter.records(state)

    @staticmethod
    def artifact_refs(pack: ContextPack | dict[str, Any] | None) -> list[str]:
        capabilities = dict((pack or {}).get("capabilities") or {})
        return [
            str(value)
            for value in capabilities.get("artifact_refs") or []
            if str(value).startswith("artifact://context/")
        ]

    @classmethod
    def requires_artifact_reader(
        cls,
        pack: ContextPack | dict[str, Any] | None,
    ) -> bool:
        return bool(cls.artifact_refs(pack))
