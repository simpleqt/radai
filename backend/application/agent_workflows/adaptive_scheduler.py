"""Pure scheduling decisions for coordinated research workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar


class SchedulableTask(Protocol):
    key: str
    priority: int
    dependencies: tuple[str, ...]
    risk: str


TaskT = TypeVar("TaskT", bound=SchedulableTask)


@dataclass(frozen=True)
class SchedulerDecision:
    wave: list[tuple[int, SchedulableTask]]
    dependency_fallback: bool


def select_next_wave(
    remaining: list[tuple[int, TaskT]],
    *,
    known_keys: set[str],
    terminal_keys: set[str],
    capacity: int,
) -> SchedulerDecision:
    """Select tasks whose dependencies have reached a terminal state.

    Dependencies order execution; they do not require every upstream branch to
    succeed.  A downstream synthesis task must still run after an upstream
    failure so it can use the successful subset and report the missing
    evidence explicitly.  Only a graph that cannot make topological progress
    triggers ``dependency_fallback``.
    """
    ready: list[tuple[int, TaskT]] = []
    blocked: list[tuple[int, TaskT]] = []
    for index, task in remaining:
        dependencies = [
            dependency
            for dependency in task.dependencies
            if dependency in known_keys and dependency != task.key
        ]
        target = ready if all(dependency in terminal_keys for dependency in dependencies) else blocked
        target.append((index, task))

    dependency_fallback = not ready and bool(blocked)
    candidates = [] if dependency_fallback else ready
    risk_order = {"critical": 3, "high": 2, "medium": 1, "low": 0}
    candidates = sorted(
        candidates,
        key=lambda pair: (
            -pair[1].priority,
            -risk_order.get(str(getattr(pair[1], "risk", "medium")).lower(), 1),
            pair[0],
        ),
    )
    selected: list[tuple[int, TaskT]] = []
    # ``budget`` describes reasoning/tool effort, not a semaphore cost.  Every
    # branch occupies one executor slot; mixing these concepts previously made
    # a nominal concurrency capacity run fewer specialists than requested.
    selected = candidates[: max(1, capacity)]
    return SchedulerDecision(
        wave=list(selected),
        dependency_fallback=dependency_fallback,
    )
