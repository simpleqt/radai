"""Import tool providers declared in ToolSpec."""

from __future__ import annotations

import importlib
import inspect
from typing import Any

from backend.domain.tools.spec import ToolSpec
from backend.domain.tools.runtime_context import ToolRuntimeContext


class ToolProviderLoader:
    def load(self, spec: ToolSpec) -> Any:
        module_name, sep, attr_name = spec.provider.partition(":")
        if not sep or not module_name or not attr_name:
            raise ValueError(f"Invalid provider for tool {spec.name}: {spec.provider}")
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)

    def invoke(
        self,
        provider: Any,
        *,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> Any:
        """Invoke old zero-argument providers or context-aware providers."""
        signature = inspect.signature(provider)
        if "runtime_context" in signature.parameters:
            return provider(runtime_context=runtime_context)
        return provider()
