"""PDF parser via MinerU (§7.3, 附录 E.4) -- content_list -> multi-type blocks."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.config.loggers import get_logger
from backend.rag.ingestion.concurrency import mineru_concurrency, semaphore
from backend.rag.ingestion.ids import sha256_text, stable_block_id
from backend.rag.ingestion.models import IngestBlock, ParsedDocument, SourceDocument
from backend.rag.ingestion.parsers.base import BaseParser

logger = get_logger(__name__)

_NUMBERED_HEADING_RE = re.compile(
    r"^(?P<number>\d+(?:\.\d+){0,5})\s+(?P<title>\S.{0,120})$"
)
_CN_HEADING_RE = re.compile(
    r"^第(?P<number>[一二三四五六七八九十百千万\d]+)(?P<unit>[章节篇])\s*(?P<title>\S.{0,120})?$"
)
_LOOSE_HEADING_RE = re.compile(
    r"^(?P<number>[一二三四五六七八九十]+)[、.．]\s*(?P<title>\S.{0,120})$"
)
_BIBLIOGRAPHY_ENTRY_RE = re.compile(
    r"^\d{1,4}\s+"
    r"(?:[A-Z]\.\s*){1,4}"
    r"[A-Z][A-Za-z'`-]+"
    r".*(?:,\s*(?:19|20)\d{2}|(?:19|20)\d{2}\.)"
)


class MinerUPdfParser(BaseParser):
    """Parse PDF via MinerU API, extracting content_list into typed blocks.

    Phase-3 upgrade: instead of only returning raw markdown, we now request
    ``content_list`` from MinerU which contains typed items (text, image,
    table, equation) with page_num / bbox metadata.
    """

    async def parse(
        self,
        content: bytes | Path,
        source_document: SourceDocument,
    ) -> ParsedDocument:
        from backend.core.tools.ocr.mineru_ocr import MinerUOCR, OCRConfig

        owns_cache_path = not isinstance(content, Path)
        if owns_cache_path:
            cache_dir = (
                Path(__file__).resolve().parents[3]
                / "data"
                / "cache"
                / "self_ingestion"
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            suffix = Path(source_document.original_file_name or "").suffix or ".pdf"
            safe_name = "".join(
                ch if ch.isalnum() or ch in ("-", "_") else "_"
                for ch in Path(source_document.original_file_name or "document").stem
            )
            file_id = source_document.file_id or source_document.doc_id
            cache_path = cache_dir / f"{file_id}_{safe_name}{suffix}"
            await asyncio.to_thread(cache_path.write_bytes, content)
        else:
            cache_path = content

        config = OCRConfig(
            backend=settings.mineru_backend,
            lang=settings.mineru_lang,
            method=settings.mineru_method,
            formula_enable=settings.mineru_formula_enable,
            table_enable=settings.mineru_table_enable,
        )
        mineru = MinerUOCR(api_url=settings.mineru_api_url, config=config)
        warnings: list[str] = []
        artifacts: dict[str, Any] = {"parser": "mineru_pdf_v2"}

        try:
            logger.info("[self-ingestion] MinerU PDF 解析: %s", source_document.original_file_name)
            mineru_sem = await semaphore("self_ingestion_mineru", mineru_concurrency())
            mineru_timeout = int(
                getattr(settings, "self_ingestion_mineru_timeout", 3600) or 3600
            )
            async with mineru_sem:
                task_id = await mineru.submit_task_async(file_path=str(cache_path), config=config)
                result = await mineru.wait_for_completion_async(
                    task_id=task_id,
                    timeout=mineru_timeout,
                    poll_interval=5,
                )
            if result.status.value != "completed":
                error = result.error_message or result.status.value
                raise RuntimeError(f"MinerU 解析失败: {error}")

            md_result = await mineru.get_task_data_async(
                task_id,
                include_fields=["md", "markdown", "content_list", "images"],
                upload_images=True,
            )
            artifacts["task_id"] = task_id
            artifacts["md_url"] = md_result.md_url
            if md_result.images:
                artifacts["mineru_images"] = md_result.images
            raw_markdown = md_result.markdown or ""
            content_list = md_result.content_list

            if content_list:
                blocks = self._content_list_to_blocks(content_list, source_document, warnings)
                artifacts["source"] = "content_list"
            elif raw_markdown.strip():
                blocks = await self._markdown_fallback(raw_markdown, source_document)
                artifacts["source"] = "markdown_fallback"
                warnings.append("content_list 不可用，回退为纯 markdown 拆分")
            else:
                raise RuntimeError("MinerU 返回的 markdown 和 content_list 均为空")

            return ParsedDocument(
                source_document=source_document,
                blocks=blocks,
                raw_text=raw_markdown,
                parser_artifacts=artifacts,
                warnings=warnings,
            )
        finally:
            try:
                await mineru.close()
            finally:
                if owns_cache_path:
                    await asyncio.to_thread(cache_path.unlink, missing_ok=True)

    def _content_list_to_blocks(
        self,
        content_list: list[Any],
        sd: SourceDocument,
        warnings: list[str],
    ) -> list[IngestBlock]:
        blocks: list[IngestBlock] = []
        section_path: list[str] = []
        order = 0

        for item in content_list:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "text")).lower()
            page_num = item.get("page_num")
            if page_num is None:
                page_idx = item.get("page_idx")
                page_num = page_idx + 1 if isinstance(page_idx, int) else page_idx
            bbox = item.get("bbox")
            if isinstance(bbox, (list, tuple)):
                bbox = [float(v) for v in bbox]
            else:
                bbox = None

            if item_type in ("text", "paragraph"):
                text = str(item.get("text", "") or item.get("content", "")).strip()
                if not text:
                    continue
                heading = _detect_heading(text)
                if heading:
                    level, title = heading
                    section_path = section_path[: max(level - 1, 0)]
                    section_path.append(title)
                blocks.append(self._make_block(
                    sd, "text", text, order,
                    section_path=list(section_path),
                    page_num=page_num, bbox=bbox,
                ))
                order += 1

            elif item_type in ("image", "figure"):
                img_path = item.get("img_path") or item.get("image_path") or ""
                caption = str(item.get("caption", "") or item.get("text", "")).strip()
                content_text = f"[图片] {caption}".strip() if caption else f"[图片] page {page_num or '?'}"
                blocks.append(self._make_block(
                    sd, "image", content_text, order,
                    section_path=list(section_path),
                    page_num=page_num, bbox=bbox,
                    metadata={
                        "img_path": img_path,
                        "caption": caption,
                    },
                ))
                order += 1

            elif item_type in ("table",):
                table_body = item.get("table_body") or item.get("text") or item.get("content") or ""
                if isinstance(table_body, list):
                    table_body = self._list_table_to_markdown(table_body)
                table_body = str(table_body).strip()
                if not table_body:
                    continue
                caption = str(item.get("caption", "")).strip()
                content_text = f"{caption}\n\n{table_body}".strip() if caption else table_body
                blocks.append(self._make_block(
                    sd, "table", content_text, order,
                    section_path=list(section_path),
                    page_num=page_num, bbox=bbox,
                    metadata={"caption": caption},
                ))
                order += 1

            elif item_type in ("equation", "formula"):
                latex = str(item.get("latex", "") or item.get("text", "") or item.get("content", "")).strip()
                if not latex:
                    continue
                blocks.append(self._make_block(
                    sd, "formula", latex, order,
                    section_path=list(section_path),
                    page_num=page_num, bbox=bbox,
                    metadata={"latex": latex, "display": "block"},
                ))
                order += 1

            else:
                text = str(item.get("text", "") or item.get("content", "")).strip()
                if text:
                    blocks.append(self._make_block(
                        sd, "text", text, order,
                        section_path=list(section_path),
                        page_num=page_num, bbox=bbox,
                    ))
                    order += 1

        if not blocks:
            warnings.append("content_list 解析出 0 个 block")
        return _inject_surrounding_context(blocks)

    async def _markdown_fallback(
        self,
        markdown: str,
        sd: SourceDocument,
    ) -> list[IngestBlock]:
        """Fallback to the markdown parser while preserving headings and rich blocks."""
        from backend.rag.ingestion.parsers.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        parsed = await parser.parse(markdown.encode("utf-8"), sd)
        return parsed.blocks

    @staticmethod
    def _list_table_to_markdown(table_body: list) -> str:
        if not table_body:
            return ""
        rows = []
        for row in table_body:
            if isinstance(row, (list, tuple)):
                rows.append("| " + " | ".join(str(c) for c in row) + " |")
            else:
                rows.append(str(row))
        if len(rows) >= 1:
            width = rows[0].count("|") - 1
            if width > 0:
                sep = "| " + " | ".join(["---"] * width) + " |"
                rows.insert(1, sep)
        return "\n".join(rows)

    def _make_block(
        self,
        sd: SourceDocument,
        block_type: str,
        content: str,
        order_index: int,
        section_path: list[str] | None = None,
        page_num: int | None = None,
        bbox: list[float] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestBlock:
        section_path = list(section_path or [])
        block_id = stable_block_id(
            sd.doc_id, sd.commit_version, block_type,
            section_path=section_path,
            page_num=page_num,
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
            section_path=section_path,
            page_num=page_num,
            bbox=bbox,
            order_index=order_index,
            source_hash=sd.source_hash,
            original_file_name=sd.original_file_name,
            object_storage_uri=sd.object_storage_uri,
            content_hash=sha256_text(content),
            stable_block_fingerprint=sha256_text(f"{block_type}:{content}"),
            metadata=metadata or {},
            job_id=sd.job_id,
        )


class MinerUContentListParser(MinerUPdfParser):
    """Parse a previously generated MinerU ``*_content_list.json`` payload.

    Legacy knowledge bases often keep the MinerU Markdown URL in
    ``kb_documents.file_path``. The sibling JSON preserves block type, page,
    and bounding-box metadata and avoids submitting the PDF to MinerU again.
    """

    async def parse(
        self,
        content: bytes,
        source_document: SourceDocument,
    ) -> ParsedDocument:
        try:
            payload = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("MinerU content_list JSON 解析失败") from exc

        if isinstance(payload, dict):
            content_list = payload.get("content_list") or payload.get("data")
        else:
            content_list = payload
        if not isinstance(content_list, list):
            raise RuntimeError("MinerU content_list JSON 顶层必须是数组或包含 content_list 数组")

        warnings: list[str] = []
        blocks = self._content_list_to_blocks(content_list, source_document, warnings)
        if not blocks:
            raise RuntimeError("MinerU content_list JSON 未解析出有效 block")

        raw_text = "\n\n".join(
            block.content for block in blocks if block.block_type == "text" and block.content
        )
        return ParsedDocument(
            source_document=source_document,
            blocks=blocks,
            raw_text=raw_text,
            parser_artifacts={
                "parser": "mineru_content_list_v1",
                "source": "legacy_content_list",
                "content_list_url": source_document.parser_output_uri,
                "md_url": source_document.object_storage_uri,
            },
            warnings=warnings,
        )


def _detect_heading(text: str) -> tuple[int, str] | None:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    if not normalized or len(normalized) > 140:
        return None
    if _looks_like_bibliography_entry(normalized):
        return None
    match = _NUMBERED_HEADING_RE.match(normalized)
    if match:
        number = match.group("number")
        title = f"{number} {match.group('title').strip()}"
        return number.count(".") + 1, title
    match = _CN_HEADING_RE.match(normalized)
    if match:
        unit = match.group("unit")
        title = normalized
        level = 1 if unit in {"章", "篇"} else 2
        return level, title
    match = _LOOSE_HEADING_RE.match(normalized)
    if match:
        return 2, normalized
    return None


def _looks_like_bibliography_entry(text: str) -> bool:
    """Avoid treating numbered references as section headings."""
    return bool(_BIBLIOGRAPHY_ENTRY_RE.match(text))


def _inject_surrounding_context(blocks: list[IngestBlock], *, window: int = 1) -> list[IngestBlock]:
    """Attach nearby text to multimodal blocks for summary/extraction context."""
    for index, block in enumerate(blocks):
        if block.block_type not in {"image", "table", "formula"}:
            continue
        previous_text: list[str] = []
        next_text: list[str] = []
        cursor = index - 1
        while cursor >= 0 and len(previous_text) < window:
            candidate = blocks[cursor]
            if candidate.block_type == "text":
                previous_text.append(_compact_context_text(candidate.content))
            cursor -= 1
        cursor = index + 1
        while cursor < len(blocks) and len(next_text) < window:
            candidate = blocks[cursor]
            if candidate.block_type == "text":
                next_text.append(_compact_context_text(candidate.content))
            cursor += 1
        context = "\n".join(
            part for part in [*reversed(previous_text), *next_text] if part
        ).strip()
        if context:
            metadata = dict(block.metadata or {})
            metadata.setdefault("surrounding_context", context[:1000])
            block.metadata = metadata
    return blocks


def _compact_context_text(text: str, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())[:limit]
