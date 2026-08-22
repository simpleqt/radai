"""Tool-result capture handlers used by context governance middleware."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.core.agents.context.ledger import (
    context_item_to_record,
    make_evidence_record,
    parse_multikb_text_evidence,
)
from backend.core.agents.context.objects import ContextObject, ProjectionReceipt
from backend.core.agents.context.result_sink import ResultSink
from backend.core.agents.context.summary import generate_summary
from backend.core.agents.context.types import EvidenceRecord
from backend.core.skills.native_skill_tools import normalize_skill_tool_name


@dataclass
class CapturedContext:
    replacement: str
    loaded_skills: dict[str, str] = field(default_factory=dict)
    context_records: list[EvidenceRecord] = field(default_factory=list)
    project_bootstrap: dict[str, Any] | None = None
    context_objects: list[ContextObject] = field(default_factory=list)
    projection_receipts: list[ProjectionReceipt] = field(default_factory=list)


class ContextCaptureHandler(Protocol):
    def can_handle(self, tool_name: str, content: str) -> bool: ...

    def capture(
        self,
        *,
        tool_name: str,
        content: str,
        tool_args: dict[str, Any],
        thread_id: str,
        artifact: Any | None = None,
    ) -> CapturedContext: ...


class SkillCaptureHandler:
    def __init__(
        self,
        result_sink: ResultSink | None = None,
        *,
        inline_char_limit: int = 6000,
    ) -> None:
        self._result_sink = result_sink
        self._inline_char_limit = max(1000, int(inline_char_limit))

    def can_handle(self, tool_name: str, content: str) -> bool:
        return tool_name == "read_local_skill_file" and bool(content)

    def capture(
        self,
        *,
        tool_name: str,
        content: str,
        tool_args: dict[str, Any],
        thread_id: str,
        artifact: Any | None = None,
    ) -> CapturedContext:
        del tool_name, artifact
        skill_name = normalize_skill_tool_name(
            tool_args.get("skill_name") or tool_args.get("skill") or "unknown_skill"
        )
        loaded_content = content
        context_records: list[EvidenceRecord] = []
        if self._result_sink is not None and len(content) > self._inline_char_limit:
            item = self._result_sink.ingest(
                source_tool="read_local_skill_file",
                raw_content=content,
                thread_id=thread_id,
                importance_hint=1.0,
                tags=["skill_rule", skill_name],
                kind_override="skill_rule",
            )
            loaded_content = str(
                item.get("summary") or generate_summary(
                    content,
                    max_chars=self._inline_char_limit,
                )
            )
            context_records.append(context_item_to_record(item))
        return CapturedContext(
            replacement=(
                f"[State Elevation: Loaded skill content for '{skill_name}' successfully "
                "elevated to Graph State and enforced dynamically in the System Prompt.]"
            ),
            loaded_skills={skill_name: loaded_content},
            context_records=context_records,
        )


class MultiKbCaptureHandler:
    def __init__(self, result_sink: ResultSink):
        self._result_sink = result_sink

    def can_handle(self, tool_name: str, content: str) -> bool:
        return tool_name == "multi_kb_search" and bool(content)

    def capture(
        self,
        *,
        tool_name: str,
        content: str,
        tool_args: dict[str, Any],
        thread_id: str,
        artifact: Any | None = None,
    ) -> CapturedContext:
        del tool_args
        item = self._result_sink.ingest(
            source_tool=tool_name,
            raw_content=content,
            thread_id=thread_id,
            importance_hint=0.95,
            tags=["evidence", "knowledge_base"],
        )
        structured_records = (
            list(artifact.get("evidence_records") or [])
            if isinstance(artifact, dict)
            else []
        )
        raw_ref = str(item.get("raw_ref") or "").strip() or None
        if structured_records and raw_ref:
            # The provider's records describe source chunks and normally do
            # not know about the context artifact ResultSink has just saved.
            # Fill that reference here so an ``ev_*`` evidence id is never
            # mistaken for an artifact URI. Preserve any provider-owned ref.
            structured_records = [
                {
                    **record,
                    **(
                        {}
                        if str(record.get("raw_ref") or "").strip()
                        else {"raw_ref": raw_ref}
                    ),
                }
                for record in structured_records
                if isinstance(record, dict)
            ]
        return CapturedContext(
            replacement=(
                "[Context Isolation: Retrieved relevant knowledge from database successfully "
                "isolated to structured context records to optimize context window.]"
            ),
            context_records=(
                structured_records
                or parse_multikb_text_evidence(
                    content,
                    raw_ref=raw_ref,
                )
            ),
        )


class WebSearchCaptureHandler:
    """Preserve page-level web evidence without an extra LLM summary pass."""

    def __init__(
        self,
        result_sink: ResultSink,
        *,
        page_content_chars: int = 1800,
    ):
        self._result_sink = result_sink
        self._page_content_chars = max(400, int(page_content_chars))

    def can_handle(self, tool_name: str, content: str) -> bool:
        return tool_name == "web_search" and bool(content)

    def capture(
        self,
        *,
        tool_name: str,
        content: str,
        tool_args: dict[str, Any],
        thread_id: str,
        artifact: Any | None = None,
    ) -> CapturedContext:
        del artifact
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return CapturedContext(replacement=content)
        if not isinstance(payload, dict) or payload.get("error"):
            return CapturedContext(replacement=content)

        results = payload.get("results")
        if not isinstance(results, list):
            return CapturedContext(replacement=content)

        pages = [
            item
            for item in results
            if isinstance(item, dict)
            and str(item.get("url") or item.get("href") or item.get("link") or "").strip()
        ]
        if not pages:
            return CapturedContext(replacement=content)

        item = self._result_sink.ingest(
            source_tool=tool_name,
            raw_content=content,
            thread_id=thread_id,
            importance_hint=0.9,
            tags=["evidence", "web"],
        )
        raw_ref = item.get("raw_ref")
        query = str(payload.get("query") or tool_args.get("query") or "").strip()
        records: list[EvidenceRecord] = []
        source_lines: list[str] = []
        for index, page in enumerate(pages, start=1):
            url = str(
                page.get("url") or page.get("href") or page.get("link") or ""
            ).strip()
            title = str(page.get("title") or url).strip()
            provider = str(
                page.get("provider") or payload.get("engine") or "web"
            ).strip()
            page_text = str(
                page.get("content")
                or page.get("markdown")
                or page.get("description")
                or ""
            ).strip()
            evidence = "\n".join(
                part
                for part in (
                    f"标题：{title}",
                    f"页面：{url}",
                    f"来源：{provider}",
                    (
                        "页面内容："
                        + generate_summary(page_text, max_chars=self._page_content_chars)
                        if page_text
                        else ""
                    ),
                )
                if part
            )
            records.append(
                make_evidence_record(
                    source_tool=tool_name,
                    content=evidence,
                    category="retrieval",
                    source_id=url,
                    chunk_id=f"web-result-{index}",
                    doc_title=title,
                    score=max(0.55, 1.0 - index * 0.08),
                    kind="web",
                    # The first record points to the complete persisted payload.
                    # Repeating the same excerpt for every page wastes JIT budget.
                    raw_ref=raw_ref if index == 1 else None,
                    tags=["evidence", "web", provider],
                )
            )
            source_lines.append(f"- {title} — {url}")

        query_label = f"“{query}”" if query else "本次查询"
        retrieval_budget = payload.get("retrieval_budget")
        budget_notice = ""
        if (
            isinstance(retrieval_budget, dict)
            and retrieval_budget.get("exhausted") is True
        ):
            budget_notice = (
                " Retrieval budget is exhausted: do not call any retrieval tool again; "
                "synthesize the final branch conclusion from the evidence already collected."
            )
        return CapturedContext(
            replacement=(
                f"[Context Isolation: Web search {query_label} returned {len(records)} pages; "
                "page evidence was elevated to structured context records. Sources:\n"
                + "\n".join(source_lines)
                + budget_notice
                + "]"
            ),
            context_records=records,
        )


class ProjectEvidenceCaptureHandler:
    def __init__(self, result_sink: ResultSink, *, tool_names: set[str], char_threshold: int):
        self._result_sink = result_sink
        self._tool_names = frozenset(tool_names)
        self._char_threshold = char_threshold

    def can_handle(self, tool_name: str, content: str) -> bool:
        return tool_name in self._tool_names and len(content) > self._char_threshold

    def capture(
        self,
        *,
        tool_name: str,
        content: str,
        tool_args: dict[str, Any],
        thread_id: str,
        artifact: Any | None = None,
    ) -> CapturedContext:
        del tool_args, artifact
        compact_content = self._compact_groundwater_quality_check(tool_name, content)
        wrapped = f'<project_tool_result name="{tool_name}">\n{content}\n</project_tool_result>'
        item = self._result_sink.ingest(
            source_tool=tool_name,
            raw_content=content,
            thread_id=thread_id,
            importance_hint=0.95,
            tags=["evidence", "project_data"],
        )
        record = make_evidence_record(
            source_tool=tool_name,
            content=compact_content or item.get("summary") or wrapped,
            category="tool_result",
            kind="project_data",
            raw_ref=item.get("raw_ref"),
            tags=["evidence", "project_data"],
        )
        if compact_content:
            record["claim"] = compact_content
            record["content"] = compact_content
        return CapturedContext(
            replacement=compact_content
            or (
                f"[Context Isolation: Groundwater project data from tool '{tool_name}' "
                "successfully isolated to structured context records.]"
            ),
            context_records=[record],
        )

    @staticmethod
    def _compact_groundwater_quality_check(tool_name: str, content: str) -> str | None:
        if tool_name != "groundwater_quality_check":
            return None
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("error"):
            return None
        point_code = payload.get("point_code")
        exceedances = payload.get("exceedances")
        if not point_code or not isinstance(exceedances, list):
            return None
        completeness = payload.get("layer_completeness") or {}
        lines = [
            "GROUNDWATER_QUALITY_CHECK_RESULT",
            (
                f"project_id={payload.get('project_id')}; "
                f"data_version={payload.get('data_version')}; "
                f"point_code={point_code}; standard_class={payload.get('standard_class')}; "
                f"standard_reference={payload.get('standard_reference')}"
            ),
            (
                "layer_completeness="
                f"present={completeness.get('present_layers')}; "
                f"missing={completeness.get('missing_layers')}; "
                f"measurement_count={payload.get('measurement_count')}; "
                f"checked_parameter_count={payload.get('checked_parameter_count')}; "
                f"exceedance_count={payload.get('exceedance_count')}"
            ),
            "exceedances: sample_layer | parameter | measured | limit | exceed_amount | ratio_to_limit",
        ]
        for row in exceedances:
            if not isinstance(row, dict):
                continue
            measured = row.get("value_for_comparison", row.get("measured_value"))
            unit = row.get("comparison_unit") or row.get("measured_unit") or row.get("standard_unit")
            lines.append(
                " - "
                f"{row.get('sample_layer')} | "
                f"{row.get('parameter_name') or row.get('parameter_code')} | "
                f"{measured} {unit} | "
                f"{row.get('standard_limit')} {row.get('standard_unit') or unit} | "
                f"{row.get('exceed_amount')} {row.get('exceed_amount_unit') or unit} | "
                f"{row.get('ratio_to_limit')}"
            )
        if payload.get("parameters_without_configured_iv_limit"):
            lines.append(
                "unchecked_parameters="
                + json.dumps(
                    payload.get("parameters_without_configured_iv_limit"),
                    ensure_ascii=False,
                    default=str,
                )
            )
        lines.append(
            "instruction=Evidence is sufficient for a direct answer; do not re-query full GW18 water quality rows unless the user asks for raw records."
        )
        return "\n".join(lines)


class ProjectBootstrapCaptureHandler:
    """Keep the compact schema contract outside pruneable tool-message history."""

    def can_handle(self, tool_name: str, content: str) -> bool:
        return tool_name == "groundwater_project_bootstrap" and bool(content)

    def capture(
        self,
        *,
        tool_name: str,
        content: str,
        tool_args: dict[str, Any],
        thread_id: str,
        artifact: Any | None = None,
    ) -> CapturedContext:
        del tool_name, tool_args, thread_id, artifact
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict) or payload.get("error"):
            return CapturedContext(replacement=content)
        return CapturedContext(
            replacement=(
                "[State Elevation: Current groundwater project SQL contract was loaded and "
                "will remain available during context compression.]"
            ),
            project_bootstrap=payload,
        )


class ContextArtifactReadCaptureHandler:
    """Expose artifact content once without persisting the reader output again.

    ``read_context_artifact`` already reads from the external artifact store. If
    its JSON wrapper is sent through ``LargeResultCaptureHandler`` it becomes a
    second artifact whose content contains the first reference. The model can
    then keep opening wrapper artifacts until it exhausts its turn budget.
    """

    def can_handle(self, tool_name: str, content: str) -> bool:
        return tool_name in {"read_context_artifact", "context_read"} and bool(content)

    def capture(
        self,
        *,
        tool_name: str,
        content: str,
        tool_args: dict[str, Any],
        thread_id: str,
        artifact: Any | None = None,
    ) -> CapturedContext:
        del tool_name, tool_args, thread_id, artifact
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("error"):
            error = str(payload.get("error") or "artifact_read_failed")
            return CapturedContext(
                replacement=(
                    "[System Notification: Context artifact read failed with "
                    f"'{error}'. Do not retry or derive another artifact reference "
                    "from this response; continue with evidence already available.]"
                )
            )
        artifact_content = (
            str(payload.get("content") or "")
            if isinstance(payload, dict)
            else content
        )
        return CapturedContext(
            replacement=(
                "[Context Artifact Read: The requested artifact content is included "
                "below exactly once. Use it directly and do not reopen references "
                "embedded in an artifact wrapper.]\n"
                + artifact_content
            )
        )


class LargeResultCaptureHandler:
    def __init__(self, result_sink: ResultSink, *, char_threshold: int):
        self._result_sink = result_sink
        self._char_threshold = char_threshold

    def can_handle(self, tool_name: str, content: str) -> bool:
        del tool_name
        return len(content) > self._char_threshold

    def capture(
        self,
        *,
        tool_name: str,
        content: str,
        tool_args: dict[str, Any],
        thread_id: str,
        artifact: Any | None = None,
    ) -> CapturedContext:
        del tool_args, artifact
        source_tool = tool_name or "unknown"
        item = self._result_sink.ingest(
            source_tool=source_tool,
            raw_content=content,
            thread_id=thread_id,
            importance_hint=0.8,
        )
        return CapturedContext(
            replacement=(
                f"[Result Capture: Tool '{source_tool}' returned {len(content)} characters. "
                f"The result is available to the model through dynamic context item '{item['id']}'.]"
            ),
            context_records=[context_item_to_record(item)],
        )


class ContextCaptureRegistry:
    def __init__(
        self,
        handlers: list[ContextCaptureHandler],
        *,
        fallback_handler: ContextCaptureHandler | None = None,
    ):
        self._handlers = list(handlers)
        self._fallback_handler = fallback_handler

    def _capture_source(
        self,
        *,
        tool_name: str,
        content: str,
        tool_args: dict[str, Any],
        thread_id: str,
        artifact: Any | None = None,
    ) -> CapturedContext | None:
        """Run source adapters without invoking the generic large-result fallback."""

        for handler in self._handlers:
            if handler.can_handle(tool_name, content):
                return handler.capture(
                    tool_name=tool_name,
                    content=content,
                    tool_args=tool_args,
                    thread_id=thread_id,
                    artifact=artifact,
                )
        return None

    def normalize_records(
        self,
        *,
        tool_name: str,
        content: str,
        tool_args: dict[str, Any],
        thread_id: str,
        artifact: Any | None = None,
    ) -> list[EvidenceRecord]:
        """Normalize direct content through the same source adapters as Graph tools."""

        captured = self._capture_source(
            tool_name=tool_name,
            content=content,
            tool_args=tool_args,
            thread_id=thread_id,
            artifact=artifact,
        )
        return list(captured.context_records) if captured is not None else []

    def capture(
        self,
        *,
        tool_name: str,
        content: str,
        tool_args: dict[str, Any],
        thread_id: str,
        artifact: Any | None = None,
    ) -> CapturedContext | None:
        captured = self._capture_source(
            tool_name=tool_name,
            content=content,
            tool_args=tool_args,
            thread_id=thread_id,
            artifact=artifact,
        )
        if captured is not None or self._fallback_handler is None:
            return captured
        if not self._fallback_handler.can_handle(tool_name, content):
            return None
        return self._fallback_handler.capture(
            tool_name=tool_name,
            content=content,
            tool_args=tool_args,
            thread_id=thread_id,
            artifact=artifact,
        )
