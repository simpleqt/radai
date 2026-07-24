"""Artifact registration helpers for platform agent runtimes."""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import quote

from backend.config.loggers import get_logger
from backend.core.config import parse_internal_thread_key
from backend.services.user_file_service import (
    register_local_artifact_for_thread,
    register_minio_artifact_for_thread_sync,
)
from backend.storage import get_user_minio_bucket_name

logger = get_logger(__name__)


CODE_LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".vue": "vue",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".ps1": "powershell",
    ".sql": "sql",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".txt": "text",
    ".log": "text",
    ".ini": "ini",
    ".conf": "text",
}

PREVIEWABLE_TEXT_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".text",
    ".html",
    ".htm",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".conf",
    ".log",
    *CODE_LANGUAGE_BY_EXTENSION.keys(),
}

EXPLICIT_MIME_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".vue": "text/x-vue",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".js": "text/javascript",
    ".jsx": "text/javascript",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".sql": "text/x-sql",
}


def _get_suffix(filename: str) -> str:
    return Path(str(filename or "")).suffix.lower()


def guess_artifact_mime_type(filename: str) -> str:
    suffix = _get_suffix(filename)
    if suffix in EXPLICIT_MIME_TYPES:
        return EXPLICIT_MIME_TYPES[suffix]

    guessed, _ = mimetypes.guess_type(str(filename or ""))
    if guessed:
        return guessed

    lower_name = str(filename or "").lower()
    if lower_name.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "application/octet-stream"


def guess_artifact_kind(filename: str, mime_type: str | None = None) -> str:
    suffix = _get_suffix(filename)
    mime_value = str(mime_type or "").lower()

    if suffix in {".md", ".markdown"} or "markdown" in mime_value:
        return "markdown"
    if suffix in {".html", ".htm"} or mime_value in {"text/html", "application/xhtml+xml"}:
        return "html"
    if suffix in CODE_LANGUAGE_BY_EXTENSION:
        return "code"
    if mime_value.startswith("text/"):
        return "text"
    if mime_value in {
        "application/json",
        "application/xml",
        "text/xml",
        "application/yaml",
        "application/x-yaml",
        "text/yaml",
        "application/toml",
        "text/javascript",
        "application/javascript",
    }:
        return "code" if suffix in CODE_LANGUAGE_BY_EXTENSION else "text"
    return "file"


def guess_artifact_language(filename: str) -> str | None:
    suffix = _get_suffix(filename)
    return CODE_LANGUAGE_BY_EXTENSION.get(suffix)


def build_minio_artifact_download_url(
    *,
    minio_object: str,
    filename: str,
    mime_type: str | None = None,
) -> str:
    query = (
        f"object_name={quote(str(minio_object or '').lstrip('/'), safe='')}"
        f"&filename={quote(str(Path(filename or 'artifact').name), safe='')}"
    )
    if mime_type:
        query += f"&mime_type={quote(str(mime_type), safe='')}"
    return f"/api/v2/files/minio-artifact/download?{query}"


async def build_registered_artifact_metadata(
    *,
    internal_thread_id: str,
    artifact_path: str,
    title: str,
    minio_object: str | None = None,
) -> dict[str, Any]:
    """Register artifact and return frontend-safe metadata."""
    file_id = artifact_path
    mime_type = guess_artifact_mime_type(artifact_path)
    url = (
        build_minio_artifact_download_url(
            minio_object=minio_object,
            filename=artifact_path,
            mime_type=mime_type,
        )
        if minio_object
        else f"/api/v2/files/{file_id}/download"
    )
    artifact_type = guess_artifact_kind(artifact_path, mime_type)
    language = guess_artifact_language(artifact_path)
    suffix = _get_suffix(artifact_path)

    try:
        resolved = None
        if minio_object:
            resolved = await asyncio.to_thread(
                register_minio_artifact_for_thread_sync,
                internal_thread_id=internal_thread_id,
                artifact_filename=artifact_path,
                storage_bucket=get_user_minio_bucket_name(),
                storage_object=minio_object,
                mime_type=mime_type,
            )
        else:
            resolved = await register_local_artifact_for_thread(
                internal_thread_id=internal_thread_id,
                artifact_filename=artifact_path,
            )
        if resolved:
            file_id, url = resolved
    except Exception as exc:
        logger.warning("[AgentArtifact] 工件注册失败，降级输出: %s", exc)

    if minio_object and file_id == artifact_path:
        logger.warning("[AgentArtifact] MinIO 工件未完成用户文件登记，已避免回退本地路径: %s", minio_object)
    elif not minio_object and file_id == artifact_path:
        _, external_thread_id = parse_internal_thread_key(internal_thread_id)
        url = (
            f"/api/v2/threads/{quote(external_thread_id, safe='')}/artifacts/"
            f"{quote(artifact_path.lstrip('/'), safe='/')}"
        )

    metadata: dict[str, Any] = {
        "id": file_id,
        "file_id": file_id,
        "title": title,
        "type": artifact_type,
        "filename": artifact_path,
        "mime_type": mime_type,
        "extension": suffix,
        "previewable": artifact_type in {"markdown", "html", "code", "text"} or suffix in PREVIEWABLE_TEXT_EXTENSIONS,
    }
    if language:
        metadata["language"] = language
    if url:
        metadata["url"] = url
    return metadata
