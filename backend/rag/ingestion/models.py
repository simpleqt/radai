"""Intermediate self-ingestion data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(slots=True)
class SourceDocument:
    doc_id: str
    kb_id: str
    kb_code: str
    file_id: str | None = None
    job_id: str | None = None
    subject: str | None = None

    original_file_name: str = ""
    file_ext: str = ""
    mime_type: str = ""
    file_size: int = 0
    source_hash: str = ""

    object_bucket: str = ""
    object_key: str = ""
    object_storage_uri: str = ""
    local_cache_path: str | None = None
    parser_output_uri: str | None = None

    uploaded_at: datetime | None = None
    received_at: datetime | None = None
    stored_at: datetime | None = None
    ingest_started_at: datetime | None = None

    parser_version: str = "phase1"
    schema_version: str = "generic_v1.0"
    commit_version: int = 1
    doc_status: str = "active"


@dataclass(slots=True)
class IngestBlock:
    block_id: str
    doc_id: str
    kb_id: str
    kb_code: str
    commit_version: int

    block_type: str = "text"
    content: str = ""
    section_path: list[str] = field(default_factory=list)
    page_num: int | None = None
    bbox: list[float] | None = None
    order_index: int = 0

    source_hash: str = ""
    original_file_name: str = ""
    object_storage_uri: str = ""
    parser_output_uri: str | None = None

    content_hash: str = ""
    stable_block_fingerprint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    job_id: str | None = None


@dataclass(slots=True)
class IngestChunk:
    chunk_id: str
    doc_id: str
    chunk_index: int
    content: str
    content_hash: str
    stable_chunk_fingerprint: str
    token_count: int
    block_type: str = "text"
    char_start: int | None = None
    char_end: int | None = None
    file_name: str = ""
    file_path: str | None = None
    source_hash: str | None = None
    document_date: date | None = None
    category_path: str | None = None
    commit_version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    block_id: str | None = None
    job_id: str | None = None
    kb_id: str | None = None
    kb_code: str | None = None
    page_num: int | None = None
    bbox: list[float] | None = None
    section_path: list[str] = field(default_factory=list)
    schema_version: str = "generic_v1.0"
    parser_version: str = "phase1"
    is_active: bool = True
    is_visible: bool = False
    commit_status: str = "staged"
    doc_status: str = "active"


@dataclass(slots=True)
class ParsedDocument:
    source_document: SourceDocument
    blocks: list[IngestBlock]
    raw_text: str = ""
    parser_artifacts: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
