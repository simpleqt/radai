"""Shared retrieval-query resolution helpers for conversational workflows.

The answer model may contextualize a follow-up before calling a retrieval tool,
but its rewrite is only a hint.  The literal user turn remains part of the
effective query so explicit numbers, document references, and media intent
cannot disappear during rewriting.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class RetrievalTurnContext:
    """Request-local information needed by every retrieval entry point."""

    current_question: str = ""


_RETRIEVAL_TURN_CONTEXT: ContextVar[RetrievalTurnContext] = ContextVar(
    "retrieval_turn_context",
    default=RetrievalTurnContext(),
)


def set_retrieval_turn_context(question: str) -> Token[RetrievalTurnContext]:
    """Bind the literal user turn to the current async request task."""

    return _RETRIEVAL_TURN_CONTEXT.set(
        RetrievalTurnContext(current_question=_clean_query_text(question))
    )


def reset_retrieval_turn_context(token: Token[RetrievalTurnContext]) -> None:
    _RETRIEVAL_TURN_CONTEXT.reset(token)


def get_retrieval_turn_context() -> RetrievalTurnContext:
    return _RETRIEVAL_TURN_CONTEXT.get()


def compose_retrieval_query(
    *,
    current_question: str,
    contextual_query: str,
    max_chars: int = 4000,
) -> str:
    """Combine a contextual rewrite with the literal turn without losing either.

    ``contextual_query`` supplies the missing subject for follow-ups such as
    "其中的参数呢".  ``current_question`` preserves every constraint the user
    explicitly wrote in this turn.  Containment checks avoid duplicating a
    faithful standalone rewrite.
    """

    current = _clean_query_text(current_question)
    contextual = _clean_query_text(contextual_query)
    if not contextual:
        return current[:max_chars]
    if not current:
        return contextual[:max_chars]

    normalized_current = _normalize_for_comparison(current)
    normalized_contextual = _normalize_for_comparison(contextual)
    if not normalized_contextual or normalized_contextual == normalized_current:
        return current[:max_chars]
    if normalized_current and normalized_current in normalized_contextual:
        return _fit_contextual_and_current(
            contextual=contextual,
            current=current,
            max_chars=max_chars,
        )
    if normalized_contextual in normalized_current:
        return current[:max_chars]

    # Put the standalone/contextualized form first for embedding quality.  The
    # literal turn follows as an invariant-preserving clause for media intent,
    # numbers, units, formula symbols, and exact document references.
    return _fit_contextual_and_current(
        contextual=contextual,
        current=current,
        max_chars=max_chars,
    )


def latest_user_message_text(messages: Iterable[Any] | None) -> str:
    """Return the latest human message from LangChain objects or dictionaries."""

    for message in reversed(list(messages or [])):
        if _is_human_message(message):
            return _content_to_text(
                message.get("content")
                if isinstance(message, dict)
                else getattr(message, "content", "")
            )
    return ""


def effective_tool_query(model_query: str) -> str:
    """Resolve a Full-mode tool query against the bound literal user turn."""

    return compose_retrieval_query(
        current_question=get_retrieval_turn_context().current_question,
        contextual_query=model_query,
    )


def _clean_query_text(value: Any) -> str:
    text = _content_to_text(value)
    return re.sub(r"[ \t\f\v]+", " ", text).strip()


def _normalize_for_comparison(value: str) -> str:
    return re.sub(r"[^\w]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _fit_contextual_and_current(
    *,
    contextual: str,
    current: str,
    max_chars: int,
) -> str:
    """Fit a query while keeping the literal current turn at the tail."""

    max_chars = max(0, int(max_chars))
    if max_chars == 0:
        return ""
    normalized_current = _normalize_for_comparison(current)
    if (
        normalized_current
        and len(contextual) <= max_chars
        and normalized_current in _normalize_for_comparison(contextual)
    ):
        return contextual
    if len(current) >= max_chars:
        return current[:max_chars]
    contextual_budget = max_chars - len(current) - 1
    contextual_prefix = contextual[:contextual_budget].rstrip()
    if not contextual_prefix:
        return current
    return f"{contextual_prefix}\n{current}"


def _is_human_message(message: Any) -> bool:
    if isinstance(message, dict):
        role = str(message.get("role") or message.get("type") or "").lower()
    else:
        role = str(
            getattr(message, "type", None)
            or getattr(message, "role", None)
            or ""
        ).lower()
    return role in {"human", "user"}


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    return str(content or "").strip()


__all__ = [
    "RetrievalTurnContext",
    "compose_retrieval_query",
    "effective_tool_query",
    "get_retrieval_turn_context",
    "latest_user_message_text",
    "reset_retrieval_turn_context",
    "set_retrieval_turn_context",
]
