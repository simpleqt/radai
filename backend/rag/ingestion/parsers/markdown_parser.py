"""Markdown parser (§7.4) -- extract text / image / table / formula blocks."""

from __future__ import annotations

import re
from typing import Any

from backend.rag.ingestion.ids import sha256_text, stable_block_id
from backend.rag.ingestion.models import IngestBlock, ParsedDocument, SourceDocument
from backend.rag.ingestion.parsers.base import BaseParser

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HTML_IMG_RE = re.compile(r"<img\b[^>]*src=[\"']([^\"']+)[\"'][^>]*/?>", re.IGNORECASE)
_TABLE_START_RE = re.compile(r"^\|.+\|$", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\|[\s:\-|]+\|$")
_LATEX_BLOCK_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_LATEX_DISPLAY_RE = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)


class MarkdownParser(BaseParser):
    """Parse markdown into multi-type IngestBlock list.

    Supports: heading hierarchy (section_path), inline/block images,
    markdown tables, LaTeX block equations.
    """

    async def parse(
        self,
        content: bytes,
        source_document: SourceDocument,
    ) -> ParsedDocument:
        text = content.decode("utf-8", errors="replace")
        blocks = self._split_blocks(text, source_document)
        return ParsedDocument(
            source_document=source_document,
            blocks=blocks,
            raw_text=text,
            parser_artifacts={"parser": "markdown_v2"},
        )

    def _split_blocks(
        self,
        text: str,
        sd: SourceDocument,
    ) -> list[IngestBlock]:
        lines = text.split("\n")
        blocks: list[IngestBlock] = []
        section_path: list[str] = []
        heading_levels: list[int] = []
        buffer: list[str] = []
        order = 0

        def _flush_text_buffer() -> None:
            nonlocal order
            body = "\n".join(buffer).strip()
            if not body:
                return
            remaining = body
            while remaining:
                remaining, order = self._extract_inline_elements(
                    remaining, sd, section_path, order, blocks,
                )

        i = 0
        while i < len(lines):
            line = lines[i]

            heading_match = _HEADING_RE.match(line)
            if heading_match:
                _flush_text_buffer()
                buffer.clear()
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                while heading_levels and heading_levels[-1] >= level:
                    heading_levels.pop()
                    if section_path:
                        section_path.pop()
                heading_levels.append(level)
                section_path.append(title)
                i += 1
                continue

            if _TABLE_START_RE.match(line):
                _flush_text_buffer()
                buffer.clear()
                table_lines = [line]
                i += 1
                while i < len(lines) and _TABLE_START_RE.match(lines[i]):
                    table_lines.append(lines[i])
                    i += 1
                if len(table_lines) >= 2:
                    blocks.append(self._make_block(
                        sd, "table", "\n".join(table_lines),
                        list(section_path), order,
                    ))
                    order += 1
                else:
                    buffer.extend(table_lines)
                continue

            latex_block = _LATEX_BLOCK_RE.search(line) or _LATEX_DISPLAY_RE.search(line)
            if latex_block and line.strip().startswith(("$$", "\\[")):
                _flush_text_buffer()
                buffer.clear()
                formula_lines = [line]
                if line.strip() in ("$$", "\\["):
                    i += 1
                    while i < len(lines):
                        formula_lines.append(lines[i])
                        if lines[i].strip() in ("$$", "\\]"):
                            break
                        i += 1
                full = "\n".join(formula_lines)
                latex = _LATEX_BLOCK_RE.search(full) or _LATEX_DISPLAY_RE.search(full)
                content_text = latex.group(1).strip() if latex else full
                blocks.append(self._make_block(
                    sd, "formula", content_text,
                    list(section_path), order,
                    metadata={"latex": content_text, "display": "block"},
                ))
                order += 1
                i += 1
                continue

            buffer.append(line)
            i += 1

        _flush_text_buffer()
        buffer.clear()
        return blocks

    def _extract_inline_elements(
        self,
        text: str,
        sd: SourceDocument,
        section_path: list[str],
        order: int,
        blocks: list[IngestBlock],
    ) -> tuple[str, int]:
        """Extract images and formulas from text, flush remaining as text block."""
        image_match = _IMAGE_RE.search(text) or _HTML_IMG_RE.search(text)
        formula_match = _LATEX_BLOCK_RE.search(text) or _LATEX_DISPLAY_RE.search(text)

        first_match = None
        match_type = None
        for m, mtype in [(image_match, "image"), (formula_match, "formula")]:
            if m and (first_match is None or m.start() < first_match.start()):
                first_match = m
                match_type = mtype

        if first_match is None:
            if text.strip():
                blocks.append(self._make_block(
                    sd, "text", text.strip(), list(section_path), order,
                ))
                order += 1
            return "", order

        before = text[:first_match.start()].strip()
        after = text[first_match.end():].strip()

        if before:
            blocks.append(self._make_block(
                sd, "text", before, list(section_path), order,
            ))
            order += 1

        if match_type == "image":
            if isinstance(first_match, re.Match) and first_match.lastindex and first_match.lastindex >= 2:
                alt_text = first_match.group(1)
                img_path = first_match.group(2)
            else:
                alt_text = ""
                img_path = first_match.group(1) if first_match.lastindex else ""
            blocks.append(self._make_block(
                sd, "image", f"[图片] {alt_text}".strip(),
                list(section_path), order,
                metadata={"img_path": img_path, "alt_text": alt_text, "caption": alt_text},
            ))
            order += 1
        elif match_type == "formula":
            latex = first_match.group(1).strip()
            blocks.append(self._make_block(
                sd, "formula", latex,
                list(section_path), order,
                metadata={"latex": latex, "display": "inline"},
            ))
            order += 1

        return after, order

    def _make_block(
        self,
        sd: SourceDocument,
        block_type: str,
        content: str,
        section_path: list[str],
        order_index: int,
        metadata: dict[str, Any] | None = None,
    ) -> IngestBlock:
        block_id = stable_block_id(
            sd.doc_id, sd.commit_version, block_type,
            section_path=section_path,
            order_index=order_index,
        )
        return IngestBlock(
            block_id=block_id,
            doc_id=sd.doc_id,
            kb_id=sd.kb_id,
            kb_code=sd.kb_code,
            commit_version=sd.commit_version,
            block_type=block_type,
            content=content,
            section_path=list(section_path),
            order_index=order_index,
            source_hash=sd.source_hash,
            original_file_name=sd.original_file_name,
            object_storage_uri=sd.object_storage_uri,
            content_hash=sha256_text(content),
            stable_block_fingerprint=sha256_text(f"{block_type}:{content}"),
            metadata=metadata or {},
            job_id=sd.job_id,
        )
