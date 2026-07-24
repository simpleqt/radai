"""Standalone image file parser (附录 E.3) with VLM vision summary."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.config.loggers import get_logger
from backend.rag.ingestion.ids import sha256_text, stable_block_id
from backend.rag.ingestion.models import IngestBlock, ParsedDocument, SourceDocument
from backend.rag.ingestion.parsers.base import BaseParser

logger = get_logger(__name__)

_VISION_PROMPT = (
    "你是科技文档图片分析助手。请用中文描述图片内容，100-300 字。\n"
    "输出 JSON：\n"
    '{"vision_summary": "...", "image_type": "diagram|photo|chart|flowchart|formula_screenshot"}'
)


class ImageParser(BaseParser):
    """Parse standalone image files.

    Calls VLM to generate ``vision_summary``; degrades to filename if
    VLM is unavailable or fails.
    """

    async def parse(
        self,
        content: bytes,
        source_document: SourceDocument,
    ) -> ParsedDocument:
        file_name = source_document.original_file_name or "image"
        ext = Path(file_name).suffix.lower().lstrip(".")
        content_type = _ext_to_mime(ext)
        warnings: list[str] = []

        vision_summary = await self._try_vlm(content, content_type, file_name, warnings)

        if vision_summary:
            block_content = f"[图片] {file_name}\n\n{vision_summary}"
        else:
            block_content = f"[图片] {file_name}"

        img_b64_preview = base64.b64encode(content[:1024]).decode("ascii")

        block = self._make_block(
            source_document,
            "image",
            block_content,
            0,
            metadata={
                "object_storage_uri": source_document.object_storage_uri,
                "content_type": content_type,
                "img_size_bytes": len(content),
                "img_base64_preview": img_b64_preview,
                "vision_model": getattr(settings, "self_vision_model", None),
                "vision_summary": vision_summary,
            },
        )
        return ParsedDocument(
            source_document=source_document,
            blocks=[block],
            raw_text=block_content,
            parser_artifacts={"parser": "image_v2"},
            warnings=warnings,
        )

    async def _try_vlm(
        self,
        content: bytes,
        content_type: str,
        file_name: str,
        warnings: list[str],
    ) -> str:
        model_name = str(getattr(settings, "self_vision_model", "") or "").strip()
        if not model_name:
            warnings.append("VLM 未配置 (self_vision_model)，降级为文件名")
            return ""
        try:
            from backend.core.models import create_chat_model
        except Exception:
            warnings.append("create_chat_model 不可用，VLM 降级")
            return ""

        try:
            model = create_chat_model(name=model_name, thinking_enabled=False)
            img_b64 = base64.b64encode(content).decode("ascii")
            data_url = f"data:{content_type};base64,{img_b64}"
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{_VISION_PROMPT}\n\n文件名: {file_name}"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ]
            timeout = float(getattr(settings, "self_extraction_llm_timeout", 45) or 45)
            response = await asyncio.wait_for(model.ainvoke(messages), timeout=timeout)
            raw = getattr(response, "content", response)
            if isinstance(raw, list):
                raw = "".join(
                    item.get("text", str(item)) if isinstance(item, dict) else str(item)
                    for item in raw
                )
            text = str(raw or "").strip()
            try:
                from backend.rag.ingestion.extraction.json_repair import loads_json_object
                payload = loads_json_object(text)
                return str(payload.get("vision_summary", text))[:2000]
            except Exception:
                return text[:2000]
        except Exception as exc:
            logger.warning("[self-ingestion] VLM vision summary 失败: %s", exc)
            warnings.append(f"VLM 失败: {exc}")
            return ""

    def _make_block(
        self,
        sd: SourceDocument,
        block_type: str,
        content: str,
        order_index: int,
        metadata: dict[str, Any] | None = None,
    ) -> IngestBlock:
        block_id = stable_block_id(
            sd.doc_id, sd.commit_version, block_type,
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
            order_index=order_index,
            source_hash=sd.source_hash,
            original_file_name=sd.original_file_name,
            object_storage_uri=sd.object_storage_uri,
            content_hash=sha256_text(content),
            stable_block_fingerprint=sha256_text(f"{block_type}:{content}"),
            metadata=metadata or {},
            job_id=sd.job_id,
        )


def _ext_to_mime(ext: str) -> str:
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
        "tiff": "image/tiff",
    }.get(ext, "image/png")
