"""Normalize durable graph state into the canonical context-record model."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from backend.core.agents.context.ledger import (
    context_item_to_record,
    make_evidence_record,
    parse_multikb_text_evidence,
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
