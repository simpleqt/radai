"""Canonical Skill-to-Agent domain compatibility policy."""

from __future__ import annotations

from collections.abc import Iterable

# These legacy packages historically used their package key as ``domain`` even
# though they are intentionally available to every agent. Keep that behavior in
# one place. New cross-agent packages should declare ``general`` or ``*``.
SHARED_SKILL_DOMAINS = frozenset(
    {
        "general-table-analysis",
        "html-ppt",
        "deep-research",
        "academic-search",
        "excel-experiment-analysis",
    }
)


def normalize_domain(value: object) -> str:
    return str(value or "").strip().lower()


def skill_domain_matches(
    skill_domain: object,
    *,
    agent_code: object,
    agent_domain: object,
    shared_domains: Iterable[str] = SHARED_SKILL_DOMAINS,
) -> bool:
    """Return whether a Skill domain can be used by an agent."""

    normalized = normalize_domain(skill_domain)
    compatible = {
        "*",
        "general",
        normalize_domain(agent_code),
        normalize_domain(agent_domain),
        *(normalize_domain(item) for item in shared_domains),
    }
    compatible.discard("")
    return normalized in compatible


__all__ = [
    "SHARED_SKILL_DOMAINS",
    "normalize_domain",
    "skill_domain_matches",
]
