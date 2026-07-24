"""Schema-aware entity/relation extractor — text LLM + image VLM, model required."""

from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any

from backend.config import settings
from backend.config.loggers import get_logger
from backend.rag.ingestion.concurrency import (
    generation_model_concurrency,
    ingest_file_concurrency,
    llm_batch_concurrency,
    semaphore,
)
from backend.rag.ingestion.extraction.json_repair import loads_json_object
from backend.rag.ingestion.extraction.orphan_linker import link_orphan_entities
from backend.rag.ingestion.extraction.payload_mapper import payload_to_extraction
from backend.rag.ingestion.extraction.quality_gate import apply_quality_gate
from backend.rag.ingestion.extraction.simple_extractor import (
    ExtractedEntity,
    ExtractedRelation,
    ExtractionResult,
)
from backend.rag.ingestion.extraction.vision_extractor import (
    ImageAsset,
    extract_image_chunks,
)
from backend.rag.ingestion.models import IngestBlock, IngestChunk
from backend.rag.ingestion.normalization import normalize_extraction
from backend.rag.ingestion.schema.schema_loader import (
    SchemaDefinition,
    few_shot_examples_for_schema,
    format_schema_for_prompt,
    load_schema_for_subject,
)

logger = get_logger(__name__)

_TEXT_BLOCK_TYPES = frozenset({"text", "table", "formula", ""})
_IMAGE_BLOCK_TYPES = frozenset({"image"})


class SelfExtractionError(RuntimeError):
    """Raised when model-based extraction is required but unavailable."""


