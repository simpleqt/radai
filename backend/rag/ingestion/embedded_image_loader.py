"""Load embedded document images (MinerU img_path / images dict) for self-ingestion VLM."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from backend.config.loggers import get_logger
from backend.rag.ingestion.extraction.vision_extractor import ImageAsset
from backend.rag.ingestion.models import IngestBlock, SourceDocument
from backend.rag.ingestion.parsers.image_parser import ImageParser
from backend.rag.raganything.utils import is_remote_image_path

logger = get_logger(__name__)

_MIN_IMAGE_BYTES = 100
_VLM_CONCURRENCY = 4


def _detect_image_content_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _image_basename(img_path: str) -> str:
    raw = str(img_path or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("data:image/"):
        return "embedded.png"
    if "://" in raw:
        return Path(urlparse(raw).path).name
    return Path(raw.replace("\\", "/")).name


def resolve_image_url(img_path: str, *, markdown_url: str | None = None) -> str:
    """Resolve MinerU img_path to a fetchable URL (legacy chat_file_parser compatible)."""
    path = str(img_path or "").strip()
    if not path:
        return ""
    if is_remote_image_path(path):
        return path
    if markdown_url:
        return urljoin(markdown_url, path)
    try:
        from backend.storage import get_presigned_url, get_public_url

        obj = path.replace("\\", "/").lstrip("/")
        resolved = get_presigned_url(obj) or get_public_url(obj) or ""
        return str(resolved).strip()
    except Exception:
        return ""


def _is_presigned_url(url: str) -> bool:
    query = urlparse(url).query.lower()
    return "x-amz-signature" in query or "x-amz-algorithm" in query


async def _download_via_minio_sdk(img_path: str) -> bytes:
    """Fallback: read object directly via MinIO SDK (legacy storage path normalization)."""
    from backend.config.settings import settings
    from backend.storage import _get_minio_client, _normalize_minio_object_name

    object_name = _normalize_minio_object_name(img_path)
    if not object_name:
        return b""

    client = _get_minio_client()
    bucket = settings.minio_bucket_name

    def _read() -> bytes:
        response = client.get_object(bucket, object_name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warning(
            "[self-ingestion] MinIO SDK 读取嵌入图片失败: object=%s error=%s",
            object_name[:120],
            exc,
        )
        return b""


async def _download_remote_image_bytes(url: str) -> bytes:
    """Download image bytes from HTTP(S) or MinIO presigned URL (legacy modalprocessors style)."""
    s = str(url).strip()
    if not s:
        return b""
    if s.lower().startswith("data:image/"):
        try:
            _, b64 = s.split(",", 1)
            return base64.b64decode(b64)
        except Exception:
            return b""

    if s.lower().startswith(("http://", "https://")):
        # Presigned URLs must be used as-is; re-encoding path breaks the signature.
        if _is_presigned_url(s):
            fetch_url = s
        else:
            parsed = urlparse(s)
            path = parsed.path
            if "%25" in path:
                fetch_url = s
            else:
                from backend.storage import _encode_minio_path_for_url

                fetch_url = parsed._replace(path=_encode_minio_path_for_url(path)).geturl()

        async with httpx.AsyncClient(trust_env=False, timeout=60.0, follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    response = await client.get(fetch_url)
                    response.raise_for_status()
                    if len(response.content) >= _MIN_IMAGE_BYTES:
                        return response.content
                except Exception as exc:
                    if attempt >= 2:
                        logger.debug(
                            "[self-ingestion] HTTP 下载嵌入图片失败，尝试 MinIO SDK: %s",
                            exc,
                        )
                    else:
                        await asyncio.sleep(0.5 * (2 ** attempt))

    return await _download_via_minio_sdk(s)


def _iter_mineru_image_entries(mineru_images: dict[str, Any]) -> list[tuple[str, Any]]:
    """Flatten MinerU images payload (flat dict or nested {count, images:[...]})."""
    entries: list[tuple[str, Any]] = []
    nested = mineru_images.get("images")
    if isinstance(nested, list):
        for item in nested:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("file_name") or "").strip()
            if not name:
                continue
            entries.append((name, item))
    for key, value in mineru_images.items():
        if key in ("count", "images") or isinstance(value, int):
            continue
        entries.append((str(key), value))
    return entries


def _mineru_entry_matches(img_path: str, name: str) -> bool:
    target = _image_basename(img_path)
    key_name = _image_basename(name)
    if not target:
        return False
    return key_name == target or target in name or key_name in img_path


def _lookup_mineru_image_url(
    img_path: str,
    mineru_images: dict[str, Any] | None,
) -> str | None:
    if not mineru_images:
        return None
    for name, value in _iter_mineru_image_entries(mineru_images):
        if not _mineru_entry_matches(img_path, name):
            continue
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
        if isinstance(value, dict):
            raw_url = (
                value.get("url")
                or value.get("img_url")
                or value.get("image_url")
                or value.get("img_path")
            )
            if raw_url and str(raw_url).startswith(("http://", "https://")):
                return str(raw_url)
    return None


def _bytes_from_mineru_images_value(value: Any) -> tuple[bytes, str] | None:
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return None
        try:
            data = base64.b64decode(value)
        except Exception:
            return None
    elif isinstance(value, dict):
        raw_url = (
            value.get("url")
            or value.get("img_url")
            or value.get("image_url")
            or value.get("img_path")
        )
        if raw_url and str(raw_url).startswith(("http://", "https://")):
            return None
        raw_base64 = value.get("base64") or value.get("content")
        if not raw_base64:
            return None
        try:
            data = base64.b64decode(str(raw_base64))
        except Exception:
            return None
    else:
        return None

    if len(data) < _MIN_IMAGE_BYTES:
        return None
    return data, _detect_image_content_type(data)


def _lookup_mineru_image_bytes(
    img_path: str,
    mineru_images: dict[str, Any] | None,
) -> tuple[bytes, str] | None:
    if not mineru_images:
        return None

    for name, value in _iter_mineru_image_entries(mineru_images):
        if not _mineru_entry_matches(img_path, name):
            continue
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            continue
        decoded = _bytes_from_mineru_images_value(value)
        if decoded:
            return decoded
    return None


async def _load_single_block_image(
    block: IngestBlock,
    *,
    mineru_images: dict[str, Any] | None,
    markdown_url: str | None,
) -> tuple[str, ImageAsset | None]:
    meta = dict(block.metadata or {})
    inline_base64 = str(meta.get("img_base64") or meta.get("image_base64") or "").strip()
    if inline_base64:
        try:
            content = base64.b64decode(inline_base64)
        except Exception:
            content = b""
        if len(content) >= _MIN_IMAGE_BYTES:
            content_type = str(meta.get("content_type") or "").strip() or _detect_image_content_type(content)
            file_name = (
                str(meta.get("part_name") or "").strip()
                or f"embedded-{block.order_index or 0}.png"
            )
            return block.block_id, ImageAsset(
                block_id=block.block_id,
                content=content,
                content_type=content_type,
                file_name=file_name,
            )

    img_path = str(meta.get("img_path") or meta.get("image_path") or "").strip()
    if not img_path:
        return block.block_id, None

    decoded = _lookup_mineru_image_bytes(img_path, mineru_images)
    if decoded:
        content, content_type = decoded
    else:
        mineru_url = _lookup_mineru_image_url(img_path, mineru_images)
        resolved = mineru_url or resolve_image_url(img_path, markdown_url=markdown_url)
        if not resolved and is_remote_image_path(img_path):
            resolved = img_path
        if not resolved:
            return block.block_id, None
        content = await _download_remote_image_bytes(resolved)
        if len(content) < _MIN_IMAGE_BYTES:
            return block.block_id, None
        content_type = _detect_image_content_type(content)

    file_name = _image_basename(img_path) or f"page-{block.page_num or 0}.png"
    return block.block_id, ImageAsset(
        block_id=block.block_id,
        content=content,
        content_type=content_type,
        file_name=file_name,
    )


async def _enrich_block_vision(
    block: IngestBlock,
    asset: ImageAsset,
    parser: ImageParser,
) -> bool:
    caption = str((block.metadata or {}).get("caption") or "").strip()
    file_name = asset.file_name or block.original_file_name or "image"
    warnings: list[str] = []
    vision_summary = await parser._try_vlm(asset.content, asset.content_type, file_name, warnings)
    if warnings:
        logger.debug("[self-ingestion] embedded image VLM warnings block=%s %s", block.block_id, warnings[:2])

    meta = dict(block.metadata or {})
    meta["vision_summary"] = vision_summary
    meta["content_type"] = asset.content_type
    meta["img_size_bytes"] = len(asset.content)
    meta["image_load_source"] = "mineru_embedded"
    block.metadata = meta

    if vision_summary:
        prefix = f"[图片] {caption or file_name}".strip()
        block.content = f"{prefix}\n\n{vision_summary}".strip()
    elif caption:
        block.content = f"[图片] {caption}".strip()
    return bool(vision_summary)


async def load_and_enrich_embedded_images(
    blocks: list[IngestBlock],
    *,
    source_document: SourceDocument | None = None,
    parser_artifacts: dict[str, Any] | None = None,
    run_vision_summary: bool = True,
) -> tuple[dict[str, ImageAsset], dict[str, Any]]:
    """Download embedded images and optionally enrich blocks with VLM summaries."""
    image_blocks = [block for block in blocks if block.block_type == "image"]
    if not image_blocks:
        return {}, {"image_blocks": 0}

    artifacts = parser_artifacts or {}
    mineru_images = artifacts.get("mineru_images")
    if not isinstance(mineru_images, dict):
        mineru_images = None
    markdown_url = str(artifacts.get("md_url") or "").strip() or None
    if mineru_images:
        nested = mineru_images.get("images") if isinstance(mineru_images.get("images"), list) else None
        image_count = len(nested) if nested is not None else len(mineru_images)
        logger.info(
            "[self-ingestion] mineru_images available: count=%d",
            image_count,
        )

    load_results = await asyncio.gather(
        *[
            _load_single_block_image(
                block,
                mineru_images=mineru_images if isinstance(mineru_images, dict) else None,
                markdown_url=markdown_url,
            )
            for block in image_blocks
        ],
        return_exceptions=True,
    )

    assets: dict[str, ImageAsset] = {}
    for result in load_results:
        if isinstance(result, Exception):
            logger.warning("[self-ingestion] embedded image load error: %s", result)
            continue
        block_id, asset = result
        if asset is not None:
            assets[block_id] = asset

    # Large inline base64 values are transient transport only; never persist them
    # into staging payloads.
    for block in image_blocks:
        meta = dict(block.metadata or {})
        if "img_base64" in meta or "image_base64" in meta:
            meta.pop("img_base64", None)
            meta.pop("image_base64", None)
            block.metadata = meta

    vision_enriched = 0
    if run_vision_summary and assets:
        parser = ImageParser()
        semaphore = asyncio.Semaphore(_VLM_CONCURRENCY)

        async def _run(block: IngestBlock) -> bool:
            asset = assets.get(block.block_id)
            if asset is None:
                return False
            async with semaphore:
                return await _enrich_block_vision(block, asset, parser)

        vision_results = await asyncio.gather(
            *[_run(block) for block in image_blocks if block.block_id in assets],
            return_exceptions=True,
        )
        for item in vision_results:
            if item is True:
                vision_enriched += 1

    persisted = 0
    if source_document and source_document.kb_id and source_document.job_id:
        from backend.storage import upload_bytes_to_minio

        for block_id, asset in assets.items():
            ext = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }.get(asset.content_type, ".bin")
            object_key = (
                f"staging/{source_document.kb_id}/{source_document.job_id}/images/"
                f"{block_id}{ext}"
            )
            uploaded = await upload_bytes_to_minio(
                asset.content, object_key, content_type=asset.content_type,
            )
            if not uploaded:
                continue
            persisted += 1
            for block in image_blocks:
                if block.block_id != block_id:
                    continue
                meta = dict(block.metadata or {})
                meta["persistent_object_key"] = object_key
                block.metadata = meta
                break

    stats = {
        "image_blocks": len(image_blocks),
        "loaded": len(assets),
        "vision_enriched": vision_enriched,
        "persisted_to_minio": persisted,
    }
    logger.info(
        "[self-ingestion] embedded images loaded: blocks=%d loaded=%d vision=%d persisted=%d",
        stats["image_blocks"], stats["loaded"], stats["vision_enriched"], stats["persisted_to_minio"],
    )
    return assets, stats
