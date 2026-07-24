"""Structured context types for unified context governance."""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import NotRequired, TypedDict  # noqa: UP035 -- Python 3.10

ContextKind = Literal[
    "skill_rule",
    "retrieval_evidence",
    "analysis_result",
    "artifact_ref",
    "subagent_result",
    "generic",
]

ContextScope = Literal["turn", "thread"]
ContextCategory = Literal[
    "system",
    "history",
    "skills",
    "retrieval",
    "tool_result",
    "artifact",
    "handoff",
]


class ContextItem(TypedDict):
    """Structured context entry stored in thread state."""

    id: str
    kind: ContextKind
    source_tool: str
    scope: ContextScope
    summary: str
    keywords: list[str]
    importance: float
    raw_ref: NotRequired[str | None]
    created_at: float
    turn_id: NotRequired[str | None]
    skill: NotRequired[str | None]
    tags: list[str]
    prompt_content: NotRequired[str]


class WorkingMemory(TypedDict, total=False):
    """Per-turn executable state."""

    turn_id: str | None
    active_skills: list[str]
    tools_called: list[str]
    tool_budget_used: dict[str, int]
    interim_summary: str | None


class ContextBudgetState(TypedDict, total=False):
    """Tracks budget consumption for the current assembly cycle."""

    jit_char_budget: int
    jit_chars_used: int
    token_budget: int
    tokens_used: int
    base_tokens: int
    dynamic_token_budget: int
    dynamic_tokens_used: int
    reserved_output_tokens: int
    items_injected: int
    items_total: int
    jit_dropped_for_exact_budget: bool
    jit_trimmed_for_exact_budget: bool
    exact_budget_items_dropped: int
    last_assembled_at: float


class EvidenceRecord(TypedDict, total=False):
    """Structured evidence unit used by token-aware context assembly."""

    id: str
    category: ContextCategory
    source_id: str
    chunk_id: str
    doc_title: str
    score: float
    timestamp: float
    kind: str
    claim: str
    source_tool: str
    reliability: float
    raw_ref: str | None
    fallback_raw_ref: str | None
    materialized_ref: str | None
    tags: list[str]
    content: str
    owner_record_id: str
    turn_id: str | None
    retrieval_seq: int


class ClaimRecord(TypedDict, total=False):
    """Evidence-backed claim exchanged between coordinated agents."""

    claim: str
    supporting_refs: list[str]
    confidence: float
    conflicts: list[str]
    missing: list[str]
    source_ids: list[str]


class SourceCoverage(TypedDict, total=False):
    """Coverage metrics for a structured subagent handoff."""

    total_claims: int
    claims_with_refs: int
    unsupported_claims: int
    kb_refs: int
    web_refs: int
    project_data_refs: int
    raw_artifact_refs: int


class ContextCapabilities(TypedDict, total=False):
    """Capabilities derived from selected records, independent of rendering."""

    artifact_refs: list[str]
    materialized_refs: list[str]


class ContextPack(TypedDict, total=False):
    """Rendered single JIT context payload and its accounting details."""

    rendered: str
    selected_ids: list[str]
    budget: ContextBudgetState
    usage_by_category: dict[str, int]
    total_records: int
    capabilities: ContextCapabilities


class CompiledContextRequest(TypedDict, total=False):
    """Model-ready context envelope produced from a conservative token estimate."""

    messages: list
    system_content: str | None
    pack: ContextPack
    history_pruned: bool
    final_tokens: int
    estimated_tokens: int
    token_estimate_method: str


class SubagentResult(TypedDict, total=False):
    """Structured subagent return consumed by the main agent."""

    schema_version: str
    task_id: str
    summary: str
    evidence_refs: list[str]
    claims: list[ClaimRecord]
    confidence: float
    conflicts: list[str]
    missing: list[str]
    risks: list[str]
    suggested_next_actions: list[str]
    raw_artifact_ref: str | None
    raw_artifact_refs: list[str]
    source_coverage: SourceCoverage
    validation_warnings: list[str]
    subagent_type: str
    description: str


_NonEmptyHandoffString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
_Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
_CoverageCount = Annotated[int, Field(ge=0)]


