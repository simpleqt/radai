"""Normalize embedded images before sending them to vision models."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
import weakref
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from backend.config.loggers import get_logger

logger = get_logger(__name__)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PLACEABLE_WMF_MAGIC = b"\xd7\xcd\xc6\x9a"
_METAFILE_MIMES = {
    "image/wmf": "wmf",
    "image/x-wmf": "wmf",
    "application/wmf": "wmf",
    "application/x-wmf": "wmf",
    "image/emf": "emf",
    "image/x-emf": "emf",
    "application/emf": "emf",
    "application/x-emf": "emf",
}
_LOOP_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_SEMAPHORE_LOCK = threading.Lock()


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


_MAX_RASTER_BYTES = _positive_int_env(
    "SELF_INGESTION_IMAGE_MAX_BYTES",
    4 * 1024 * 1024,
)
_MAX_RASTER_DIMENSION = _positive_int_env(
    "SELF_INGESTION_IMAGE_MAX_DIMENSION",
    2048,
)
_MAX_RASTER_PIXELS = _positive_int_env(
    "SELF_INGESTION_IMAGE_MAX_PIXELS",
    4_000_000,
)


@dataclass(slots=True)
class PreparedImage:
    content: bytes
    content_type: str
    file_name: str
    converted_from: str | None = None


def _conversion_semaphore() -> asyncio.Semaphore:
    """Limit local LibreOffice processes across all concurrently ingested files."""

    loop = asyncio.get_running_loop()
    with _SEMAPHORE_LOCK:
        current = _LOOP_SEMAPHORES.get(loop)
        if current is None:
            current = asyncio.Semaphore(2)
            _LOOP_SEMAPHORES[loop] = current
        return current


def _detect_raster_mime(content: bytes) -> str | None:
    if content.startswith(_PNG_MAGIC):
        return "image/png"
    if content.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"BM"):
        return "image/bmp"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return None


def _detect_metafile_kind(
    content: bytes,
    content_type: str,
    file_name: str,
) -> str | None:
    suffix = Path(file_name or "").suffix.lower()
    if suffix in {".wmf", ".emf"}:
        return suffix[1:]

    declared = _METAFILE_MIMES.get((content_type or "").strip().lower())
    if declared:
        return declared

    if content.startswith(_PLACEABLE_WMF_MAGIC):
        return "wmf"
    if (
        len(content) >= 6
        and content[:2] in {b"\x01\x00", b"\x02\x00"}
        and content[2:4] == b"\x09\x00"
        and content[4:6] in {b"\x00\x01", b"\x00\x03"}
    ):
        return "wmf"
    if len(content) >= 44 and content[:4] == b"\x01\x00\x00\x00" and content[40:44] == b" EMF":
        return "emf"
    return None


async def _convert_metafile_to_png(content: bytes, kind: str) -> bytes | None:
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if not executable:
        logger.warning(
            "[self-ingestion] 无法转换 %s：未安装 LibreOffice，跳过该图片视觉处理",
            kind.upper(),
        )
        return None

    async with _conversion_semaphore():
        with tempfile.TemporaryDirectory(prefix="self-ingestion-metafile-") as temp_dir:
            root = Path(temp_dir)
            source = root / f"input.{kind}"
            output_dir = root / "output"
            profile_dir = root / "profile"
            output_dir.mkdir()
            profile_dir.mkdir()
            await asyncio.to_thread(source.write_bytes, content)

            process = await asyncio.create_subprocess_exec(
                executable,
                f"-env:UserInstallation={profile_dir.as_uri()}",
                "--headless",
                "--convert-to",
                "png",
                "--outdir",
                str(output_dir),
                str(source),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                logger.warning(
                    "[self-ingestion] LibreOffice 转换 %s 超时，跳过该图片视觉处理",
                    kind.upper(),
                )
                return None

            output = output_dir / "input.png"
            if process.returncode != 0 or not output.is_file():
                detail = (stderr or stdout).decode("utf-8", errors="ignore").strip()
                logger.warning(
                    "[self-ingestion] LibreOffice 转换 %s 失败 rc=%s detail=%s",
                    kind.upper(),
                    process.returncode,
                    detail[:300],
                )
                return None

            converted = await asyncio.to_thread(output.read_bytes)
            if not converted.startswith(_PNG_MAGIC):
                logger.warning(
                    "[self-ingestion] LibreOffice 转换 %s 未生成有效 PNG，跳过该图片视觉处理",
                    kind.upper(),
                )
                return None
            return converted


def _optimize_large_raster(
    content: bytes,
    content_type: str,
    file_name: str,
) -> PreparedImage | None:
    """Bound retained image memory while preserving VLM-relevant detail."""

    try:
        from PIL import Image, ImageOps

        with Image.open(BytesIO(content)) as source:
            width, height = source.size
            pixels = max(width, 1) * max(height, 1)
            needs_resize = (
                width > _MAX_RASTER_DIMENSION
                or height > _MAX_RASTER_DIMENSION
                or pixels > _MAX_RASTER_PIXELS
            )
            if not needs_resize and len(content) <= _MAX_RASTER_BYTES:
                return None

            image = ImageOps.exif_transpose(source)
            scale = min(
                1.0,
                _MAX_RASTER_DIMENSION / max(image.width, 1),
                _MAX_RASTER_DIMENSION / max(image.height, 1),
                (_MAX_RASTER_PIXELS / max(image.width * image.height, 1)) ** 0.5,
            )
            if scale < 1.0:
                image = image.resize(
                    (
                        max(1, int(image.width * scale)),
                        max(1, int(image.height * scale)),
                    ),
                    Image.Resampling.LANCZOS,
                )

            has_alpha = image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            output = BytesIO()
            if has_alpha:
                image.save(output, format="WEBP", quality=82, method=4)
                output_type = "image/webp"
                output_name = str(Path(file_name or "image.png").with_suffix(".webp"))
            else:
                image.convert("RGB").save(
                    output,
                    format="JPEG",
                    quality=85,
                    optimize=True,
                    progressive=True,
                )
                output_type = "image/jpeg"
                output_name = str(Path(file_name or "image.jpg").with_suffix(".jpg"))
            optimized = output.getvalue()
    except Exception as exc:
        logger.warning(
            "[self-ingestion] 大图压缩失败，保留原图: file=%s error=%s",
            file_name,
            exc,
        )
        return None

    logger.info(
        "[self-ingestion] 大图内存削峰: file=%s %d->%d bytes",
        file_name,
        len(content),
        len(optimized),
    )
    return PreparedImage(
        content=optimized,
        content_type=output_type,
        file_name=output_name,
        converted_from=content_type or "raster",
    )


async def prepare_image_for_vlm(
    content: bytes,
    content_type: str,
    file_name: str,
) -> PreparedImage | None:
    """Return model-safe raster bytes, converting WMF/EMF to PNG when needed.

    Unknown or corrupt image bytes are rejected before reaching the remote model,
    preventing provider-side 400/retry storms.
    """

    raster_mime = _detect_raster_mime(content)
    if raster_mime:
        optimized = await asyncio.to_thread(
            _optimize_large_raster,
            content,
            raster_mime,
            file_name,
        )
        if optimized is not None:
            return optimized
        return PreparedImage(
            content=content,
            content_type=raster_mime,
            file_name=file_name,
        )

    metafile_kind = _detect_metafile_kind(content, content_type, file_name)
    if metafile_kind:
        converted = await _convert_metafile_to_png(content, metafile_kind)
        if converted is None:
            return None
        original = Path(file_name or f"image.{metafile_kind}")
        return PreparedImage(
            content=converted,
            content_type="image/png",
            file_name=str(original.with_suffix(".png")),
            converted_from=metafile_kind,
        )

    logger.warning(
        "[self-ingestion] 不支持或损坏的图片，发送 VLM 前跳过: file=%s content_type=%s",
        file_name,
        content_type,
    )
    return None
