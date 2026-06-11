"""Resolve the knowledge bases an agent request is authorized to use."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.domain.agents.profile import AgentProfile


class KnowledgeBaseAccessError(PermissionError):
    """Raised when a request selects knowledge bases outside its authorization."""


@dataclass(frozen=True)
class KnowledgeBaseRecord:
    kb_id: str
    owner_id: str | None
    is_default: bool
    status: str


KnowledgeBaseLookup = Callable[[list[str]], dict[str, KnowledgeBaseRecord]]


class KnowledgeBaseAccessPolicy:
    """Apply profile binding and user ownership rules to requested KB IDs."""

    def __init__(self, lookup: KnowledgeBaseLookup | None = None):
        self._lookup = lookup or self._lookup_from_database

    def resolve_allowed_kb_ids(
        self,
        *,
        user_id: str,
        requested_ids: list[str],
        profile: AgentProfile,
    ) -> list[str]:
        policy = str(profile.kb_policy or "bound_or_selected").strip().lower()
        bound_ids = self._normalize(profile.default_kb_ids)
        requested = self._normalize(requested_ids)

        if policy in {"disabled", "none"}:
            if requested:
                raise KnowledgeBaseAccessError(
                    f"Agent {profile.code} does not allow knowledge-base selection"
                )
            return []

        if policy in {"bound_only", "profile_bound"}:
            denied = [kb_id for kb_id in requested if kb_id not in bound_ids]
            if denied:
                raise KnowledgeBaseAccessError(
                    f"Knowledge bases are not bound to agent {profile.code}: {', '.join(denied)}"
                )
            candidates = requested or bound_ids
            self._validate_records(
                candidates,
                user_id=user_id,
                bound_ids=set(bound_ids),
            )
            return candidates

        if policy not in {"bound_or_selected", "selected_owned", "user_selected"}:
            raise ValueError(f"Unknown knowledge-base policy: {profile.kb_policy}")

        candidates = requested or bound_ids
        if not candidates:
            return []

        self._validate_records(
            candidates,
            user_id=user_id,
            bound_ids=set(bound_ids),
        )
        return candidates

    def _validate_records(
        self,
        kb_ids: list[str],
        *,
        user_id: str,
        bound_ids: set[str],
    ) -> None:
        records = self._lookup(kb_ids)
        denied = [
            kb_id
            for kb_id in kb_ids
            if not self._is_accessible(
                records.get(kb_id),
                user_id=user_id,
                profile_bound=kb_id in bound_ids,
            )
        ]
        if denied:
            raise KnowledgeBaseAccessError(
                f"Knowledge-base access denied: {', '.join(denied)}"
            )

    @staticmethod
    def _normalize(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values or []:
            normalized = str(value or "").strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    @staticmethod
    def _is_accessible(
        record: KnowledgeBaseRecord | None,
        *,
        user_id: str,
        profile_bound: bool,
    ) -> bool:
        if record is None or record.status != "active":
            return False
        if profile_bound:
            return True
        return record.is_default or record.owner_id == user_id

    @staticmethod
    def _lookup_from_database(kb_ids: list[str]) -> dict[str, KnowledgeBaseRecord]:
        if not kb_ids:
            return {}

        from sqlalchemy import select

        from backend.db.connection import get_session
        from backend.db.models import KnowledgeBase

        session = get_session()
        try:
            rows = session.execute(
                select(
                    KnowledgeBase.kb_id,
                    KnowledgeBase.owner_id,
                    KnowledgeBase.is_default,
                    KnowledgeBase.status,
                ).where(KnowledgeBase.kb_id.in_(kb_ids))
            ).all()
            return {
                str(row.kb_id): KnowledgeBaseRecord(
                    kb_id=str(row.kb_id),
                    owner_id=str(row.owner_id) if row.owner_id else None,
                    is_default=bool(row.is_default),
                    status=str(getattr(row.status, "value", row.status) or ""),
                )
                for row in rows
            }
        finally:
            session.close()
