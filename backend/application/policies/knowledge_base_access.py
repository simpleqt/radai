"""Resolve the knowledge bases an agent request is authorized to use.

Agent bindings are configuration, not authorization.  A binding may suggest a
knowledge base for an agent, but the current principal must still be allowed to
read that knowledge base.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.core.utils.normalization import normalize_unique_strings
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
AgentBindingLookup = Callable[[str], list[str]]
DefaultKnowledgeBaseLookup = Callable[[], list[str]]
KnowledgeBaseGrantLookup = Callable[[str, list[str]], set[str]]

SUPPORTED_KNOWLEDGE_BASE_POLICIES = frozenset(
    {
        "disabled",
        "none",
        "bound_only",
        "profile_bound",
        "bound_or_selected",
        "selected_owned",
        "user_selected",
    }
)


def is_guest_principal(user_id: str | None) -> bool:
    """Return whether ``user_id`` represents an unauthenticated guest scope."""
    normalized = str(user_id or "").strip()
    return not normalized or normalized == "guest" or normalized.startswith("guest_")


def can_read_knowledge_base(
    record: KnowledgeBaseRecord | None,
    *,
    user_id: str,
    explicitly_granted: bool = False,
    is_admin: bool = False,
) -> bool:
    """Apply the shared KB read rule without treating an agent binding as ACL."""
    if record is None or str(record.status or "").lower() != "active":
        return False
    if is_admin:
        return True

    # Default KBs and ownerless platform KBs are the two currently explicit
    # public forms in the schema.  Merely being active or agent-bound is not
    # enough to make an owner-private KB public.
    if record.is_default or not record.owner_id:
        return True
    if is_guest_principal(user_id):
        return False
    return record.owner_id == user_id or explicitly_granted


class KnowledgeBaseAccessPolicy:
    """Apply profile binding and user ownership rules to requested KB IDs."""

    @staticmethod
    def supports_policy(policy: str | None) -> bool:
        return (
            str(policy or "bound_or_selected").strip().lower()
            in SUPPORTED_KNOWLEDGE_BASE_POLICIES
        )

    @staticmethod
    def supported_policies() -> frozenset[str]:
        return SUPPORTED_KNOWLEDGE_BASE_POLICIES

    def __init__(
        self,
        lookup: KnowledgeBaseLookup | None = None,
        binding_lookup: AgentBindingLookup | None = None,
        default_lookup: DefaultKnowledgeBaseLookup | None = None,
        grant_lookup: KnowledgeBaseGrantLookup | None = None,
    ):
        self._lookup = lookup or self._lookup_from_database
        self._binding_lookup = binding_lookup or self._lookup_bindings_from_database
        self._default_lookup = default_lookup or (
            self._lookup_defaults_from_database
            if lookup is None and binding_lookup is None
            else lambda: []
        )
        self._grant_lookup = grant_lookup or (
            self._lookup_grants_from_database
            if lookup is None
            else lambda _user_id, _kb_ids: set()
        )

    def resolve_allowed_kb_ids(
        self,
        *,
        user_id: str,
        requested_ids: list[str],
        profile: AgentProfile,
    ) -> list[str]:
        policy = str(profile.kb_policy or "bound_or_selected").strip().lower()
        bound_ids = self._normalize(self._binding_lookup(profile.code))
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
            if requested:
                self._validate_records(requested, user_id=user_id)
                return requested
            return self._filter_accessible_records(bound_ids, user_id=user_id)

        if policy not in {"bound_or_selected", "selected_owned", "user_selected"}:
            raise ValueError(f"Unknown knowledge-base policy: {profile.kb_policy}")

        candidates = requested or bound_ids or self._normalize(self._default_lookup())
        if not candidates:
            return []

        if requested:
            self._validate_records(candidates, user_id=user_id)
            return candidates

        # Bindings/defaults are implicit suggestions.  Inaccessible entries are
        # removed so one private binding cannot break an otherwise public agent.
        return self._filter_accessible_records(candidates, user_id=user_id)

    def _validate_records(
        self,
        kb_ids: list[str],
        *,
        user_id: str,
    ) -> None:
        records = self._lookup(kb_ids)
        granted_ids = self._granted_ids(user_id, kb_ids)
        denied = [
            kb_id
            for kb_id in kb_ids
            if not can_read_knowledge_base(
                records.get(kb_id),
                user_id=user_id,
                explicitly_granted=kb_id in granted_ids,
            )
        ]
        if denied:
            raise KnowledgeBaseAccessError(
                f"Knowledge-base access denied: {', '.join(denied)}"
            )

    def _filter_accessible_records(
        self,
        kb_ids: list[str],
        *,
        user_id: str,
    ) -> list[str]:
        records = self._lookup(kb_ids)
        granted_ids = self._granted_ids(user_id, kb_ids)
        return [
            kb_id
            for kb_id in kb_ids
            if can_read_knowledge_base(
                records.get(kb_id),
                user_id=user_id,
                explicitly_granted=kb_id in granted_ids,
            )
        ]

    def _granted_ids(self, user_id: str, kb_ids: list[str]) -> set[str]:
        if is_guest_principal(user_id) or not kb_ids:
            return set()
        return set(self._grant_lookup(user_id, kb_ids) or set())

    @staticmethod
    def _normalize(values: list[str]) -> list[str]:
        return normalize_unique_strings(values)

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

    @staticmethod
    def _lookup_grants_from_database(user_id: str, kb_ids: list[str]) -> set[str]:
        if not user_id or not kb_ids:
            return set()

        from datetime import datetime

        from sqlalchemy import or_, select
        from sqlalchemy.exc import SQLAlchemyError

        from backend.db.connection import get_session
        from backend.db.models.knowledge_base import KnowledgeBaseAcl

        session = get_session()
        try:
            rows = session.execute(
                select(KnowledgeBaseAcl.kb_id).where(
                    KnowledgeBaseAcl.kb_id.in_(kb_ids),
                    KnowledgeBaseAcl.subject_type == "user",
                    KnowledgeBaseAcl.subject_id == user_id,
                    KnowledgeBaseAcl.permission.in_(("read", "use")),
                    KnowledgeBaseAcl.effect == "allow",
                    or_(
                        KnowledgeBaseAcl.expires_at.is_(None),
                        KnowledgeBaseAcl.expires_at > datetime.now(),
                    ),
                )
            ).all()
            return {str(row.kb_id) for row in rows}
        except SQLAlchemyError:
            # Older databases may not have the ACL migration yet.  Fail closed:
            # owners/public KBs continue to work, grants do not.
            return set()
        finally:
            session.close()

    @staticmethod
    def _lookup_bindings_from_database(profile_code: str) -> list[str]:
        from sqlalchemy import select

        from backend.db.connection import get_session
        from backend.db.models.agent import Agent, AgentKnowledgeBase
        from backend.domain.agents.bindings import agent_binding_codes

        session = get_session()
        try:
            agent_codes = agent_binding_codes(profile_code)
            if not agent_codes:
                return []
            rows = session.execute(
                select(
                    AgentKnowledgeBase.agent_code,
                    AgentKnowledgeBase.kb_id,
                )
                .join(Agent, Agent.agent_code == AgentKnowledgeBase.agent_code)
                .where(
                    AgentKnowledgeBase.agent_code.in_(agent_codes),
                    Agent.is_active.is_(True),
                )
                .order_by(
                    AgentKnowledgeBase.sort_order,
                    AgentKnowledgeBase.id,
                )
            ).all()
            grouped: dict[str, list[str]] = {}
            for row in rows:
                grouped.setdefault(str(row.agent_code), []).append(str(row.kb_id))
            for agent_code in agent_codes:
                if grouped.get(agent_code):
                    return grouped[agent_code]
            return []
        finally:
            session.close()

    @staticmethod
    def _lookup_defaults_from_database() -> list[str]:
        from sqlalchemy import select

        from backend.db.connection import get_session
        from backend.db.models import KnowledgeBase
        from backend.db.models.knowledge_base import KnowledgeBaseStatus

        session = get_session()
        try:
            rows = session.execute(
                select(KnowledgeBase.kb_id)
                .where(
                    KnowledgeBase.is_default.is_(True),
                    KnowledgeBase.status == KnowledgeBaseStatus.ACTIVE,
                )
                .order_by(
                    KnowledgeBase.updated_at.desc(),
                    KnowledgeBase.created_at.desc(),
                )
            ).all()
            return [str(row.kb_id) for row in rows]
        finally:
            session.close()
