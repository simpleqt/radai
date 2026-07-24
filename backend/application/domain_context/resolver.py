"""Resolve request-scoped domain context declared by an agent profile."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest

DomainContextProvider = Callable[..., dict[str, Any]]


class DomainContextResolver:
    """Load and invoke the domain context provider declared by a profile."""

    def resolve(
        self,
        *,
        profile: AgentProfile,
        request: AgentRequest,
        provider: DomainContextProvider | None = None,
    ) -> dict[str, Any]:
        provider_path = str(profile.domain_context_provider or "").strip()
        if not provider_path:
            return {}
        provider = provider or self._load_provider(provider_path)
        resolved = provider(
            user_id=request.user_id,
            request_metadata=dict(request.metadata or {}),
        )
        if not isinstance(resolved, dict):
            raise ValueError(
                f"Domain context provider must return a mapping: {provider_path}"
            )
        return dict(resolved)

    @staticmethod
    def _load_provider(provider_path: str) -> DomainContextProvider:
        module_name, separator, attribute_name = provider_path.partition(":")
        if not separator or not module_name or not attribute_name:
            raise ValueError(f"Invalid domain context provider: {provider_path}")
        module = importlib.import_module(module_name)
        provider = getattr(module, attribute_name)
        if not callable(provider):
            raise ValueError(
                f"Domain context provider is not callable: {provider_path}"
            )
        return provider
