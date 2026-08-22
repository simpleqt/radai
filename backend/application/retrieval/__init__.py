"""Agent-side retrieval query-text utilities.

The retrieval runtime lives in the omnigraph knowledge service; radai keeps
only the pure query-resolution helpers used by streaming/lightweight executors.
"""

from backend.application.retrieval.query_resolution import (
    compose_retrieval_query,
    effective_tool_query,
    latest_user_message_text,
)

__all__ = [
    "compose_retrieval_query",
    "effective_tool_query",
    "latest_user_message_text",
]
