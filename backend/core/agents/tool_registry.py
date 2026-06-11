"""Tool registry — provides available tools for agent graph construction.

Only loads tools that the project actually uses.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


def get_builtin_tools(
    *,
    subagent_enabled: bool = False,
) -> list[BaseTool]:
    """Return built-in tools needed by the agent."""
    tools: list[BaseTool] = []

    if subagent_enabled:
        try:
            from backend.core.tools.task_tool import task_tool
            tools.append(task_tool)
            logger.info("Including subagent tools (task)")
        except ImportError:
            logger.warning("task_tool unavailable (backend.core.tools.task_tool import failed)")

    return tools


def get_available_tools(
    *,
    model_name: str | None = None,
    subagent_enabled: bool = False,
) -> list[BaseTool]:
    """Get all available tools for agent construction.

    Args:
        model_name: Model name (unused, kept for API compatibility).
        subagent_enabled: Whether to include task tool.
    """
    builtin_tools = get_builtin_tools(subagent_enabled=subagent_enabled)

    logger.info("Tool registry: builtin_tools=%d", len(builtin_tools))

    return builtin_tools
