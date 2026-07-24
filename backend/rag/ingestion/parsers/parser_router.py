"""Parser router (§7.1) -- select the right parser by file extension / MIME."""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.rag.ingestion.models import ParsedDocument, SourceDocument
from backend.rag.ingestion.parsers.base import BaseParser, UnsupportedFileType

_TEXT_EXTENSIONS = {".txt", ".text"}
_MARKDOWN_EXTENSIONS = {".md", ".markdown"}
_PDF_EXTENSIONS = {".pdf"}
_DOCX_EXTENSIONS = {".docx"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}

_TEXT_MIMES = {"text/plain"}
_MARKDOWN_MIMES = {"text/markdown", "text/x-markdown"}
_PDF_MIMES = {"application/pdf"}
_DOCX_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff"}


class ParserRouter:
    """Route a file to the appropriate parser based on extension / MIME type."""

    def resolve(self, file_name: str, mime_type: str = "") -> BaseParser:
        ext = Path(file_name or "").suffix.lower()
        mime = (mime_type or "").lower().strip()

        if ext in _PDF_EXTENSIONS or mime in _PDF_MIMES:
            from backend.rag.ingestion.parsers.mineru_pdf_parser import MinerUPdfParser
            return MinerUPdfParser()

        if ext in _MARKDOWN_EXTENSIONS or mime in _MARKDOWN_MIMES:
            from backend.rag.ingestion.parsers.markdown_parser import MarkdownParser
            return MarkdownParser()

        if ext in _TEXT_EXTENSIONS or mime in _TEXT_MIMES:
            from backend.rag.ingestion.parsers.text_parser import TextParser
            return TextParser()

        if ext in _DOCX_EXTENSIONS or mime in _DOCX_MIMES:
            from backend.rag.ingestion.parsers.docx_parser import DocxParser
            return DocxParser()

        if ext in _IMAGE_EXTENSIONS or mime in _IMAGE_MIMES:
            from backend.rag.ingestion.parsers.image_parser import ImageParser
            return ImageParser()

        raise UnsupportedFileType(
            f"No parser for ext={ext!r}, mime={mime!r}, file={file_name!r}"
        )

    async def parse(
        self,
        content: bytes | Path,
        source_document: SourceDocument,
    ) -> ParsedDocument:
        parser_output_uri = (source_document.parser_output_uri or "").lower()
        if parser_output_uri.endswith("_content_list.json"):
            from backend.rag.ingestion.parsers.mineru_pdf_parser import (
                MinerUContentListParser,
            )

            if isinstance(content, Path):
                content = await asyncio.to_thread(content.read_bytes)
            return await MinerUContentListParser().parse(content, source_document)
        parser = self.resolve(
            source_document.original_file_name,
            source_document.mime_type,
        )
        return await parser.parse(content, source_document)
