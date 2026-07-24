"""Request-scoped image URL registry for streaming model output."""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from typing import Iterator

from backend.application.image_pipeline import (
    canonical_image_token,
    find_safe_image_stream_end,
    normalize_image_url,
    rewrite_image_references,
)

_registry_var: contextvars.ContextVar[ImageURLRegistry | None] = contextvars.ContextVar(
    "image_url_registry", default=None
)


@dataclass
class ImageEntry:
    url: str
    caption: str
    index: int


@dataclass
class ImageURLRegistry:
    _entries: dict[str, ImageEntry] = field(default_factory=dict)
    _url_to_token: dict[str, str] = field(default_factory=dict)

    def clear(self) -> None:
        self._entries.clear()
        self._url_to_token.clear()

    def register(self, url: str, caption: str = "") -> str:
        url = normalize_image_url(url)
        if url in self._url_to_token:
            return self._url_to_token[url]

        index = len(self._entries) + 1
        token = f"[IMAGE:{index:03d}]"
        entry = ImageEntry(url=url, caption=caption, index=index)
        self._entries[token] = entry
        self._url_to_token[url] = token
        return token

    def resolve_all(self, text: str, *, promote_standalone: bool = True) -> str:
        return rewrite_image_references(
            text,
            resolve_token=self.resolve,
            promote_standalone=promote_standalone,
        )

    def resolve_token_text(self, token_text: str) -> str | None:
        return self.resolve(token_text)

    def resolve(self, token: str) -> str | None:
        if not token:
            return None
        if token.startswith("IMAGE:"):
            token = canonical_image_token(token.split(":", 1)[1])
        elif token.startswith("[IMAGE:") and token.endswith("]"):
            token = canonical_image_token(token[7:-1])
        entry = self._entries.get(token)
        return entry.url if entry else None

    def is_empty(self) -> bool:
        return not bool(self._entries)

    def iter_entries(self) -> Iterator[tuple[str, ImageEntry]]:
        yield from self._entries.items()

    def inject_missing_captioned_images(self, text: str) -> str:
        """Insert image markdown before numbered captions when the model omitted it."""

        if not text or not self._entries or "图" not in text:
            return text

        result = text
        check_text = re.sub(r"<think>[\s\S]*?</think>", "", result)
        markdown_image_text = "\n".join(
            match.group(0)
            for match in re.finditer(r"!\[[^\]]*\]\([^)]+\)", check_text)
        )
        available = [
            (token, entry)
            for token, entry in sorted(self._entries.items(), key=lambda item: item[1].index)
            if token not in markdown_image_text and entry.url not in markdown_image_text
        ]
        if not available:
            return result

        next_image = iter(available)
        caption_line = re.compile(
            r"(?m)^(?P<indent>[ \t]*)(?P<line>(?:[*_]{1,2})?\s*图\s*"
            r"\d+(?:\s*[.．]\s*\d+)*\s*(?:[：:]|\s+|$)[^\n]{0,180}(?:[*_]{1,2})?\s*)$"
        )

        def _insert(match: re.Match) -> str:
            previous = result[max(0, match.start() - 260) : match.start()]
            if re.search(r"!\[[^\]]*\]\([^)]+\)\s*$", previous):
                return match.group(0)
            try:
                token, entry = next(next_image)
            except StopIteration:
                return match.group(0)
            raw_line = match.group("line").strip()
            alt = re.sub(r"^[*_]+|[*_]+$", "", raw_line).strip() or entry.caption or f"图{entry.index}"
            return f"\n\n![{alt}]({token})\n\n{match.group(0)}"

        result = caption_line.sub(_insert, result)
        return result


def get_registry() -> ImageURLRegistry:
    import logging
    logger = logging.getLogger("backend.image_url_registry")
    registry = _registry_var.get()
    if registry is None:
        registry = ImageURLRegistry()
        _registry_var.set(registry)
        logger.info("[ImageURLRegistry] Created new registry ID: %s", id(registry))
    return registry


def reset_registry() -> None:
    registry = _registry_var.get()
    if registry is None:
        _registry_var.set(ImageURLRegistry())
    else:
        registry.clear()


def register_image_url(url: str, caption: str = "") -> str:
    return get_registry().register(url, caption)


def resolve_image_tokens(text: str, *, promote_standalone: bool = True) -> str:
    return get_registry().resolve_all(text, promote_standalone=promote_standalone)


def inject_missing_captioned_images(text: str) -> str:
    return get_registry().inject_missing_captioned_images(text)


class StreamingTokenResolver:
    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        safe_end = self._find_safe_end()
        safe_text = self._buf[:safe_end]
        self._buf = self._buf[safe_end:]
        return resolve_image_tokens(safe_text, promote_standalone=False)

    def flush(self) -> str:
        remaining = self._buf
        self._buf = ""
        return resolve_image_tokens(remaining, promote_standalone=False)

    def _find_safe_end(self) -> int:
        return find_safe_image_stream_end(self._buf)
