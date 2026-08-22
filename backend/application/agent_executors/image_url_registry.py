"""Request-scoped image URL registry for streaming model output."""

from __future__ import annotations

import contextvars
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

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
    object_name: str | None = None

    def to_metadata(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "caption": self.caption,
            "index": self.index,
        }
        if self.object_name:
            payload["object_name"] = self.object_name
        else:
            payload["url"] = self.url
        return payload


@dataclass
class ImageURLRegistry:
    _entries: dict[str, ImageEntry] = field(default_factory=dict)
    _url_to_token: dict[str, str] = field(default_factory=dict)
    _persistent_entries: dict[str, ImageEntry] = field(default_factory=dict)
    # 同一 request 级 registry 会被多个 KB 检索工作线程（run_blocking +
    # copy_context 共享同一实例）并发变更；用可重入锁保护 register/clear/遍历的
    # 读改写序列，防止 token 串号与 "dictionary changed size during iteration"。
    _lock: threading.RLock = field(
        default_factory=threading.RLock, compare=False, repr=False
    )

    @staticmethod
    def _reference_key(url: str, object_name: str | None = None) -> str:
        return f"kb-object:{object_name}" if object_name else f"url:{url}"

    @staticmethod
    def _extract_object_name(url: str) -> str | None:
        """kb-image 与 qb-image 代理 URL 都还原成稳定 object_name。

        qb 题图以 ``question-bank/`` 前缀区分：签名 URL（exp/sig）只是展示态，
        绝不能作为持久身份存进 ``registered_images``。
        """
        from backend.storage import extract_kb_image_object_name

        object_name = extract_kb_image_object_name(url)
        if object_name:
            return object_name
        from backend.application.question_bank.images import (
            extract_qb_image_object_name,
        )

        return extract_qb_image_object_name(url)

    @staticmethod
    def _proxy_url_for_object(object_name: str) -> str:
        if object_name.startswith("question-bank/"):
            from backend.application.question_bank.images import build_qb_image_url

            return build_qb_image_url(object_name)
        from backend.storage import build_kb_image_proxy_url

        return build_kb_image_proxy_url(object_name)

    def clear(self, *, include_persisted: bool = False) -> None:
        with self._lock:
            if include_persisted:
                self._persistent_entries.clear()
            self._entries = dict(self._persistent_entries)
            self._url_to_token = {}
            for token, entry in self._entries.items():
                self._url_to_token.setdefault(
                    self._reference_key(entry.url, entry.object_name),
                    token,
                )

    def replace_persisted(self, registered_images: dict[str, object] | None) -> None:
        """Seed the request registry with image identities from earlier turns."""

        self.clear(include_persisted=True)

        for raw_token, raw_entry in (registered_images or {}).items():
            token_match = re.fullmatch(
                r"\[IMAGE:(\d+)\]", str(raw_token or ""), re.IGNORECASE
            )
            if not token_match:
                continue
            token = canonical_image_token(token_match.group(1))
            data = raw_entry if isinstance(raw_entry, dict) else {"url": raw_entry}
            raw_url = str(data.get("url") or "").strip()
            object_name = str(data.get("object_name") or "").strip().lstrip("/") or None
            object_name = object_name or self._extract_object_name(raw_url)
            url = (
                self._proxy_url_for_object(object_name)
                if object_name
                else normalize_image_url(raw_url)
            )
            if not url:
                continue
            try:
                index = max(1, int(data.get("index") or token_match.group(1)))
            except (TypeError, ValueError):
                index = max(1, int(token_match.group(1)))
            entry = ImageEntry(
                url=url,
                caption=str(data.get("caption") or ""),
                index=index,
                object_name=object_name,
            )
            self._entries[token] = entry
            self._url_to_token.setdefault(
                self._reference_key(entry.url, entry.object_name),
                token,
            )
        self._persistent_entries = dict(self._entries)

    def register(self, url: str, caption: str = "") -> str:
        url = normalize_image_url(url)
        object_name = self._extract_object_name(url)
        reference_key = self._reference_key(url, object_name)
        # 「查去重表 → 算 max(index)+1 → 写两个 dict」必须原子，否则并发分支会给
        # 不同 URL 发同一 token（后写覆盖先写，最终正文图片张冠李戴）。
        with self._lock:
            if reference_key in self._url_to_token:
                return self._url_to_token[reference_key]

            index = (
                max((entry.index for entry in self._entries.values()), default=0) + 1
            )
            token = f"[IMAGE:{index:03d}]"
            while token in self._entries:
                index += 1
                token = f"[IMAGE:{index:03d}]"
            entry = ImageEntry(
                url=url,
                caption=caption,
                index=index,
                object_name=object_name,
            )
            self._entries[token] = entry
            self._url_to_token[reference_key] = token
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
        if not entry:
            return None
        if entry.object_name:
            # 按对象前缀走正确的重建路径：qb 题图签名走 /files/qb-image，
            # 其余走 kb-image；注册时保存的旧签名一律现签现用。
            return self._proxy_url_for_object(entry.object_name)
        return entry.url

    def is_empty(self) -> bool:
        return not bool(self._entries)

    def iter_entries(self) -> Iterator[tuple[str, ImageEntry]]:
        # 锁内取快照再 yield：避免并发 register 触发 "dictionary changed size
        # during iteration"，把一次成功的检索误报为失败。
        with self._lock:
            snapshot = list(self._entries.items())
        yield from snapshot

    def iter_persisted_entries(self) -> Iterator[tuple[str, ImageEntry]]:
        """Yield only server-validated image identities restored from prior turns."""

        yield from self._persistent_entries.items()

    def inject_missing_captioned_images(self, text: str) -> str:
        """Insert image markdown before numbered captions when the model omitted it."""

        if not text or not self._entries or "图" not in text:
            return text

        result = text
        check_text = re.sub(r"<think>[\s\S]*?</think>", "", result)
        markdown_image_text = "\n".join(
            match.group(0) for match in re.finditer(r"!\[[^\]]*\]\([^)]+\)", check_text)
        )
        with self._lock:
            snapshot = sorted(
                self._entries.items(), key=lambda item: item[1].index
            )
        available = [
            (token, entry)
            for token, entry in snapshot
            if token not in markdown_image_text
            and (self.resolve(token) or entry.url) not in markdown_image_text
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
            alt = (
                re.sub(r"^[*_]+|[*_]+$", "", raw_line).strip()
                or entry.caption
                or f"图{entry.index}"
            )
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
