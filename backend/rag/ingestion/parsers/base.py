"""Base parser interface for self-ingestion pipeline (§7.2)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.rag.ingestion.models import ParsedDocument, SourceDocument


class UnsupportedFileType(RuntimeError):
    """Raised when no parser can handle the given file type."""


class BaseParser(ABC):
    """All parsers produce a ``ParsedDocument`` with typed ``IngestBlock`` list.

    Parsers never write to Milvus/Nebula/Redis -- they only produce
    intermediate representations for downstream BlockBuilder / Chunker.
    """

    @abstractmethod
    async def parse(
        self,
        content: bytes,
        source_document: SourceDocument,
    ) -> ParsedDocument:
        """Parse raw file bytes into blocks.

        Args:
            content: Raw file bytes.
            source_document: Metadata about the source file (doc_id, kb_id, etc.)

        Returns:
            A ``ParsedDocument`` containing typed ``IngestBlock`` items.
        """
