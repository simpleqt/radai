"""Plain text parser (附录 E.1)."""

from __future__ import annotations

from backend.rag.ingestion.ids import sha256_text, stable_block_id
from backend.rag.ingestion.models import IngestBlock, ParsedDocument, SourceDocument
from backend.rag.ingestion.parsers.base import BaseParser


class TextParser(BaseParser):
    """Parse UTF-8 / GB18030 text files into a single text block."""

    async def parse(
        self,
        content: bytes,
        source_document: SourceDocument,
    ) -> ParsedDocument:
        text = self._decode(content)
        block_id = stable_block_id(
            source_document.doc_id,
            source_document.commit_version,
            "text",
            page_num=None,
            order_index=0,
        )
        content_hash = sha256_text(text)
        block = IngestBlock(
            block_id=block_id,
            doc_id=source_document.doc_id,
            kb_id=source_document.kb_id,
            kb_code=source_document.kb_code,
            commit_version=source_document.commit_version,
            block_type="text",
            content=text,
            section_path=[],
            page_num=None,
            bbox=None,
            order_index=0,
            source_hash=source_document.source_hash,
            original_file_name=source_document.original_file_name,
            object_storage_uri=source_document.object_storage_uri,
            content_hash=content_hash,
            stable_block_fingerprint=content_hash,
            job_id=source_document.job_id,
        )
        return ParsedDocument(
            source_document=source_document,
            blocks=[block],
            raw_text=text,
            parser_artifacts={"encoding": "auto"},
        )

    @staticmethod
    def _decode(content: bytes) -> str:
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace")
