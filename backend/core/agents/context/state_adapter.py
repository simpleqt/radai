"""Normalize durable graph state into the canonical context-record model."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, cast

from backend.core.agents.context.ledger import (
    context_item_to_record,
    make_evidence_record,
    parse_multikb_text_evidence,
)
from backend.core.agents.context.objects import (
    ContextObject,
    make_context_object,
    make_segment,
    split_text_blocks,
)
from backend.core.agents.context.policy import ContextPolicy
from backend.core.agents.context.types import EvidenceRecord


class ContextStateAdapter:
    """One migration boundary for canonical and legacy context state."""

    def __init__(self, policy: ContextPolicy):
        self._policy = policy

    def records(self, state: Mapping[str, Any]) -> list[EvidenceRecord]:
        records: list[EvidenceRecord] = [
            cast(EvidenceRecord, dict(record))
            for record in state.get("context_records") or []
        ]
        loaded_skills = state.get("loaded_skills") or {}
        externalized_skill_names = {
            str(tag).strip()
            for record in records
            if str(record.get("source_tool") or "") == "read_local_skill_file"
            and str(record.get("kind") or "") == "skill_rule"
            for tag in list(record.get("tags") or [])
            if str(tag).strip() not in {"skill_rule", "tool_result"}
        }

        project_skill = str(state.get("project_skill") or "").strip()
        if project_skill:
            records.append(
                make_evidence_record(
                    source_tool="groundwater_project_skill_read",
                    content=project_skill,
                    category="skills",
                    source_id="current-groundwater-project-skill",
                    doc_title="当前地下水项目 Skill",
                    score=1.0,
                    kind="project_skill_rule",
                    tags=["skill_rule", "groundwater", "current_project"],
                )
            )

        project_bootstrap = state.get("project_bootstrap") or {}
        if isinstance(project_bootstrap, dict) and project_bootstrap.get("project_id"):
            contract = self._render_project_bootstrap_contract(project_bootstrap)
            contract_record = make_evidence_record(
                source_tool="groundwater_project_bootstrap",
                content=contract,
                category="system",
                source_id=(
                    f"{project_bootstrap.get('project_id')}@"
                    f"{project_bootstrap.get('data_version')}"
                ),
                doc_title="当前地下水项目 SQL 合同",
                score=1.0,
                kind="project_sql_contract",
                tags=["groundwater", "project_schema", "sql_contract"],
            )
            contract_record["claim"] = contract
            records.append(contract_record)

        if isinstance(loaded_skills, Mapping):
            for name, content in loaded_skills.items():
                if str(name).strip() in externalized_skill_names:
                    continue
                records.append(
                    make_evidence_record(
                        source_tool="read_local_skill_file",
                        content=content,
                        category="skills",
                        source_id=str(name),
                        doc_title=str(name),
                        kind="skill_rule",
                        tags=["skill_rule", str(name)],
                    )
                )

        for content in self._bounded_recent(
            state.get("project_evidence") or [],
            max_blocks=self._policy.project_fallback_blocks,
            max_chars=self._policy.project_fallback_chars,
        ):
            records.append(
                make_evidence_record(
                    source_tool="project_evidence",
                    content=content,
                    category="tool_result",
                    kind="project_data",
                    tags=["project_data"],
                )
            )
        for content in self._bounded_recent(
            state.get("retrieved_evidence") or [],
            max_blocks=self._policy.retrieval_fallback_blocks,
            max_chars=self._policy.retrieval_fallback_chars,
        ):
            records.extend(parse_multikb_text_evidence(content))
        for item in state.get("context_items") or []:
            records.append(context_item_to_record(item))
        return records

    def context_objects(self, state: Mapping[str, Any]) -> list[ContextObject]:
        """Promote every legacy state shape into synthetic context objects."""

        objects: list[ContextObject] = []
        origin_base = {
            "thread_id": str(
                (state.get("thread_data") or {}).get("thread_id")
                if isinstance(state.get("thread_data"), Mapping)
                else ""
            ),
            "created_at": time.time(),
            "last_seen_at": time.time(),
        }

        def origin(tool: str) -> dict[str, Any]:
            return {**origin_base, "tool": tool}

        for index, record in enumerate(state.get("context_records") or []):
            if not isinstance(record, Mapping):
                continue
            if str(record.get("object_ref") or "").strip():
                continue
            content = str(record.get("content") or record.get("claim") or "")
            segment = make_segment(
                sid=str(record.get("id") or f"legacy:record:{index}"),
                text=content,
                title=str(record.get("doc_title") or record.get("source_id") or "旧上下文记录"),
                score=float(record.get("score") or 0.5),
                tags=list(record.get("tags") or []),
            )
            objects.append(
                make_context_object(
                    schema="legacy/record/v1",
                    origin=origin(str(record.get("source_tool") or "legacy")),
                    segments=[segment],
                )
            )

        for index, item in enumerate(state.get("context_items") or []):
            if not isinstance(item, Mapping):
                continue
            content = str(item.get("summary") or "")
            if not content.strip():
                continue
            objects.append(
                make_context_object(
                    schema="legacy/item/v1",
                    origin=origin(str(item.get("source_tool") or "legacy")),
                    segments=[
                        make_segment(
                            sid=str(item.get("id") or f"legacy:item:{index}"),
                            text=content,
                            title=str(item.get("source_tool") or "旧上下文项"),
                            score=float(item.get("importance") or 0.5),
                            tags=list(item.get("tags") or []),
                        )
                    ],
                )
            )

        for block_index, content in enumerate(
            self._bounded_recent(
                state.get("retrieved_evidence") or [],
                max_blocks=self._policy.retrieval_fallback_blocks,
                max_chars=self._policy.retrieval_fallback_chars,
            )
        ):
            parsed = parse_multikb_text_evidence(content)
            segments = [
                make_segment(
                    sid=str(record.get("id") or f"legacy:evidence:{block_index}:{idx}"),
                    text=str(record.get("content") or ""),
                    title=str(record.get("doc_title") or "旧检索证据"),
                    score=float(record.get("score") or 0.5),
                )
                for idx, record in enumerate(parsed)
            ]
            if not segments:
                segments = split_text_blocks(content, prefix="legacy")
            objects.append(
                make_context_object(
                    schema="legacy/retrieval/v1",
                    origin=origin("multi_kb_search"),
                    segments=segments,
                )
            )

        for _block_index, content in enumerate(
            self._bounded_recent(
                state.get("project_evidence") or [],
                max_blocks=self._policy.project_fallback_blocks,
                max_chars=self._policy.project_fallback_chars,
            )
        ):
            objects.append(
                make_context_object(
                    schema="legacy/project/v1",
                    origin=origin("project_evidence"),
                    segments=split_text_blocks(content, prefix="legacy_project"),
                )
            )

        loaded_skills = state.get("loaded_skills") or {}
        if isinstance(loaded_skills, Mapping):
            for name, content in loaded_skills.items():
                objects.append(
                    make_context_object(
                        schema="skill/v1",
                        origin=origin("read_local_skill_file"),
                        segments=[
                            make_segment(
                                sid=f"skill:{name}",
                                text=str(content or ""),
                                title=str(name),
                                score=1.0,
                                tags=["skill_rule"],
                            )
                        ],
                    )
                )
        project_skill = str(state.get("project_skill") or "").strip()
        if project_skill:
            objects.append(
                make_context_object(
                    schema="skill/v1",
                    origin=origin("groundwater_project_skill_read"),
                    segments=[
                        make_segment(
                            sid="project_skill:0",
                            text=project_skill,
                            title="当前地下水项目 Skill",
                            score=1.0,
                            tags=["skill_rule", "groundwater"],
                        )
                    ],
                )
            )
        project_bootstrap = state.get("project_bootstrap") or {}
        if isinstance(project_bootstrap, Mapping) and project_bootstrap.get("project_id"):
            objects.append(
                make_context_object(
                    schema="contract/v1",
                    origin=origin("groundwater_project_bootstrap"),
                    segments=[
                        make_segment(
                            sid="project_contract:0",
                            text=self._render_project_bootstrap_contract(
                                dict(project_bootstrap)
                            ),
                            title="当前地下水项目 SQL 合同",
                            score=1.0,
                            tags=["groundwater", "sql_contract"],
                        )
                    ],
                )
            )
        return objects

    @staticmethod
    def _bounded_recent(
        values: list[str],
        *,
        max_blocks: int,
        max_chars: int,
    ) -> list[str]:
        """Keep a newest-first-compatible suffix within one character cap."""

        candidates = list(values[-max(1, int(max_blocks)) :])
        kept: list[str] = []
        used = 0
        for value in reversed(candidates):
            text = str(value or "")
            remaining = max(0, int(max_chars) - used)
            if remaining <= 0:
                break
            if len(text) <= remaining:
                kept.insert(0, text)
                used += len(text)
                continue
            if not kept:
                kept.insert(0, text[:remaining])
            break
        return kept

    @staticmethod
    def _render_project_bootstrap_contract(payload: dict[str, Any]) -> str:
        table_parts = []
        for name, descriptor in sorted((payload.get("tables") or {}).items()):
            if not isinstance(descriptor, dict):
                continue
            columns = ",".join(str(item) for item in descriptor.get("columns") or [])
            table_parts.append(
                f"{name}[rows={descriptor.get('row_count', 0)}; columns={columns}]"
            )
        routes = " | ".join(
            f"{topic}=>{target}"
            for topic, target in (payload.get("table_routes") or {}).items()
        )
        return "\n".join(
            [
                "GROUNDWATER_PROJECT_SQL_CONTRACT "
                "(authoritative; never invent tables/columns)",
                (
                    f"project_id={payload.get('project_id')}; "
                    f"data_version={payload.get('data_version')}; "
                    f"engine={payload.get('engine')}"
                ),
                "tables=" + " | ".join(table_parts),
                "routes=" + routes,
                "rule=Use only the listed tables and columns. "
                "Stop after evidence is sufficient.",
            ]
        )
