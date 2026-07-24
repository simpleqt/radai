"""Conversation checkpoint message serialization helpers."""

from __future__ import annotations

from typing import Any

SUMMARY_MARKERS = (
    "Here is a summary of the conversation to date:",
    "[State Elevation:",
    "[Context Isolation:",
    "[Result Capture:",
    "[System Notification:",
)


def resolve_registered_image_token(
    token: str,
    registered_images: dict[str, Any] | None,
) -> str | None:
    """Resolve a registered image token to a public URL."""
    if not token:
        return None
    entry = (registered_images or {}).get(token)
    if not entry:
        return None
    if isinstance(entry, dict):
        return entry.get("url")
    if isinstance(entry, str):
        return entry
    return None


def serialize_checkpoint_messages(
    messages: list[Any],
    registered_images: dict[str, Any] | None = None,
    *,
    coerce_content_to_str: bool = False,
) -> list[dict[str, Any]]:
    """Serialize LangChain/LangGraph checkpoint messages for API responses."""
    from backend.application.image_pipeline import rewrite_image_references

    serialized: list[dict[str, Any]] = []
    for message in messages:
        role = "user"
        if hasattr(message, "type"):
            role = message.type
        elif isinstance(message, dict):
            role = message.get("type", "user")

        content: Any = ""
        if hasattr(message, "content"):
            content = message.content
        elif isinstance(message, dict):
            content = message.get("content", "")

        if isinstance(content, str) and role in ("ai", "assistant"):
            content = rewrite_image_references(
                content,
                resolve_token=lambda token: resolve_registered_image_token(
                    token,
                    registered_images,
                ),
            )

        metadata: dict[str, Any] = {}
        if hasattr(message, "additional_kwargs"):
            metadata = message.additional_kwargs
        elif isinstance(message, dict) and "additional_kwargs" in message:
            metadata = message["additional_kwargs"]
        elif isinstance(message, dict) and "metadata" in message:
            metadata = message["metadata"]

        if metadata and metadata.get("lc_source") == "summarization":
            continue

        compaction_metadata = (
            metadata.get("context_compaction")
            if isinstance(metadata, dict)
            else None
        )
        if isinstance(compaction_metadata, dict) and compaction_metadata.get("window_id"):
            serialized.append(
                {
                    "role": "context_compaction",
                    "content": content,
                    "metadata": {"context_compaction": compaction_metadata},
                    "id": getattr(message, "id", None),
                }
            )
            continue

        if isinstance(content, str) and (
            "Here is a summary of the conversation to date:" in content
            or content.startswith(SUMMARY_MARKERS[1:])
        ):
            continue

        if coerce_content_to_str and not isinstance(content, str):
            content = str(content)

        serialized.append(
            {
                "role": role,
                "content": content,
                "metadata": metadata,
                "id": getattr(message, "id", None),
            }
        )
    return serialized