class ClaimRecordV2Model(BaseModel):
    """Strict runtime representation of one evidence-backed claim."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    claim: _NonEmptyHandoffString
    supporting_refs: list[_NonEmptyHandoffString] = Field(max_length=8)
    confidence: _Confidence
    conflicts: list[_NonEmptyHandoffString] = Field(max_length=10)
    missing: list[_NonEmptyHandoffString] = Field(max_length=10)
    source_ids: list[_NonEmptyHandoffString] = Field(max_length=10)


class SourceCoverageV2Model(BaseModel):
    """Strict runtime representation of handoff source-coverage counters."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    total_claims: _CoverageCount
    claims_with_refs: _CoverageCount
    unsupported_claims: _CoverageCount
    kb_refs: _CoverageCount
    web_refs: _CoverageCount
    project_data_refs: _CoverageCount
    raw_artifact_refs: _CoverageCount


class SubagentResultV2Model(BaseModel):
    """Strict, complete runtime contract for a normalized V2 handoff.

    Compatibility builders continue to return :class:`SubagentResult` dictionaries.
    This model is an opt-in boundary validator for callers that need a complete,
    non-coercing contract before persisting or consuming a handoff.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["2.0"]
    task_id: _NonEmptyHandoffString
    summary: _NonEmptyHandoffString
    evidence_refs: list[_NonEmptyHandoffString] = Field(max_length=20)
    # A concise operational result may contain no extractable factual claim.
    # Keep that payload structurally valid and surface the condition through
    # ``validation_warnings`` instead of changing legacy handoff output.
    claims: list[ClaimRecordV2Model] = Field(max_length=12)
    confidence: _Confidence
    conflicts: list[_NonEmptyHandoffString] = Field(max_length=10)
    missing: list[_NonEmptyHandoffString] = Field(max_length=10)
    risks: list[_NonEmptyHandoffString] = Field(max_length=10)
    suggested_next_actions: list[_NonEmptyHandoffString] = Field(max_length=10)
    raw_artifact_ref: _NonEmptyHandoffString | None = None
    raw_artifact_refs: list[_NonEmptyHandoffString] = Field(max_length=20)
    source_coverage: SourceCoverageV2Model
    validation_warnings: list[_NonEmptyHandoffString]
    subagent_type: _NonEmptyHandoffString
    description: _NonEmptyHandoffString

    @model_validator(mode="after")
    def validate_source_coverage(self) -> SubagentResultV2Model:
        claims_with_refs = sum(1 for claim in self.claims if claim.supporting_refs)
        refs = list(dict.fromkeys([*self.evidence_refs, *self.raw_artifact_refs]))
        expected = {
            "total_claims": len(self.claims),
            "claims_with_refs": claims_with_refs,
            "unsupported_claims": len(self.claims) - claims_with_refs,
            "kb_refs": sum(
                1
                for ref in refs
                if ref.startswith("[")
                and not ref.startswith("[project-db:")
                and "#" in ref
            ),
            "web_refs": sum(
                1 for ref in refs if ref.startswith(("http://", "https://"))
            ),
            "project_data_refs": sum(
                1 for ref in refs if ref.startswith("[project-db:")
            ),
            "raw_artifact_refs": sum(
                1 for ref in refs if ref.startswith("artifact://context/")
            ),
        }
        mismatches = [
            f"source_coverage.{field} must be {value}, got {getattr(self.source_coverage, field)}"
            for field, value in expected.items()
            if getattr(self.source_coverage, field) != value
        ]
        if mismatches:
            raise ValueError("; ".join(mismatches))
        return self


def new_context_item_id() -> str:
    return f"ctx_{uuid.uuid4().hex[:12]}"


def new_turn_id() -> str:
    return f"t_{uuid.uuid4().hex[:8]}"


def make_context_item(
    *,
    kind: ContextKind,
    source_tool: str,
    summary: str,
    keywords: list[str] | None = None,
    importance: float = 0.5,
    raw_ref: str | None = None,
    scope: ContextScope = "thread",
    turn_id: str | None = None,
    skill: str | None = None,
    tags: list[str] | None = None,
) -> ContextItem:
    return ContextItem(
        id=new_context_item_id(),
        kind=kind,
        source_tool=source_tool,
        scope=scope,
        summary=summary,
        keywords=keywords or [],
        importance=importance,
        raw_ref=raw_ref,
        created_at=time.time(),
        turn_id=turn_id,
        skill=skill,
        tags=tags or [],
    )
