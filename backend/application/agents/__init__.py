"""Agent application helpers."""

__all__ = [
    "AgentDefinitionIssue",
    "AgentDefinitionValidationError",
    "AgentDefinitionValidationReport",
    "AgentDefinitionValidator",
    "ResolvedAgentDefinition",
]


def __getattr__(name: str):
    """Lazily expose definition contracts without making profile imports heavy."""
    if name not in __all__:
        raise AttributeError(name)
    from backend.application.agents import definition

    return getattr(definition, name)
