"""HTML PPT 资产层:骨架模板/主题/资产目录常量、模板与主题列举、安全文件名。(拆自 html_ppt_tool.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from functools import lru_cache
from pathlib import Path
from typing import Any
import re

logger = get_logger(__name__)


BACKEND_ROOT = Path(__file__).resolve().parents[3]


SKILL_ROOT = BACKEND_ROOT / "skills" / "public" / "html-ppt"


FULL_DECKS_DIR = SKILL_ROOT / "templates" / "full-decks"


GENERIC_DECK_TEMPLATE = SKILL_ROOT / "templates" / "deck.html"


ASSETS_DIR = SKILL_ROOT / "assets"


THEMES_DIR = SKILL_ROOT / "assets" / "themes"


ASSETS_URL_PREFIX = "/static/html-ppt-assets"


HTML_MIME_TYPE = "text/html; charset=utf-8"


def _list_available_templates() -> list[str]:
    return sorted(
        path.name
        for path in FULL_DECKS_DIR.iterdir()
        if path.is_dir() and (path / "index.html").exists()
    )


def _list_available_themes() -> list[str]:
    return sorted(path.stem for path in THEMES_DIR.glob("*.css"))


@lru_cache(maxsize=32)
def _read_text_file(file_path: Path) -> str:
    return file_path.read_text(encoding="utf-8").strip()


def _safe_html_filename(filename: str | None, title: str) -> str:
    raw_name = _clean_tool_text_arg(filename) or _clean_tool_text_arg(title) or "html-ppt"
    raw_name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", raw_name)
    raw_name = re.sub(r"[\s-]+", "-", raw_name).strip(".-")
    if not raw_name:
        raw_name = "html-ppt"
    if raw_name.lower().endswith(".html.html"):
        raw_name = raw_name[:-5]
    if not raw_name.lower().endswith(".html"):
        raw_name = f"{raw_name}.html"
    return raw_name


def _clean_tool_text_arg(raw: Any) -> str:
    """Clean model-emitted free-text tool args without truncating content."""
    text = str(raw or "").strip().strip("\"'`")
    if not text:
        return ""
    text = re.sub(r"</?parameter[^>]*(?:>|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?tool_call[^>]*(?:>|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\r\n?", "\n", text)
    return text.strip()
