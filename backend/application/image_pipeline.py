"""Unified image handling for agent-visible answers."""

from __future__ import annotations

import re
from collections.abc import Callable

from backend.config.loggers import get_logger

logger = get_logger(__name__)

IMAGE_TOKEN_PATTERN = re.compile(r"\[IMAGE:(\d+)\]")
BARE_IMAGE_TOKEN_PATTERN = re.compile(r"(?<![\[\w])IMAGE:(\d+)(?!\d)")
UNRESOLVED_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\[[^\]]*\]\(\s*(?:\[?IMAGE:[^\]\)\n]+\]?)\s*\)",
    re.IGNORECASE,
)
EMPTY_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(\s*\)")
EMPTY_HTML_IMAGE_PATTERN = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*(['\"])\s*\1[^>]*>",
    re.IGNORECASE,
)
UNRESOLVED_IMAGE_TOKEN_PATTERN = re.compile(
    r"\[?IMAGE:[^\]\s\)]+(?:\])?", re.IGNORECASE
)
MINIO_IMAGE_URL_PATTERN = re.compile(
    r"(?P<url>(?:https?://[^\s<>'\"\]\)]+)?/files/"
    r"[^\s<>'\"\]\)]*?\.(?:jpg|jpeg|png|gif|webp|bmp|tif|tiff)"
    r"(?:\?[^\s<>'\"\]\)]*)?)",
    re.IGNORECASE,
)
# 已经是浏览器直连形态的图片代理 URL（知识库图 / 题库图 / 工件下载）。它们携带
# 自己的签名，不得再走 MinIO 对象归一化——题库图 URL 的查询参数是
# name=question-bank/…，会被 /files/ 模式误当对象路径改写成不存在的
# object_name=qb-image 并叠出 /api/v2/api/v2/ 双前缀，导致流式期间图片 404
# 裂图；工件下载链接（object_name/filename 以图片扩展名结尾时）同理会被
# 改写成不存在的 kb-image 对象。
BROWSER_IMAGE_PROXY_PREFIXES = (
    "/api/v2/files/kb-image?",
    "/api/v2/files/qb-image?",
    "/api/v2/files/minio-artifact/download",
    "/files/kb-image?",
    "/files/qb-image?",
    "/files/minio-artifact/download",
)
_BROWSER_IMAGE_PROXY_MARKERS = (
    "/api/v2/files/kb-image?",
    "/api/v2/files/qb-image?",
    "/files/minio-artifact/download",
)


def is_browser_image_proxy_url(url: str) -> bool:
    """Return True when the URL is already a browser-facing signed proxy URL."""

    raw = url or ""
    if any(raw.startswith(prefix) for prefix in BROWSER_IMAGE_PROXY_PREFIXES):
        return True
    # 绝对源形态（https://host/api/v2/...）不做前缀匹配，但路径标记足以判定。
    return any(marker in raw for marker in _BROWSER_IMAGE_PROXY_MARKERS)
MARKDOWN_IMAGE_URL_PATTERN = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<url>"
    r"(?:https?://[^\s<>'\"\]\)]+)?/files/[^\s<>'\"\]\)]*?"
    r"\.(?:jpg|jpeg|png|gif|webp|bmp|tif|tiff)(?:\?[^\s<>'\"\]\)]*)?"
    r"|https?://[^\s<>'\"\]\)]+?\.(?:jpg|jpeg|png|gif|webp|bmp|tif|tiff)(?:\?[^\s<>'\"\]\)]*)?"
    r"|/api/v2/files/kb-image\?[^\s<>'\"\]\)]+"
    r")\s*\)",
    re.IGNORECASE,
)
CONTEXT_IMAGE_URL_PATTERN = re.compile(
    r"(?P<url>"
    r"(?:https?://[^\s<>'\"\]\)]+)?/files/[^\s<>'\"\]\)]*?"
    r"\.(?:jpg|jpeg|png|gif|webp|bmp|tif|tiff)(?:\?[^\s<>'\"\]\)]*)?"
    r"|https?://[^\s<>'\"\]\)]+?\.(?:jpg|jpeg|png|gif|webp|bmp|tif|tiff)(?:\?[^\s<>'\"\]\)]*)?"
    r"|/api/v2/files/kb-image\?[^\s<>'\"\]\)]+"
    r")",
    re.IGNORECASE,
)
HTML_IMAGE_URL_PATTERN = re.compile(
    r"<img\b(?P<attrs>[^>]*?)\bsrc\s*=\s*(['\"])(?P<url>[^'\"]+)\2[^>]*>",
    re.IGNORECASE,
)
STANDALONE_IMAGE_URL_PATTERN = re.compile(
    r"`?(?P<url>(?:https?://[^\s`]+?\.(?:jpg|jpeg|png|gif|bmp|webp|tiff|tif)"
    r"|/api/v2/files/kb-image\?[^\s`]+"
    r"|/api/v2/files/qb-image\?[^\s`]+"
    r"|/files/[^\s`]+?\.(?:jpg|jpeg|png|gif|bmp|webp|tiff|tif)))`?",
    re.IGNORECASE,
)
ANSWER_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(\s*[^)\n]+\s*\)")
ANSWER_HTML_IMAGE_PATTERN = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
ANSWER_FENCED_CODE_PATTERN = re.compile(r"```[\s\S]*?```")
BARE_KB_IMAGE_PROXY_URL_PATTERN = re.compile(
    r"(?<![\w<\[])" r"(?P<url>/api/v2/files/kb-image\?[^\s<>'\"\]\)]+)",
    re.IGNORECASE,
)
KB_IMAGE_PROXY_URL_PATTERN = re.compile(
    r"(?P<url>(?:https?://[^\s<>'\"\]\)\|]+)?"
    r"/api/v2/files/kb-image\?[^\s<>'\"\]\)\|]+)",
    re.IGNORECASE,
)
# 题库题图代理与 kb-image 同构，但签名域独立（name=question-bank/…）；
# 历史正文里的 qb URL 同样需要刷新签名（TTL 默认 24h）。
QB_IMAGE_PROXY_URL_PATTERN = re.compile(
    r"(?P<url>(?:https?://[^\s<>'\"\]\)\|]+)?"
    r"/api/v2/files/qb-image\?[^\s<>'\"\]\)\|]+)",
    re.IGNORECASE,
)
BARE_QB_IMAGE_PROXY_URL_PATTERN = re.compile(
    r"(?<![\w<\[])" r"(?P<url>/api/v2/files/qb-image\?[^\s<>'\"\]\)]+)",
    re.IGNORECASE,
)
INTERNAL_MINIO_URL_PATTERN = re.compile(
    r"https?://[^\s\)\]）\"']+/files/[^\s\)\]）\"']*",
    re.IGNORECASE,
)

