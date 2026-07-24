"""Composition boundary shared by coordinated workflow runtime components."""

from __future__ import annotations

from typing import Any


class CoordinationComponent:
    """Delegate shared policy reads and extension hooks to the workflow host.

    Runtime components own execution algorithms while the host remains the
    source of request policy, budgets, tracing, and compatibility overrides.
    Components in this layer are intentionally stateless per request.
    """

    __slots__ = ("_host",)

    def __init__(self, host: Any) -> None:
        self._host = host

    def __getattr__(self, name: str) -> Any:
        return getattr(self._host, name)
