"""Helpers for parsing stored multimodal chunk text."""

from __future__ import annotations

import re
from typing import Any


IMAGE_BLOCK_RE = re.compile(
    r"Image Content Analysis:\s*(?P<body>.*?)(?=\n\s*Image Content Analysis:|\Z)",
    re.IGNORECASE | re.DOTALL,
)

FIELD_LINE_RE = re.compile(
    r"(?im)^\s*(?P<name>"
    r"Image Path|Captions?|Caption|Footnotes?|Footnote|Visual Analysis|Visual Summary|"
    r"Media Ref|Media Title|Media Type|OCR Text|Nearby Text Before|Nearby Text After|"
    r"Nearby Text|Source Doc Name|Source Document|Page Num|Page Number"
    r")\s*:\s*"
)


def _normalize_field_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def parse_labeled_fields(text: str) -> dict[str, str]:
    """Parse loose `Label: value` blocks while allowing multiline values."""

    body = text or ""
    matches = list(FIELD_LINE_RE.finditer(body))
    fields: dict[str, str] = {}
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        value = body[start:end].strip()
        fields[_normalize_field_name(match.group("name"))] = value
    return fields


def first_field(fields: dict[str, str], *names: str) -> str:
    for name in names:
        value = fields.get(_normalize_field_name(name))
        if value:
            return value.strip()
    return ""


def iter_image_blocks(text: str) -> list[dict[str, Any]]:
    """Return parsed image blocks from stored chunk content.

    Supports both legacy chunks, where `Captions` is followed immediately by
    `Footnotes`, and newer self-ingestion chunks that include media metadata,
    OCR text, and nearby text between the core fields.
    """

    blocks: list[dict[str, Any]] = []
    for match in IMAGE_BLOCK_RE.finditer(text or ""):
        body = match.group("body") or ""
        fields = parse_labeled_fields(body)
        image_path = first_field(fields, "Image Path")
        if not image_path:
            continue
        caption = first_field(fields, "Captions", "Caption", "Media Title", "Media Ref")
        media_ref = first_field(fields, "Media Ref")
        media_title = first_field(fields, "Media Title")
        visual_analysis = first_field(fields, "Visual Analysis", "Visual Summary")
        ocr_text = first_field(fields, "OCR Text")
        nearby_before = first_field(fields, "Nearby Text Before")
        nearby_after = first_field(fields, "Nearby Text After")
        nearby_text = first_field(fields, "Nearby Text")
        if not visual_analysis:
            visual_analysis = ocr_text or nearby_text or nearby_before or nearby_after
        blocks.append(
            {
                "image_path": image_path,
                "caption": caption,
                "footnote": first_field(fields, "Footnotes", "Footnote"),
                "visual_analysis": visual_analysis,
                "media_ref": media_ref,
                "media_title": media_title,
                "ocr_text": ocr_text,
                "nearby_text_before": nearby_before,
                "nearby_text_after": nearby_after,
                "nearby_text": nearby_text,
                "fields": fields,
            }
        )
    return blocks