MAX_IMAGE_TOKEN_LEN = 14
MAX_IMAGE_URL_CARRY_LEN = 4096


def canonical_image_token(number: str) -> str:
    try:
        return f"[IMAGE:{int(number):03d}]"
    except (TypeError, ValueError):
        return f"[IMAGE:{number}]"


def normalize_image_url(url: str) -> str:
    """Return the only browser-facing image URL shape used by agent answers."""

    raw = (url or "").strip()
    if not raw:
        return raw
    if raw.startswith(("data:", "/api/")):
        return raw
    if is_browser_image_proxy_url(raw):
        return raw

    try:
        from backend.storage import get_minio_image_proxy_url

        proxy_url = get_minio_image_proxy_url(raw)
        if proxy_url:
            return proxy_url
    except Exception as exc:
        logger.debug("[image_pipeline] proxy URL build failed: %s", exc)

    if raw.startswith(("http://", "https://")):
        return raw

    try:
        from backend.storage import (
            get_minio_image_proxy_url,
            get_presigned_url,
            get_public_url,
        )

        obj_key = raw.lstrip("/").replace("\\", "/")
        # Object keys are not URLs.  Resolve them through the same-origin proxy
        # directly so a scheme-less MinIO endpoint (for example
        # ``minio.internal:9000``) cannot leak into browser-facing output as a
        # malformed relative URL.
        proxy = get_minio_image_proxy_url(f"/files/{obj_key}")
        if proxy:
            return proxy
        public = get_public_url(obj_key)
        if public:
            return get_minio_image_proxy_url(public) or public
        presigned = get_presigned_url(obj_key)
        if presigned:
            return presigned
    except Exception as exc:
        logger.debug("[image_pipeline] object key URL build failed: %s", exc)

    return raw


