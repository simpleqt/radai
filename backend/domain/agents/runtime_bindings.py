"""Immutable, serializable bindings selected for one agent definition.

This domain contract deliberately contains names only.  Provider callables and
application registry objects stay at the composition root and never cross the
request-context serialization boundary.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentRuntimeBindings:
    """Canonical registry names projected from a resolved agent definition."""

    agent_code: str
    domain: str
    prompt_profile: str
    tool_policy: str
    guardrail_policy: str
    default_workflow: str
    workflow_names: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()
    domain_context_provider: str | None = None
    skill_dependencies: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        # Defensively freeze callers that pass list-like values despite the
        # type annotation.  This keeps a frozen instance deeply immutable.
        object.__setattr__(self, "workflow_names", tuple(self.workflow_names))
        object.__setattr__(self, "tool_names", tuple(self.tool_names))
        object.__setattr__(self, "skill_names", tuple(self.skill_names))
        object.__setattr__(
            self,
            "skill_dependencies",
            tuple(
                (
                    str(skill_name),
                    tuple(str(dependency) for dependency in dependencies),
                )
                for skill_name, dependencies in self.skill_dependencies
            ),
        )
