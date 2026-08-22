"""Manifest-backed capability roles for coordinated research."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from backend.application.agents.runtime_agent_loader import RuntimeAgentLoader
from backend.domain.agents.runtime_agent import RuntimeAgentSpec


@dataclass(frozen=True)
class ResearchSpecialist:
    """Planner-facing view of one executable runtime-agent role."""

    key: str
    title: str
    planning_description: str
    instruction: str
    aliases: tuple[str, ...] = ()


def _effective_agent_specs(
    agent_specs: Mapping[str, RuntimeAgentSpec] | None,
) -> Mapping[str, RuntimeAgentSpec]:
    # Request contexts created before the runtime-agent manifest was introduced
    # may carry an empty mapping. Keep those callers compatible while making a
    # non-empty request-scoped mapping authoritative.
    if agent_specs:
        return agent_specs
    return RuntimeAgentLoader().load_all()


def planner_research_specialists(
    agent_specs: Mapping[str, RuntimeAgentSpec] | None = None,
) -> tuple[ResearchSpecialist, ...]:
    """Return planner-selectable roles in manifest declaration order."""

    specialists: list[ResearchSpecialist] = []
    for spec in _effective_agent_specs(agent_specs).values():
        if not spec.planner_selectable:
            continue
        specialists.append(
            ResearchSpecialist(
                key=spec.name,
                title=str(spec.title or spec.name),
                planning_description=str(spec.planning_description or spec.description),
                instruction=spec.system_prompt,
                aliases=tuple(spec.aliases),
            )
        )
    if not specialists:
        raise ValueError(
            "Runtime agent manifest declares no planner-selectable research roles"
        )
    return tuple(specialists)


def normalize_research_specialist(
    value: str | None,
    *,
    agent_specs: Mapping[str, RuntimeAgentSpec] | None = None,
    default_index: int | None = 1,
) -> str:
    """Resolve a canonical planner role or alias without silent substitution.

    An omitted role retains the legacy, deterministic round-robin fallback.
    A non-empty unknown role is a contract error and is never mapped to an
    unrelated specialist.
    """

    specialists = planner_research_specialists(agent_specs)
    canonical = {specialist.key: specialist.key for specialist in specialists}
    aliases: dict[str, str] = {}
    for specialist in specialists:
        for alias in specialist.aliases:
            owner = aliases.get(alias)
            if alias in canonical and alias != specialist.key:
                raise ValueError(
                    f"Planner role alias {alias!r} conflicts with a canonical role"
                )
            if owner is not None and owner != specialist.key:
                raise ValueError(
                    f"Planner role alias {alias!r} is ambiguous: {owner}, "
                    f"{specialist.key}"
                )
            aliases[alias] = specialist.key

    key = str(value or "").strip()
    if not key:
        if default_index is None:
            raise ValueError("Research task is missing required field: agent_type")
        position = max(0, int(default_index) - 1) % len(specialists)
        return specialists[position].key
    if key in canonical:
        return key
    if key in aliases:
        return aliases[key]
    available = ", ".join(specialist.key for specialist in specialists)
    raise ValueError(
        f"Unknown planner-selectable runtime agent {key!r}; available roles: "
        f"{available}"
    )