def strip_internal_urls_from_caption(caption: str) -> str:
    """Remove MinIO implementation details from captions before prompting models."""

    if not caption:
        return caption
    cleaned = re.sub(
        r"[（(][^（(）)]*" + INTERNAL_MINIO_URL_PATTERN.pattern + r"[^（(）)]*[）)]",
        "",
        caption,
        flags=re.IGNORECASE,
    )
    cleaned = INTERNAL_MINIO_URL_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"  +", " ", cleaned).strip()
    return cleaned.rstrip("，,；;、")


def rewrite_image_references(
    text: str,
    *,
    resolve_token: Callable[[str], str | None] | None = None,
    promote_standalone: bool = True,
) -> str:
    """Normalize every image reference before text reaches the frontend."""

    if not text:
        return text

    resolved = _refresh_kb_image_proxy_urls(text)
    resolved = _refresh_qb_image_proxy_urls(resolved)
    resolved = _rewrite_minio_image_urls(resolved)
    if resolve_token is not None and "IMAGE:" in resolved:
        resolved = IMAGE_TOKEN_PATTERN.sub(
            lambda match: resolve_token(canonical_image_token(match.group(1)))
            or match.group(0),
            resolved,
        )
        resolved = BARE_IMAGE_TOKEN_PATTERN.sub(
            lambda match: resolve_token(canonical_image_token(match.group(1)))
            or match.group(0),
            resolved,
        )
    resolved = strip_unresolved_image_tokens(resolved)
    resolved = promote_proxy_image_table_cells(resolved)
    return promote_standalone_image_urls(resolved) if promote_standalone else resolved


def replace_image_urls_for_model_context(
    text: str,
    *,
    register_image: Callable[[str, str], str],
    default_caption: str = "图片",
) -> str:
    """Replace image URLs in model context with registered image tokens.

    This prevents models from copying MinIO/proxy URLs into the final answer.
    The browser-facing URL is kept only in the request-scoped registry.
    """

    if not text:
        return text

    def _token_text(url: str, caption: str = "") -> str:
        caption = (
            strip_internal_urls_from_caption(caption or default_caption)
            or default_caption
        )
        token = register_image(normalize_image_url(url), caption)
        return f"[图片 {token}｜图注：{caption}]"

    def _replace_markdown(match: re.Match) -> str:
        return _token_text(match.group("url"), match.group("alt") or default_caption)

    def _replace_html(match: re.Match) -> str:
        attrs = match.group("attrs") or ""
        alt_match = re.search(r"\balt\s*=\s*(['\"])(.*?)\1", attrs, re.IGNORECASE)
        caption = alt_match.group(2) if alt_match else default_caption
        return _token_text(match.group("url"), caption)

    result = MARKDOWN_IMAGE_URL_PATTERN.sub(_replace_markdown, text)
    result = HTML_IMAGE_URL_PATTERN.sub(_replace_html, result)
    result = _replace_context_bare_image_urls(result, _token_text)
    result = strip_empty_image_markup(result)
    return result


def strip_unresolved_image_tokens(text: str) -> str:
    if not text:
        return text
    if "IMAGE:" in text:
        text = UNRESOLVED_MARKDOWN_IMAGE_PATTERN.sub("", text)
        text = UNRESOLVED_IMAGE_TOKEN_PATTERN.sub("", text)
    return strip_empty_image_markup(text)


def strip_unregistered_image_tokens(
    text: str,
    *,
    resolve_token: Callable[[str], str | None],
) -> str:
    """Drop only stale image tokens while preserving durable history tokens."""

    if not text or "IMAGE:" not in text:
        return strip_empty_image_markup(text)

    def _registered(raw_token: str) -> bool:
        match = re.fullmatch(r"\[?IMAGE:(\d+)\]?", raw_token.strip(), re.IGNORECASE)
        if not match:
            return False
        return bool(resolve_token(canonical_image_token(match.group(1))))

    def _replace_markdown(match: re.Match) -> str:
        token_match = re.search(
            r"\[?IMAGE:[^\]\)\s]+\]?", match.group(0), re.IGNORECASE
        )
        return (
            match.group(0) if token_match and _registered(token_match.group(0)) else ""
        )

    cleaned = UNRESOLVED_MARKDOWN_IMAGE_PATTERN.sub(_replace_markdown, text)
    cleaned = UNRESOLVED_IMAGE_TOKEN_PATTERN.sub(
        lambda match: match.group(0) if _registered(match.group(0)) else "",
        cleaned,
    )
    return strip_empty_image_markup(cleaned)


