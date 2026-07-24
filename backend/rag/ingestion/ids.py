"""Stable identifiers for the self-ingestion pipeline."""

from __future__ import annotations

import hashlib


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sha1_prefix(seed: str, length: int = 40) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:length]


def stable_doc_id(
    kb_id: str,
    source_hash: str,
    file_name: str = "",
    logical_path: str | None = None,
) -> str:
    """Return a stable logical document id.

    The document id must not include the content hash, otherwise an edited file
    becomes a different document and old chunks cannot be superseded by version.
    The source hash is only a fallback for callers that do not have a logical
    path yet.
    """
    identity = (logical_path or file_name or source_hash or "").strip()
    seed = f"{kb_id}:{identity}"
    return "doc-" + _sha1_prefix(seed, 32)


def stable_block_id(
    doc_id: str,
    commit_version: int,
    block_type: str,
    section_path: list[str] | None = None,
    page_num: int | None = None,
    order_index: int = 0,
) -> str:
    path_str = "/".join(section_path) if section_path else ""
    seed = f"{doc_id}:{commit_version}:{block_type}:{path_str}:{page_num or 0}:{order_index}"
    return "blk-" + _sha1_prefix(seed, 32)


def stable_chunk_fingerprint(block_type: str, text: str) -> str:
    normalized = " ".join((text or "").split())
    return sha256_text(f"{block_type}:{normalized}")


def stable_chunk_id(
    doc_id: str,
    commit_version: int,
    chunk_index: int,
    content_hash: str,
) -> str:
    seed = f"{doc_id}:{commit_version}:{chunk_index}:{content_hash}"
    return "chk-" + _sha1_prefix(seed)


def stable_entity_id(kb_code: str, entity_type: str, canonical_name: str) -> str:
    normalized = " ".join((canonical_name or "").strip().split()).lower()
    seed = f"{kb_code}:{entity_type}:{normalized}"
    return "ent-" + _sha1_prefix(seed, 32)


def stable_occurrence_id(
    canonical_id: str, doc_id: str, block_id: str, surface_name: str,
) -> str:
    seed = f"{canonical_id}:{doc_id}:{block_id}:{surface_name}"
    return "eoc-" + _sha1_prefix(seed, 32)


def stable_relation_id(src_id: str, relation_type: str, dst_id: str) -> str:
    seed = f"{src_id}:{relation_type}:{dst_id}"
    return "rel-" + _sha1_prefix(seed, 32)


def stable_relation_evidence_id(
    canonical_relation_id: str, doc_id: str, block_id: str,
) -> str:
    seed = f"{canonical_relation_id}:{doc_id}:{block_id}"
    return "rev-" + _sha1_prefix(seed, 32)
