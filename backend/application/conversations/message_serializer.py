"""Conversation checkpoint message serialization helpers."""

from __future__ import annotations

from typing import Any

from backend.core.agents.context.summary import (
    CAPTURED_CONTENT_PREFIXES,
    SUMMARY_PREFIX,
)

# Index 0 is matched with ``in`` (legacy framework summary envelope); the
# remaining internal placeholder prefixes are matched with ``startswith`` and
# stay aligned with the middleware through the shared tuple. SUMMARY_PREFIX
# covers legacy compaction summaries whose ``context_compaction`` metadata was
# stripped: without it they surfaced in history APIs as user messages.
SUMMARY_MARKERS = (
    "Here is a summary of the conversation to date:",
    *CAPTURED_CONTENT_PREFIXES,
    SUMMARY_PREFIX,
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
    from backend.application.image_pipeline import normalize_image_url
    from backend.storage import build_kb_image_proxy_url, extract_kb_image_object_name

    if isinstance(entry, dict):
        object_name = str(entry.get("object_name") or "").strip().lstrip("/")
        if object_name:
            if object_name.startswith("question-bank/"):
                # 题库题图：稳定 object_name 是持久身份，URL 现签现用。
                from backend.application.question_bank.images import build_qb_image_url

                return build_qb_image_url(object_name)
            return build_kb_image_proxy_url(object_name)
        url = str(entry.get("url") or "").strip()
        if extract_kb_image_object_name(url):
            return normalize_image_url(url)
        from backend.application.question_bank.images import (
            extract_qb_image_object_name,
        )

        qb_object_name = extract_qb_image_object_name(url)
        if qb_object_name:
            from backend.application.question_bank.images import build_qb_image_url

            return build_qb_image_url(qb_object_name)
        return url or None
    if isinstance(entry, str):
        return normalize_image_url(entry)
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
            metadata.get("context_compaction") if isinstance(metadata, dict) else None
        )
        if isinstance(compaction_metadata, dict) and compaction_metadata.get(
            "window_id"
        ):
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