def strip_empty_image_markup(text: str) -> str:
    """Remove image markup that would render as ``<img src="">``."""

    if not text:
        return text
    text = EMPTY_MARKDOWN_IMAGE_PATTERN.sub("", text)
    return EMPTY_HTML_IMAGE_PATTERN.sub("", text)


def find_safe_image_stream_end(buf: str) -> int:
    """Keep incomplete image tokens/URLs buffered during streaming."""

    n = len(buf)
    markdown_image_start = _find_incomplete_markdown_image_start(buf)
    if markdown_image_start is not None:
        return markdown_image_start

    for index in range(n - 1, max(-1, n - MAX_IMAGE_TOKEN_LEN - 1), -1):
        if buf[index] == "[":
            suffix = buf[index:]
            if suffix.startswith("[IMAGE:") and "]" not in suffix:
                return index
            if len(suffix) < 8:
                return index
            break

    for marker in ("http://", "https://", "/files/"):
        index = buf.rfind(marker, max(0, n - MAX_IMAGE_URL_CARRY_LEN))
        if index < 0:
            continue
        suffix = buf[index:]
        if not re.search(r"[\s<>'\"\]\)]", suffix):
            return index
    return n


def _find_incomplete_markdown_image_start(buf: str) -> int | None:
    """Return the start index of a trailing incomplete Markdown image."""

    if "![" not in buf:
        return None

    n = len(buf)
    search_from = max(0, n - MAX_IMAGE_URL_CARRY_LEN)
    start = buf.rfind("![", search_from)
    if start < 0:
        return None

    close_alt = buf.find("]", start + 2)
    if close_alt < 0:
        return start

    next_index = close_alt + 1
    if next_index >= n:
        return start
    if buf[next_index] != "(":
        return None

    close_url = buf.find(")", next_index + 1)
    if close_url < 0:
        return start
    return None


def promote_standalone_image_urls(text: str) -> str:
    """Render image-only lines as Markdown images before frontend parsing."""

    if not text or not any(
        marker in text
        for marker in (
            "http://",
            "https://",
            "/files/",
            "/api/v2/files/kb-image?",
            "/api/v2/files/qb-image?",
        )
    ):
        return text

    lines = text.split("\n")
    promoted: list[str] = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            promoted.append(line)
            continue
        if in_fence or not stripped or "![" in stripped or "<img" in stripped:
            promoted.append(line)
            continue

        matches = list(STANDALONE_IMAGE_URL_PATTERN.finditer(stripped))
        if not matches:
            promoted.append(line)
            continue
        covered = " ".join(match.group(0) for match in matches).strip()
        if covered != stripped:
            promoted.append(line)
            continue
        promoted.append(
            "\n\n".join(f"![](<{match.group('url')}>)" for match in matches)
        )

    return "\n".join(promoted)


_PROXY_TABLE_CELL_PATTERNS = (
    (KB_IMAGE_PROXY_URL_PATTERN, "知识库图片"),
    (QB_IMAGE_PROXY_URL_PATTERN, "题图"),
)


def promote_proxy_image_table_cells(text: str) -> str:
    """Turn a proxy URL occupying a Markdown table cell into an actual image."""

    if not text or "|" not in text:
        return text
    if not any(
        marker in text for marker in ("/api/v2/files/kb-image?", "/api/v2/files/qb-image?")
    ):
        return text

    promoted: list[str] = []
    for line in text.split("\n"):
        if (
            "|" not in line
            or "/api/v2/files/kb-image?" not in line
            and "/api/v2/files/qb-image?" not in line
        ):
            promoted.append(line)
            continue
        cells = line.split("|")
        changed = False
        for index, cell in enumerate(cells):
            stripped = cell.strip().strip("`")
            for pattern, alt in _PROXY_TABLE_CELL_PATTERNS:
                match = pattern.fullmatch(stripped)
                if not match:
                    continue
                cells[index] = f" ![{alt}](<{match.group('url')}>) "
                changed = True
                break
        promoted.append("|".join(cells) if changed else line)
    return "\n".join(promoted)


