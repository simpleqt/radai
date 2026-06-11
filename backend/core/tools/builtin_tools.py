"""Built-in tools for agent graphs.

Delegated tools such as `task` are provided by backend.core when needed.
"""

from typing import Literal

from langchain.tools import tool


@tool("ask_clarification", parse_docstring=True, return_direct=True)
def ask_clarification_tool(
    question: str,
    clarification_type: Literal[
        "missing_info",
        "ambiguous_requirement",
        "approach_choice",
        "risk_confirmation",
        "suggestion",
    ],
    context: str | None = None,
    options: list[str] | None = None,
) -> str:
    """Ask the user for clarification when you need more information to proceed.

    Use this tool when you encounter situations where you cannot proceed without user input:
    - **Missing information**: Required details not provided
    - **Ambiguous requirements**: Multiple valid interpretations exist
    - **Approach choices**: Several valid approaches exist
    - **Risky operations**: Destructive actions needing confirmation
    - **Suggestions**: Recommendation needing user approval

    Args:
        question: The clarification question to ask the user.
        clarification_type: The type of clarification needed.
        context: Optional context explaining why clarification is needed.
        options: Optional list of choices for approach_choice or suggestion types.
    """
    return "Clarification request processed by middleware"