def _exc_summary(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or repr(exc)


def _message_content(response: Any) -> str:
    raw = getattr(response, "content", response)
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(raw or "")


def _schema_prompt(schema: SchemaDefinition, chunks: list[IngestChunk]) -> str:
    chunk_payload = [
        {
            "chunk_id": chunk.chunk_id,
            "doc_id": chunk.doc_id,
            "block_type": chunk.block_type or "text",
            "section_path": list(chunk.section_path or []),
            "page_num": chunk.page_num,
            "content": chunk.content[:1800],
        }
        for chunk in chunks
    ]
    return (
        "你是知识库实体关系抽取器。只输出 JSON，不要解释。\n"
        "必须遵守 schema；实体类型和关系类型必须使用 schema id，不要输出中文标签；"
        "不确定的事实降低 confidence，不要编造。\n"
        "硬性要求：\n"
        "1. entities 中每个实体必须在 relations 中至少出现一次（source 或 target）。\n"
        "2. relations 的 source/target 必须与 entities.name 完全一致，禁止简称或别名。\n"
        "3. relations 必须遵守 source_types/target_types 方向；语义不明但同 chunk 共现时用 co_occurs。\n"
        "4. 每个实体和关系都必须包含真实存在的 chunk_ids，禁止使用示例 chunk_id。\n\n"
        f"{format_schema_for_prompt(schema)}\n\n"
        f"few_shot_examples:\n{json.dumps(few_shot_examples_for_schema(schema), ensure_ascii=False)}\n\n"
        "输出格式：\n"
        "{\n"
        '  "entities": [{"name": "...", "entity_type": "...", "confidence": 0-100, "chunk_ids": ["..."]}],\n'
        '  "relations": [{"source": "实体名", "target": "实体名", "relation_type": "...", "confidence": 0-100, "chunk_ids": ["..."]}]\n'
        "}\n\n"
        f"chunks:\n{json.dumps(chunk_payload, ensure_ascii=False)}"
    )


def _resolve_llm_model_name() -> str | None:
    explicit = str(getattr(settings, "self_extraction_llm_model", "") or "").strip()
    if explicit:
        return explicit
    provider = str(getattr(settings, "ingestion_llm_provider", "") or "").strip()
    if not provider:
        return None
    model_attr = f"{provider}_model"
    return str(getattr(settings, model_attr, "") or "").strip() or None


def _require_model_extraction() -> bool:
    mode = str(getattr(settings, "self_extraction_mode", "llm") or "llm").strip().lower()
    if mode == "simple":
        return False
    return bool(getattr(settings, "self_extraction_require_llm", True))


def _extraction_mode() -> str:
    return str(getattr(settings, "self_extraction_mode", "llm") or "llm").strip().lower()


def _per_doc_batch_window(default: int = 4) -> int:
    raw = (
        os.getenv("SELF_EXTRACTION_PER_DOC_BATCH_WINDOW")
        or getattr(settings, "self_extraction_per_doc_batch_window", None)
        or default
    )
    try:
        return max(int(raw), 1)
    except (TypeError, ValueError):
        return default


def _adaptive_batch_size(chunk_count: int, configured_batch_size: int) -> int:
    """Keep enough batches ready to share the global LLM concurrency budget.

    ``configured_batch_size`` remains the upper bound.  For shorter documents
    the batch is reduced so four active files do not collapse into only a
    handful of model requests.
    """
    configured = max(int(configured_batch_size or 1), 1)
    enabled = str(
        os.getenv("SELF_EXTRACTION_ADAPTIVE_BATCHING", "0") or "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled or chunk_count <= 0:
        return configured

    configured_target = os.getenv("SELF_EXTRACTION_TARGET_BATCHES_PER_DOC")
    try:
        target_batches = int(configured_target) if configured_target else 0
    except (TypeError, ValueError):
        target_batches = 0
    if target_batches <= 0:
        file_slots = max(ingest_file_concurrency(), 1)
        target_batches = max(
            (llm_batch_concurrency() + file_slots - 1) // file_slots,
            1,
        )

    # Floor division deliberately produces at least target_batches whenever
    # the document has enough chunks.  The global semaphore remains the hard
    # deployment-wide ceiling.
    adaptive = max(int(chunk_count) // target_batches, 1)
    return min(configured, adaptive)


def _apply_orphan_linking(
    extraction: ExtractionResult,
    schema: SchemaDefinition,
) -> tuple[ExtractionResult, dict]:
    if not getattr(settings, "self_extraction_orphan_linking", True):
        return extraction, {"enabled": False}
    max_links = max(int(getattr(settings, "self_extraction_orphan_max_links", 3) or 3), 1)
    typed_first = bool(getattr(settings, "self_extraction_orphan_typed_first", True))
    linked, audit = link_orphan_entities(
        extraction,
        schema,
        max_links_per_orphan=max_links,
        typed_first=typed_first,
    )
    audit["enabled"] = True
    return linked, audit


def _merge_extractions(*parts: ExtractionResult) -> ExtractionResult:
    entities: dict[str, ExtractedEntity] = {}
    relations: dict[str, ExtractedRelation] = {}
    for part in parts:
        entities.update(part.entities)
        relations.update(part.relations)
    return ExtractionResult(entities=entities, relations=relations)


async def _llm_extract_text_chunks(
    kb_id: str,
    kb_code: str,
    chunks: list[IngestChunk],
    schema: SchemaDefinition,
) -> tuple[ExtractionResult, dict]:
    model_name = _resolve_llm_model_name()
    if not model_name:
        raise SelfExtractionError(
            "文本实体/关系抽取需要配置 SELF_EXTRACTION_LLM_MODEL 或 ingestion_llm_provider 对应模型"
        )

    try:
        from backend.core.models import create_chat_model
    except Exception as exc:
        raise SelfExtractionError(f"模型工厂不可用: {exc}") from exc

    configured_batch_size = max(
        int(getattr(settings, "self_extraction_batch_chunks", 8) or 8),
        1,
    )
    batch_size = _adaptive_batch_size(len(chunks), configured_batch_size)
    timeout = float(getattr(settings, "self_extraction_llm_timeout", 45) or 45)
    max_output_tokens = max(
        int(getattr(settings, "self_extraction_llm_max_tokens", 8192) or 8192),
        1,
    )
    configured_retries = getattr(settings, "self_extraction_batch_retries", 1)
    max_retries = max(int(1 if configured_retries is None else configured_retries), 0)
    configured_recovery_retries = getattr(
        settings,
        "self_extraction_failed_batch_retries",
        max_retries + 2,
    )
    recovery_retries = max(
        int(
            max_retries + 2
            if configured_recovery_retries is None
            else configured_recovery_retries
        ),
        max_retries,
    )
    max_failure_ratio = float(
        getattr(settings, "self_extraction_max_batch_failure_ratio", 0.25) or 0.25
    )
    merged_entities: dict[str, ExtractedEntity] = {}
    merged_relations: dict[str, ExtractedRelation] = {}

    model = create_chat_model(
        name=model_name,
        thinking_enabled=False,
        max_retries=0,
        max_tokens=max_output_tokens,
        request_timeout=timeout,
    )
    batches = [
        (offset, chunks[offset : offset + batch_size])
        for offset in range(0, len(chunks), batch_size)
    ]
    logger.info(
        "[self-ingestion] text extraction scheduling: chunks=%d "
        "configured_batch_size=%d effective_batch_size=%d batches=%d "
        "global_limit=%d per_doc_window=%d",
        len(chunks),
        configured_batch_size,
        batch_size,
        len(batches),
        llm_batch_concurrency(),
        _per_doc_batch_window(),
    )
    batch_semaphore = await semaphore(
        "self_ingestion_generation_model",
        generation_model_concurrency(),
    )

    async def invoke_batch(
        offset: int,
        batch: list[IngestChunk],
        *,
        retries: int,
    ) -> tuple[int, ExtractionResult]:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = await asyncio.wait_for(
                    model.ainvoke(_schema_prompt(schema, batch)),
                    timeout=timeout,
                )
                payload = loads_json_object(_message_content(response))
                partial = payload_to_extraction(kb_id, kb_code, batch, payload, schema=schema)
                return offset, partial
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    backoff = min(0.5 * (2**attempt), 4.0)
                    await asyncio.sleep(random.uniform(backoff * 0.5, backoff))
                    continue
        assert last_exc is not None
        raise last_exc

    async def run_batch(offset: int, batch: list[IngestChunk]) -> tuple[int, ExtractionResult]:
        async with batch_semaphore:
            return await invoke_batch(offset, batch, retries=max_retries)

    async def run_batch_safely(offset: int, batch: list[IngestChunk]) -> tuple[int, ExtractionResult | Exception]:
        try:
            return await run_batch(offset, batch)
        except Exception as exc:
            logger.warning(
                "[self-ingestion] LLM extraction batch failed: offset=%s size=%s error=%s",
                offset,
                len(batch),
                _exc_summary(exc),
            )
            return offset, exc

    async def recover_batch(
        offset: int,
        batch: list[IngestChunk],
        *,
        depth: int = 0,
        prior_error: Exception | None = None,
    ) -> tuple[list[tuple[int, ExtractionResult]], list[dict[str, Any]]]:
        if prior_error is not None and len(batch) > 1:
            mid = max(len(batch) // 2, 1)
            logger.warning(
                "[self-ingestion] failed batch split immediately: "
                "offset=%s size=%s depth=%s error=%s",
                offset,
                len(batch),
                depth,
                _exc_summary(prior_error),
            )
            (left_success, left_failures), (right_success, right_failures) = await asyncio.gather(
                recover_batch(offset, batch[:mid], depth=depth + 1),
                recover_batch(offset + mid, batch[mid:], depth=depth + 1),
            )
            return left_success + right_success, left_failures + right_failures

        try:
            async with batch_semaphore:
                recovered = await invoke_batch(
                    offset,
                    batch,
                    retries=recovery_retries,
                )
            logger.info(
                "[self-ingestion] LLM extraction batch recovered: offset=%s size=%s depth=%s",
                offset,
                len(batch),
                depth,
            )
            return [recovered], []
        except Exception as exc:
            if len(batch) > 1:
                mid = max(len(batch) // 2, 1)
                logger.warning(
                    "[self-ingestion] LLM extraction batch split for recovery: offset=%s size=%s depth=%s error=%s",
                    offset,
                    len(batch),
                    depth,
                    _exc_summary(exc),
                )
                (left_success, left_failures), (right_success, right_failures) = await asyncio.gather(
                    recover_batch(offset, batch[:mid], depth=depth + 1),
                    recover_batch(offset + mid, batch[mid:], depth=depth + 1),
                )
                return left_success + right_success, left_failures + right_failures
            return [], [
                {
                    "offset": offset,
                    "chunk_ids": [chunk.chunk_id for chunk in batch],
                    "error": _exc_summary(exc)[:300],
                }
            ]

    per_doc_window = min(
        len(batches),
        llm_batch_concurrency(),
        _per_doc_batch_window(),
    )
    results: list[tuple[int, ExtractionResult | Exception]] = []
    for start in range(0, len(batches), per_doc_window):
        window = batches[start : start + per_doc_window]
        window_results = await asyncio.gather(
            *(run_batch_safely(offset, batch) for offset, batch in window)
        )
        results.extend(window_results)
    failures: list[dict[str, Any]] = []
    successes: list[tuple[int, ExtractionResult]] = []
    failed_batches: list[tuple[int, list[IngestChunk], Exception]] = []
    for offset, value in sorted(results, key=lambda item: item[0]):
        if isinstance(value, Exception):
            failed_batches.append((offset, chunks[offset : offset + batch_size], value))
        else:
            successes.append((offset, value))

    if failed_batches:
        recovered = await asyncio.gather(
            *(
                recover_batch(offset, batch, prior_error=error)
                for offset, batch, error in failed_batches
            )
        )
        for recovered_successes, recovered_failures in recovered:
            successes.extend(recovered_successes)
            failures.extend(recovered_failures)

    failure_ratio = len(failures) / max(len(batches), 1)
    if not successes:
        raise SelfExtractionError("所有文本抽取 batch 均失败")
    if failures and failure_ratio > max_failure_ratio:
        raise SelfExtractionError(
            f"文本抽取 batch 失败比例过高: failed={len(failures)} total={len(batches)} "
            f"ratio={failure_ratio:.2f}"
        )

    for _, partial in successes:
        merged_entities.update(partial.entities)
        merged_relations.update(partial.relations)

    return ExtractionResult(entities=merged_entities, relations=merged_relations), {
        "text_batch_count": len(batches),
        "text_batch_configured_size": configured_batch_size,
        "text_batch_effective_size": batch_size,
        "text_batch_failed_count": len(failures),
        "text_batch_failures": failures,
        "text_batch_retry_count": max_retries,
        "text_batch_recovery_retry_count": recovery_retries,
        "text_batch_failure_ratio": round(failure_ratio, 4),
        "partial_text_extraction": bool(failures),
    }


async def extract_chunks_schema_aware(
    kb_id: str,
    chunks: list[IngestChunk],
    *,
    kb_code: str = "",
    subject: str | None = None,
    blocks: list[IngestBlock] | None = None,
    image_assets: dict[str, ImageAsset] | None = None,
    require_llm: bool | None = None,
) -> tuple[ExtractionResult, dict]:
    """Extract entities/relations via models only (text LLM + image VLM)."""
    schema = load_schema_for_subject(subject)
    if _extraction_mode() == "simple":
        from backend.rag.ingestion.extraction.simple_extractor import (
            extract_chunks as simple_extract_chunks,
        )

        extraction = simple_extract_chunks(kb_id, chunks, kb_code=kb_code)
        extraction, normalization_audit = normalize_extraction(
            extraction,
            kb_id=kb_id,
            kb_code=kb_code,
        )
        gated, audit = apply_quality_gate(extraction, schema)
        gated, orphan_audit = _apply_orphan_linking(gated, schema)
        audit["extractor"] = "simple"
        audit["text_chunk_count"] = len([c for c in chunks if (c.block_type or "text") in _TEXT_BLOCK_TYPES])
        audit["image_chunk_count"] = len([c for c in chunks if (c.block_type or "") in _IMAGE_BLOCK_TYPES])
        audit["normalization"] = normalization_audit
        audit["orphan_linking"] = orphan_audit
        return gated, audit
    must_use_model = _require_model_extraction() if require_llm is None else bool(require_llm)

    text_chunks = [c for c in chunks if (c.block_type or "text") in _TEXT_BLOCK_TYPES]
    image_chunks = [c for c in chunks if (c.block_type or "") in _IMAGE_BLOCK_TYPES]

    sources: list[str] = []
    parts: list[ExtractionResult] = []
    extraction_audits: dict[str, Any] = {}

    try:
        if text_chunks:
            text_extraction, text_audit = await _llm_extract_text_chunks(
                kb_id, kb_code, text_chunks, schema
            )
            parts.append(text_extraction)
            extraction_audits.update(text_audit)
            sources.append("llm_text")
        if image_chunks:
            parts.append(
                await extract_image_chunks(
                    kb_id,
                    kb_code,
                    image_chunks,
                    schema,
                    image_assets=image_assets,
                    blocks=blocks,
                    text_llm_model_name=_resolve_llm_model_name(),
                )
            )
            sources.append("vlm_image")
    except Exception as exc:
        if must_use_model:
            raise SelfExtractionError(str(exc)) from exc
        logger.warning("[self-ingestion] model extraction failed: %s", exc)
        from backend.rag.ingestion.extraction.simple_extractor import (
            extract_chunks as simple_extract_chunks,
        )

        extraction = simple_extract_chunks(kb_id, chunks, kb_code=kb_code)
        extraction, normalization_audit = normalize_extraction(
            extraction, kb_id=kb_id, kb_code=kb_code,
        )
        gated, audit = apply_quality_gate(extraction, schema)
        gated, orphan_audit = _apply_orphan_linking(gated, schema)
        audit["extractor"] = "simple_fallback"
        audit["normalization"] = normalization_audit
        audit["orphan_linking"] = orphan_audit
        audit["extraction_error"] = str(exc)[:300]
        return gated, audit

    extraction = _merge_extractions(*parts) if parts else ExtractionResult(entities={}, relations={})
    extraction, normalization_audit = normalize_extraction(
        extraction,
        kb_id=kb_id,
        kb_code=kb_code,
    )
    gated, audit = apply_quality_gate(extraction, schema)
    gated, orphan_audit = _apply_orphan_linking(gated, schema)
    audit["extractor"] = "+".join(sources) if sources else "none"
    audit["text_chunk_count"] = len(text_chunks)
    audit["image_chunk_count"] = len(image_chunks)
    audit.update(extraction_audits)
    audit["normalization"] = normalization_audit
    audit["orphan_linking"] = orphan_audit
    return gated, audit
