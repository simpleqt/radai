"""Vision (VLM) entity/relation extraction for image blocks."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any

from backend.config import settings
from backend.config.loggers import get_logger
from backend.rag.ingestion.concurrency import (
    generation_model_concurrency,
    semaphore,
    vision_model_concurrency,
)
from backend.rag.ingestion.extraction.json_repair import loads_json_object
from backend.rag.ingestion.extraction.payload_mapper import payload_to_extraction
from backend.rag.ingestion.extraction.simple_extractor import ExtractionResult
from backend.rag.ingestion.models import IngestBlock, IngestChunk
from backend.rag.ingestion.schema.schema_loader import (
    SchemaDefinition,
    few_shot_examples_for_schema,
    format_schema_for_prompt,
)


logger = get_logger(__name__)


@dataclass(slots=True)
class ImageAsset:
    block_id: str
    content: bytes
    content_type: str = "image/png"
    file_name: str = ""


def _resolve_vision_model_name() -> str | None:
    explicit = str(getattr(settings, "self_vision_model", "") or "").strip()
    if explicit:
        return explicit
    return str(getattr(settings, "vlm_model", "") or "").strip() or None


def _vision_schema_prompt(
    schema: SchemaDefinition,
    chunk: IngestChunk,
    file_name: str,
    *,
    compact: bool = False,
) -> str:
    limits = (
        "每张图片最多输出 12 个实体、15 条关系；JSON 保持紧凑单行优先，禁止注释和多余字段。\n"
        if compact
        else "每张图片最多输出 20 个实体、25 条关系；JSON 保持紧凑，禁止冗长描述。\n"
    )
    return (
        "你是科技文档图片知识抽取助手。根据图片内容抽取实体与关系，只输出 JSON。\n"
        "必须遵守 schema；实体类型和关系类型必须使用 schema id，不要输出中文标签；"
        "不确定则降低 confidence，不要编造。\n"
        f"{limits}\n"
        f"{format_schema_for_prompt(schema)}\n\n"
        f"few_shot_examples:\n{json.dumps(few_shot_examples_for_schema(schema), ensure_ascii=False)}\n\n"
        f"chunk_id: {chunk.chunk_id}\n"
        f"doc_id: {chunk.doc_id}\n"
        f"file_name: {file_name}\n"
        f"已有文本描述（可参考）:\n{(chunk.content or '')[:1200]}\n\n"
        "输出格式：\n"
        "{\n"
        '  "entities": [{"name": "...", "entity_type": "...", "confidence": 0-100, "chunk_ids": ["..."]}],\n'
        '  "relations": [{"source": "...", "target": "...", "relation_type": "...", "confidence": 0-100, "chunk_ids": ["..."]}]\n'
        "}"
    )


async def _invoke_vision_model(
    model: Any,
    *,
    prompt: str,
    image_bytes: bytes | None,
    content_type: str,
    timeout: float,
) -> dict[str, Any]:
    if image_bytes:
        img_b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{content_type};base64,{img_b64}"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
    else:
        messages = [{"role": "user", "content": prompt}]

    response = await asyncio.wait_for(model.ainvoke(messages), timeout=timeout)
    raw = getattr(response, "content", response)
    if isinstance(raw, list):
        raw = "".join(
            item.get("text", str(item)) if isinstance(item, dict) else str(item)
            for item in raw
        )
    return loads_json_object(str(raw or ""))


async def _invoke_vision_model_with_retry(
    model: Any,
    *,
    schema: SchemaDefinition,
    chunk: IngestChunk,
    file_name: str,
    image_bytes: bytes | None,
    content_type: str,
    timeout: float,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for compact in (False, True):
        prompt = _vision_schema_prompt(schema, chunk, file_name, compact=compact)
        if compact and image_bytes is None:
            prompt += "\n\n注意：未提供原始图片字节，请基于文本描述抽取。"
        try:
            return await _invoke_vision_model(
                model,
                prompt=prompt,
                image_bytes=image_bytes,
                content_type=content_type,
                timeout=timeout,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            logger.warning(
                "[self-ingestion] VLM JSON 解析失败，chunk=%s compact=%s error=%s",
                chunk.chunk_id,
                compact,
                exc,
            )
    if last_error is not None:
        raise last_error
    return {"entities": [], "relations": []}


async def extract_image_chunks(
    kb_id: str,
    kb_code: str,
    image_chunks: list[IngestChunk],
    schema: SchemaDefinition,
    *,
    image_assets: dict[str, ImageAsset] | None = None,
    blocks: list[IngestBlock] | None = None,
    text_llm_model_name: str | None = None,
) -> ExtractionResult:
    """Extract entities/relations from image chunks via VLM (preferred) or text LLM fallback."""
    if not image_chunks:
        return ExtractionResult(entities={}, relations={})

    assets = image_assets or {}
    block_by_id = {block.block_id: block for block in (blocks or [])}
    vision_model_name = _resolve_vision_model_name()
    timeout = float(getattr(settings, "self_extraction_llm_timeout", 45) or 45)

    merged_entities: dict = {}
    merged_relations: dict = {}

    try:
        from backend.core.models import create_chat_model
    except Exception as exc:
        raise RuntimeError(f"模型工厂不可用，无法对图片做实体关系抽取: {exc}") from exc

    vision_model = None
    text_model = None
    if vision_model_name:
        try:
            vision_model = create_chat_model(name=vision_model_name, thinking_enabled=False)
        except Exception as exc:
            logger.warning("[self-ingestion] VLM 模型加载失败: %s", exc)

    if text_llm_model_name and vision_model is None:
        text_model = create_chat_model(name=text_llm_model_name, thinking_enabled=False)

    if vision_model is None and text_model is None:
        raise RuntimeError(
            "图片实体/关系抽取需要配置 SELF_VISION_MODEL 或 SELF_EXTRACTION_LLM_MODEL"
        )

    generation_semaphore = await semaphore(
        "self_ingestion_generation_model",
        generation_model_concurrency(),
    )
    vision_semaphore = asyncio.Semaphore(vision_model_concurrency())

    async def run_chunk(chunk: IngestChunk) -> ExtractionResult:
        block = block_by_id.get(chunk.block_id or "")
        file_name = (block.original_file_name if block else None) or chunk.file_name or "image"
        asset = assets.get(chunk.block_id or "")
        image_bytes = asset.content if asset else None
        content_type = asset.content_type if asset else "image/png"

        prompt = _vision_schema_prompt(schema, chunk, file_name)
        payload: dict[str, Any]

        try:
            async with vision_semaphore:
                async with generation_semaphore:
                    if vision_model is not None and image_bytes:
                        payload = await _invoke_vision_model_with_retry(
                            vision_model,
                            schema=schema,
                            chunk=chunk,
                            file_name=file_name,
                            image_bytes=image_bytes,
                            content_type=content_type,
                            timeout=timeout,
                        )
                        extractor_tag = "vlm"
                    elif vision_model is not None and not image_bytes:
                        payload = await _invoke_vision_model_with_retry(
                            vision_model,
                            schema=schema,
                            chunk=chunk,
                            file_name=file_name,
                            image_bytes=None,
                            content_type=content_type,
                            timeout=timeout,
                        )
                        extractor_tag = "vlm_text"
                    else:
                        assert text_model is not None
                        response = await asyncio.wait_for(text_model.ainvoke(prompt), timeout=timeout)
                        raw = getattr(response, "content", response)
                        if isinstance(raw, list):
                            raw = "".join(
                                item.get("text", str(item))
                                if isinstance(item, dict)
                                else str(item)
                                for item in raw
                            )
                        payload = loads_json_object(str(raw or ""))
                        extractor_tag = "llm_image_text"
        except (json.JSONDecodeError, ValueError, asyncio.TimeoutError) as exc:
            logger.warning(
                "[self-ingestion] 图片抽取失败，跳过该 chunk: chunk=%s error=%s",
                chunk.chunk_id,
                exc,
            )
            return ExtractionResult(entities={}, relations={})

        partial = payload_to_extraction(kb_id, kb_code, [chunk], payload, schema=schema)
        logger.debug(
            "[self-ingestion] image extraction chunk=%s via=%s entities=%d relations=%d",
            chunk.chunk_id, extractor_tag, len(partial.entities), len(partial.relations),
        )
        return partial

    for partial in await asyncio.gather(*(run_chunk(chunk) for chunk in image_chunks)):
        merged_entities.update(partial.entities)
        merged_relations.update(partial.relations)

    return ExtractionResult(entities=merged_entities, relations=merged_relations)
