"""Deep research report tool.

Save a generated research report to the thread outputs directory,
upload it to MinIO, and expose it as an artifact for UI rendering.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Annotated

from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.shared import Pt

from backend.config.loggers import get_logger
from backend.core.agents.state import ThreadState
from backend.core.agents.thread_key import parse_internal_thread_key
from backend.core.config import get_paths
from backend.storage import (
    get_user_minio_bucket_name,
    upload_bytes_to_minio,
    upload_bytes_to_minio_sync,
)

logger = get_logger(__name__)

DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DEFAULT_DOCX_TITLE = "Deep Research Report"


def _safe_report_filename(filename: str | None) -> str:
    """Generate a safe filename for the report.

    If *filename* is provided, preserve CJK / Unicode characters so that
    Chinese titles like "成都理工大学深度研究报告" remain readable.
    Only strip characters that are truly unsafe for filesystems or URLs
    (control chars, slashes, etc.).  Consecutive whitespace / unsafe chars
    are collapsed to a single hyphen.

    If *filename* is omitted, auto-generate from the current timestamp.
    """
    if filename:
        trimmed = filename.strip()
        if trimmed:
            # Remove characters that are unsafe in filenames / URLs,
            # but keep CJK and other Unicode letters.
            cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", trimmed)
            # Collapse consecutive hyphens / whitespace into one hyphen
            cleaned = re.sub(r"[\s-]+", "-", cleaned)
            cleaned = cleaned.strip(".-")
            if cleaned:
                if cleaned.lower().endswith(".md.docx"):
                    cleaned = cleaned[:-8] + ".docx"
                elif cleaned.lower().endswith(".docx.docx"):
                    cleaned = cleaned[:-10] + ".docx"
                elif cleaned.lower().endswith(".md"):
                    cleaned = cleaned[:-3] + ".docx"
                elif not cleaned.lower().endswith(".docx"):
                    cleaned = f"{cleaned}.docx"
                return cleaned

    ts = time.strftime("%Y%m%d-%H%M%S")
    return f"deep-research-report-{ts}.docx"


def _set_style_font(style: object, *, latin: str, east_asia: str, size_pt: int | None = None, bold: bool | None = None) -> None:
    style.font.name = latin
    if size_pt is not None:
        style.font.size = Pt(size_pt)
    if bold is not None:
        style.font.bold = bold
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:eastAsia"), east_asia)


def _apply_reference_styles(doc: Document) -> None:
    styles = doc.styles

    for level, size in ((1, 18), (2, 16), (3, 14), (4, 13)):
        heading = styles[f"Heading {level}"]
        _set_style_font(heading, latin="Calibri", east_asia="Microsoft YaHei", size_pt=size, bold=True)

    normal = styles["Normal"]
    _set_style_font(normal, latin="Calibri", east_asia="Microsoft YaHei", size_pt=12)
    normal_pf = normal.paragraph_format
    normal_pf.line_spacing = 1.5
    normal_pf.first_line_indent = Pt(24)

    for style_name, indent_pt in (("List Bullet", 18), ("List Number", 18), ("List Paragraph", 18)):
        if style_name in styles:
            style = styles[style_name]
            _set_style_font(style, latin="Calibri", east_asia="Microsoft YaHei", size_pt=12)
            style.paragraph_format.left_indent = Pt(indent_pt)

    if "Source Code" in styles:
        source_code = styles["Source Code"]
    else:
        source_code = styles.add_style("Source Code", WD_STYLE_TYPE.PARAGRAPH)
    _set_style_font(source_code, latin="Consolas", east_asia="Microsoft YaHei", size_pt=10)
    source_code.paragraph_format.left_indent = Pt(12)
    source_code.paragraph_format.space_before = Pt(6)
    source_code.paragraph_format.space_after = Pt(6)

    if "Table Grid" in styles:
        _set_style_font(styles["Table Grid"], latin="Calibri", east_asia="Microsoft YaHei", size_pt=10)


def _build_reference_docx_bytes() -> bytes:
    doc = Document()
    _apply_reference_styles(doc)
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _resolve_pandoc_executable() -> str:
    env_path = os.getenv("PANDOC_PATH", "").strip()
    if env_path:
        return env_path

    system_path = shutil.which("pandoc")
    if system_path:
        return system_path

    try:
        import pypandoc

        bundled_path = pypandoc.get_pandoc_path()
        if bundled_path:
            return str(bundled_path)
    except Exception:
        pass

    raise RuntimeError(
        "Pandoc is required for Markdown to DOCX conversion. "
        "Install pandoc/pypandoc_binary or set PANDOC_PATH."
    )


def _render_report_docx_with_python(*, title: str, content: str) -> bytes:
    """Render common Markdown constructs without relying on an external binary."""
    doc = Document()
    _apply_reference_styles(doc)
    normalized_title = title.strip() or DEFAULT_DOCX_TITLE
    lines = (content.strip() or "(No content)").splitlines()

    first_content = next((line for line in lines if line.strip()), "")
    if not re.match(r"^\s*#\s+", first_content):
        doc.add_heading(normalized_title, level=0)

    in_code_block = False
    code_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            if in_code_block:
                doc.add_paragraph("\n".join(code_lines), style="Source Code")
                code_lines = []
            in_code_block = not in_code_block
            continue
        if in_code_block:
            code_lines.append(line)
            continue

        stripped = line.strip()
        if not stripped:
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            level = min(len(heading.group(1)), 4)
            if level == 1 and len(doc.paragraphs) == 0:
                doc.add_heading(heading.group(2).strip(), level=0)
            else:
                doc.add_heading(heading.group(2).strip(), level=max(1, level - 1))
            continue
        if re.match(r"^[-*+]\s+", stripped):
            doc.add_paragraph(re.sub(r"^[-*+]\s+", "", stripped), style="List Bullet")
            continue
        if re.match(r"^\d+[.)]\s+", stripped):
            doc.add_paragraph(re.sub(r"^\d+[.)]\s+", "", stripped), style="List Number")
            continue
        if stripped.startswith(">"):
            doc.add_paragraph(stripped.lstrip("> ").strip(), style="Intense Quote")
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):
            continue
        doc.add_paragraph(stripped)

    if code_lines:
        doc.add_paragraph("\n".join(code_lines), style="Source Code")

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _render_report_docx_bytes(*, title: str, content: str) -> bytes:
    """Render Markdown to DOCX, falling back to python-docx when Pandoc fails."""
    normalized_title = title.strip() or DEFAULT_DOCX_TITLE
    normalized_content = content.strip() or f"# {normalized_title}\n\n(No content)"

    effective_title = normalized_title
    markdown_input = normalized_content
    shift_heading_level = False

    first_non_empty_match = re.search(r"(?m)^\s*\S.*$", normalized_content)
    if first_non_empty_match:
        first_line = first_non_empty_match.group(0)
        h1_match = re.match(r"^\s*#\s+(.+?)\s*$", first_line)
        if h1_match:
            extracted_title = h1_match.group(1).strip()
            if extracted_title:
                effective_title = extracted_title
            start = first_non_empty_match.start()
            end = first_non_empty_match.end()
            markdown_input = (normalized_content[:start] + normalized_content[end:]).strip()
            if not markdown_input:
                markdown_input = "(No content)"
            shift_heading_level = True
    elif not re.match(r"^\s*#\s+", normalized_content):
        markdown_input = f"# {normalized_title}\n\n{normalized_content}"

    try:
        pandoc_path = _resolve_pandoc_executable()
        with tempfile.TemporaryDirectory(prefix="deep-research-docx-") as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_md = tmp_path / "report.md"
            output_docx = tmp_path / "report.docx"

            input_md.write_text(markdown_input, encoding="utf-8")

            reference_override = os.getenv("DEEP_RESEARCH_REFERENCE_DOCX", "").strip()
            if reference_override and Path(reference_override).is_file():
                reference_doc = Path(reference_override)
            else:
                reference_doc = tmp_path / "reference.docx"
                reference_doc.write_bytes(_build_reference_docx_bytes())

            cmd = [
                pandoc_path,
                str(input_md),
                "-f",
                "gfm",
                "-o",
                str(output_docx),
                f"--reference-doc={reference_doc}",
                "--toc",
                "--metadata=lang:zh-CN",
                "--metadata=toc-title:目录",
                f"--metadata=title:{effective_title}",
            ]
            if shift_heading_level:
                cmd.append("--shift-heading-level-by=-1")

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"Pandoc conversion failed: {result.stderr.strip() or 'unknown error'}")
            return output_docx.read_bytes()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DeepResearchReport] Pandoc unavailable, using python-docx fallback: %s", exc)
        return _render_report_docx_with_python(title=effective_title, content=normalized_content)


def _save_report_locally(*, thread_id: str, filename: str, content: bytes) -> Path:
    paths = get_paths()
    paths.ensure_thread_dirs(thread_id)
    output_path = paths.thread_artifacts_dir(thread_id) / filename
    output_path.write_bytes(content)
    return output_path


def _resolve_runtime_thread_id(runtime: ToolRuntime[ContextT, ThreadState]) -> str | None:
    """Resolve internal thread_id from runtime context/config.

    In some LangGraph executions, thread_id is placed under
    runtime.config['configurable']['thread_id'] instead of runtime.context.
    """
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id:
        return str(thread_id)

    config = getattr(runtime, "config", None) or {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
    if thread_id:
        return str(thread_id)

    return None


async def save_deep_research_report_direct(
    *,
    thread_id: str,
    title: str,
    content: str,
    filename: str | None = None,
) -> dict[str, str | None]:
    """Save report without LangGraph runtime, preferring MinIO with local fallback."""
    final_content = content.strip()
    if not final_content:
        final_content = f"# {title.strip() or DEFAULT_DOCX_TITLE}\n\n(No content)"
    # Keep report generation synchronous so fallback logging and file writes
    # remain in the same request context.
    docx_bytes = _render_report_docx_bytes(title=title, content=final_content)

    # If no explicit filename given, derive it from the title so that
    # Chinese titles like "成都理工大学深度研究报告" produce readable filenames
    # instead of falling back to a generic timestamp-based name.
    effective_filename = filename or title
    report_filename = _safe_report_filename(effective_filename)
    user_id, external_thread_id = parse_internal_thread_key(thread_id)

    bucket_name = get_user_minio_bucket_name()
    object_name = (
        f"users/{user_id or 'anonymous'}/threads/{external_thread_id}/deep-research/"
        f"{uuid.uuid4().hex}-{report_filename}"
    )

    try:
        uploaded_object = await upload_bytes_to_minio(
            docx_bytes,
            object_name,
            content_type=DOCX_MIME_TYPE,
            bucket_name=bucket_name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DeepResearchReport] MinIO upload failed, using local fallback: %s", exc)
        uploaded_object = None
    local_path: Path | None = None
    if not uploaded_object:
        local_path = _save_report_locally(
            thread_id=thread_id,
            filename=report_filename,
            content=docx_bytes,
        )

    logger.info(
        "[DeepResearchReport] direct save report: thread_id=%s, artifact_path=%s, minio=%s, local=%s",
        thread_id,
        report_filename,
        uploaded_object,
        local_path,
    )

    return {
        "title": title.strip() or DEFAULT_DOCX_TITLE,
        "artifact_path": report_filename,
        "minio_object": uploaded_object,
    }


@tool("save_deep_research_report", parse_docstring=True)
def save_deep_research_report_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    title: str,
    content: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    filename: str | None = None,
) -> Command:
    """Save a deep research report, upload to MinIO, and expose as artifact.

    Use this after synthesizing research findings so the report can be
    rendered in the right-side artifact panel and shared by URL.

    Args:
        title: Report title.
        content: Full markdown report content.
        filename: Optional report filename. If omitted, auto-generated.
    """
    final_content = content.strip()
    if not final_content:
        final_content = f"# {title.strip() or DEFAULT_DOCX_TITLE}\n\n(No content)"
    docx_bytes = _render_report_docx_bytes(title=title, content=final_content)

    report_filename = _safe_report_filename(filename)

    thread_id = _resolve_runtime_thread_id(runtime)
    if not thread_id:
        return Command(
            update={"messages": [ToolMessage(content="Error: thread_id is not available in runtime context", tool_call_id=tool_call_id)]}
        )

    user_id, external_thread_id = parse_internal_thread_key(thread_id or "") if thread_id else (None, "unknown")

    bucket_name = get_user_minio_bucket_name()
    object_name = (
        f"users/{user_id or 'anonymous'}/threads/{external_thread_id}/deep-research/"
        f"{uuid.uuid4().hex}-{report_filename}"
    )

    try:
        uploaded_object = upload_bytes_to_minio_sync(
            docx_bytes,
            object_name,
            content_type=DOCX_MIME_TYPE,
            bucket_name=bucket_name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DeepResearchReport] MinIO upload failed, using local fallback: %s", exc)
        uploaded_object = None
    local_path: Path | None = None
    if not uploaded_object:
        local_path = _save_report_locally(
            thread_id=thread_id,
            filename=report_filename,
            content=docx_bytes,
        )

    payload = {
        "title": title.strip() or DEFAULT_DOCX_TITLE,
        "artifact_path": report_filename,
        "minio_object": uploaded_object,
    }
    payload_text = json.dumps(payload, ensure_ascii=False)

    logger.info(
        "[DeepResearchReport] saved report: thread_id=%s, artifact_path=%s, minio=%s, local=%s",
        thread_id,
        report_filename,
        uploaded_object,
        local_path,
    )

    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=(
                        "Deep research report saved and presented. "
                        f"Metadata: {payload_text}"
                    ),
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )
