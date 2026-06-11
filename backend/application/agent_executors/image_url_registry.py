"""Request-scoped image URL registry for streaming model output."""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from typing import Iterator

_registry_var: contextvars.ContextVar[ImageURLRegistry | None] = contextvars.ContextVar(
    "image_url_registry", default=None
)

_TOKEN_PATTERN = re.compile(r"\[IMAGE:(\d+)\]")
_MAX_TOKEN_LEN = 14


@dataclass
class ImageEntry:
    url: str
    caption: str
    index: int


@dataclass
class ImageURLRegistry:
    _entries: dict[str, ImageEntry] = field(default_factory=dict)
    _url_to_token: dict[str, str] = field(default_factory=dict)

    def register(self, url: str, caption: str = "") -> str:
        if url in self._url_to_token:
            return self._url_to_token[url]

        index = len(self._entries) + 1
        token = f"[IMAGE:{index:03d}]"
        entry = ImageEntry(url=url, caption=caption, index=index)
        self._entries[token] = entry
        self._url_to_token[url] = token
        return token

    def resolve(self, token: str) -> str | None:
        entry = self._entries.get(token)
        return entry.url if entry else None

    def resolve_all(self, text: str) -> str:
        if not self._entries:
            return text

        def _replace(match: re.Match) -> str:
            token = match.group(0)
            entry = self._entries.get(token)
            return entry.url if entry else token

        return _TOKEN_PATTERN.sub(_replace, text)

    def is_empty(self) -> bool:
        return not bool(self._entries)

    def iter_entries(self) -> Iterator[tuple[str, ImageEntry]]:
        yield from self._entries.items()


def get_registry() -> ImageURLRegistry:
    registry = _registry_var.get()
    if registry is None:
        registry = ImageURLRegistry()
        _registry_var.set(registry)
    return registry


def reset_registry() -> None:
    _registry_var.set(ImageURLRegistry())


def register_image_url(url: str, caption: str = "") -> str:
    return get_registry().register(url, caption)


def resolve_image_tokens(text: str) -> str:
    return get_registry().resolve_all(text)


class StreamingTokenResolver:
    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        safe_end = self._find_safe_end()
        safe_text = self._buf[:safe_end]
        self._buf = self._buf[safe_end:]
        return resolve_image_tokens(safe_text)

    def flush(self) -> str:
        remaining = self._buf
        self._buf = ""
        return resolve_image_tokens(remaining)

    def _find_safe_end(self) -> int:
        buf = self._buf
        n = len(buf)
        for index in range(n - 1, max(-1, n - _MAX_TOKEN_LEN - 1), -1):
            if buf[index] == "[":
                suffix = buf[index:]
                if suffix.startswith("[IMAGE:") and "]" not in suffix:
                    return index
                if len(suffix) < 8:
                    return index
                break
        return n
