"""Tool registry and policy loading.

Keep this package facade lazy.  Declarative tooling such as ``agent validate``
imports the lightweight YAML registry directly and must not initialize the
runtime assembler, logging, or application settings merely because Python
loads the parent package first.
"""

from importlib import import_module
from typing import Any

__all__ = [
    "PolicyToolAssembler",
    "ToolAssemblyContext",
    "ToolHealth",
    "ToolRuntimeState",
    "ToolPolicyAudit",
    "tool_runtime_state",
    "ToolAssemblyPipeline",
    "ToolRegistry",
]

_EXPORT_MODULES = {
    "PolicyToolAssembler": "backend.application.tools.assembler",
    "ToolAssemblyContext": "backend.application.tools.assembler",
    "ToolHealth": "backend.application.tools.assembler",
    "ToolPolicyAudit": "backend.application.tools.assembler",
    "ToolRegistry": "backend.application.tools.registry",
    "ToolRuntimeState": "backend.application.tools.runtime_state",
    "tool_runtime_state": "backend.application.tools.runtime_state",
    "ToolAssemblyPipeline": "backend.application.tools.tool_assembly_pipeline",
}


def __getattr__(name: str) -> Any:
    """Load runtime-only exports on first use, preserving the public API."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