# 兼容旧名（表单元格提升语义未变，只是把 qb 一并纳入）。
promote_kb_image_table_cells = promote_proxy_image_table_cells


def sanitize_answer_image_urls(text: str) -> str:
    """Remove internal image proxy URLs accidentally emitted in answer prose.

    Standalone image URLs remain valid images, while URLs embedded in normal
    prose are removed. Existing Markdown/HTML images and fenced code blocks are
    protected so the browser-facing image reference is not damaged.
    """

    if not text or not any(
        marker in text
        for marker in ("/api/v2/files/kb-image?", "/api/v2/files/qb-image?")
    ):
        return text

    result = _refresh_kb_image_proxy_urls(text)
    result = _refresh_qb_image_proxy_urls(result)
    result = promote_proxy_image_table_cells(result)
    result = promote_standalone_image_urls(result)
    protected: list[str] = []

    def _protect(match: re.Match) -> str:
        placeholder = f"\x00__ANSWER_IMAGE_{len(protected)}__\x00"
        protected.append(match.group(0))
        return placeholder

    result = ANSWER_FENCED_CODE_PATTERN.sub(_protect, result)
    result = ANSWER_MARKDOWN_IMAGE_PATTERN.sub(_protect, result)
    result = ANSWER_HTML_IMAGE_PATTERN.sub(_protect, result)

    def _remove_proxy_url(match: re.Match) -> str:
        url = match.group("url")
        trailing = ""
        while url and url[-1] in ".,;:!?，。；：！？、）":
            trailing = url[-1] + trailing
            url = url[:-1]
        return trailing

    result = BARE_KB_IMAGE_PROXY_URL_PATTERN.sub(_remove_proxy_url, result)
    result = BARE_QB_IMAGE_PROXY_URL_PATTERN.sub(_remove_proxy_url, result)
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"[ \t]+\n", "\n", result)

    for index, markup in enumerate(protected):
        result = result.replace(f"\x00__ANSWER_IMAGE_{index}__\x00", markup)
    return result


def _rewrite_minio_image_urls(text: str) -> str:
    if "/files/" not in text:
        return text

    def _replace(match: re.Match) -> str:
        url = match.group("url")
        if is_browser_image_proxy_url(url):
            return url
        return normalize_image_url(url)

    return MINIO_IMAGE_URL_PATTERN.sub(
        _replace,
        text,
    )


def _refresh_kb_image_proxy_urls(text: str) -> str:
    if not text or "/api/v2/files/kb-image?" not in text:
        return text

    def _replace(match: re.Match) -> str:
        from backend.storage import get_minio_image_proxy_url

        return get_minio_image_proxy_url(match.group("url")) or match.group("url")

    return KB_IMAGE_PROXY_URL_PATTERN.sub(_replace, text)


def _refresh_qb_image_proxy_urls(text: str) -> str:
    if not text or "/api/v2/files/qb-image?" not in text:
        return text

    def _replace(match: re.Match) -> str:
        from backend.application.question_bank.images import (
            build_qb_image_url,
            extract_qb_image_object_name,
        )

        object_name = extract_qb_image_object_name(match.group("url"))
        return build_qb_image_url(object_name) if object_name else match.group("url")

    return QB_IMAGE_PROXY_URL_PATTERN.sub(_replace, text)


def _replace_context_bare_image_urls(
    text: str,
    replace_url: Callable[[str, str], str],
) -> str:
    if not text or not any(
        marker in text
        for marker in ("http://", "https://", "/files/", "/api/v2/files/kb-image?")
    ):
        return text

    def _replace(match: re.Match) -> str:
        url = match.group("url")
        if "/api/v2/files/kb-image?" in url:
            return replace_url(url, "图片")
        return replace_url(url, "图片")

    return CONTEXT_IMAGE_URL_PATTERN.sub(_replace, text)
