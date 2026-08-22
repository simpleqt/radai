

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.application.tools.execution import invoke_sync_with_timeout
from backend.application.tools.runtime_resolution import resolve_runtime_thread_id as _resolve_runtime_thread_id
from backend.core.agents.state import ThreadState
from backend.core.skills.native_skill_tools import normalize_skill_tool_name
from backend.core.thread_key import parse_internal_thread_key
from backend.storage import get_user_minio_bucket_name
from backend.storage import upload_bytes_to_minio_sync
from langchain.tools import InjectedToolCallId
from langchain.tools import ToolRuntime
from langchain.tools import tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT
from typing import Annotated
import json
import os
import time
import uuid

from backend.application.tools.providers.html_ppt_assets import (  # noqa: F401  (兼容 re-export)
    ASSETS_DIR,
    ASSETS_URL_PREFIX,
    BACKEND_ROOT,
    FULL_DECKS_DIR,
    GENERIC_DECK_TEMPLATE,
    HTML_MIME_TYPE,
    SKILL_ROOT,
    THEMES_DIR,
    _clean_tool_text_arg,
    _list_available_templates,
    _list_available_themes,
    _read_text_file,
    _safe_html_filename,
)
from backend.application.tools.providers.html_ppt_outline import (  # noqa: F401  (兼容 re-export)
    _clean_text_excerpt,
    _default_outline_for_request,
    _get_latest_user_request,
    _message_to_text,
    _outline_is_too_weak,
    _parse_slides_outline,
    _resolve_effective_outline,
    _resolve_slide_image_source,
    _split_outline_blocks,
)
from backend.application.tools.providers.html_ppt_render import (  # noqa: F401  (兼容 re-export)
    _is_md_table_row,
    _is_md_table_separator,
    _render_dynamic_html_deck,
    _render_inline_markdown,
    _render_md_table_html,
    _render_paragraphs,
    _render_slide_html,
    _render_slide_images,
    _render_text_block,
    _slide_kind,
)

logger = get_logger(__name__)


def _outline_char_limit() -> int:
    """Bound the model-controlled slide outline to protect memory/temp/MinIO."""

    try:
        return max(10_000, int(os.getenv("HTML_PPT_OUTLINE_MAX_CHARS", "1000000")))
    except (TypeError, ValueError):
        return 1_000_000


def _build_artifact_identity(
    runtime: ToolRuntime[ContextT, ThreadState],
    filename: str | None,
    title: str,
) -> tuple[str, str]:
    thread_id = str(_resolve_runtime_thread_id(runtime) or "").strip()
    if not thread_id:
        raise ValueError("thread_id is not available in runtime context")

    user_id, external_thread_id = parse_internal_thread_key(thread_id)
    artifact_name = _safe_html_filename(filename, title)
    artifact_path = f"html-ppt/{artifact_name}"
    object_name = (
        f"users/{user_id or 'anonymous'}/threads/{external_thread_id}/html-ppt/"
        f"{uuid.uuid4().hex}-{artifact_name}"
    )
    return artifact_path, object_name


def _render_html_deck(
    runtime: ToolRuntime[ContextT, ThreadState],
    template: str,
    theme: str,
    title: str,
    slides_outline: str | None,
) -> str:
    template = normalize_skill_tool_name(template)
    theme = normalize_skill_tool_name(theme)
    available_templates = _list_available_templates()
    available_themes = _list_available_themes()

    if template not in available_templates:
        raise ValueError(
            f"Unknown template '{template}'. Available templates: {', '.join(available_templates)}"
        )
    if theme not in available_themes:
        raise ValueError(
            f"Unknown theme '{theme}'. Available themes: {', '.join(available_themes)}"
        )

    effective_outline = _resolve_effective_outline(
        title=title,
        source_text=_get_latest_user_request(runtime),
        template=template,
        slides_outline=slides_outline,
    )

    return _render_dynamic_html_deck(
        template=template,
        theme=theme,
        title=title,
        slides_outline=effective_outline,
    )


@tool("generate_html_ppt", parse_docstring=True)
def generate_html_ppt_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    template: str,
    theme: str,
    title: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    filename: str | None = None,
    slides_outline: str | None = None,
) -> Command:
    """Generate an HTML PPT deck and save it through the system artifact pipeline.

    Use this tool to generate a previewable HTML PPT artifact backed by MinIO.

    Args:
        template: Full-deck template name, such as `tech-sharing` or `weekly-report`.
        theme: Theme name, such as `tokyo-night` or `aurora`.
        title: Document title for the generated deck.
        filename: Optional artifact filename, such as `team-share.html`.
        slides_outline: Optional slide outline separated by `---` blocks. Each
            block may contain a slide title, paragraphs, bullet items, notes,
            and figures. Embed a figure with `![图题]([IMAGE:001])` (whitelist
            token from retrieval) or `![图题](https://.../image.png)`, or use a
            dedicated `@image: <token|url> | 图题` line. Reuse original figures
            from the knowledge base whenever they exist.
    """
    template = normalize_skill_tool_name(template)
    theme = normalize_skill_tool_name(theme)
    title = _clean_tool_text_arg(title) or "HTML PPT"
    filename = _clean_tool_text_arg(filename) if filename else None
    slides_outline = _clean_tool_text_arg(slides_outline) if slides_outline else None
    if slides_outline:
        outline_limit = _outline_char_limit()
        if len(slides_outline) > outline_limit:
            logger.warning(
                "[HTML-PPT] slides_outline 超出上限 %d (%d)，已截断",
                outline_limit,
                len(slides_outline),
            )
            slides_outline = slides_outline[:outline_limit]
    logger.info(f"[HTML-PPT] 开始生成 PPT: title={title}, template={template}, theme={theme}, filename={filename}")
    if slides_outline:
        logger.info(f"[HTML-PPT] Slides Outline 长度: {len(slides_outline)}")
    else:
        logger.info("[HTML-PPT] Slides Outline 为空，将使用默认模板")

    try:
        artifact_path, object_name = _build_artifact_identity(runtime, filename, title)
        logger.info(f"[HTML-PPT] 资源标识构建完成: artifact_path={artifact_path}, object_name={object_name}")

        start_time = time.time()
        html_content = _render_html_deck(runtime, template, theme, title, slides_outline)
        render_duration = (time.time() - start_time) * 1000
        logger.info(f"[HTML-PPT] HTML 渲染完成，耗时: {render_duration:.2f}ms, 长度: {len(html_content)}")
    except ValueError as exc:
        logger.error(f"[HTML-PPT] 生成失败 (ValueError): {exc}")
        return Command(
            update={
                "messages": [
                    ToolMessage(content=f"Error: {exc}", tool_call_id=tool_call_id),
                ]
            }
        )
    except Exception as exc:
        logger.error(f"[HTML-PPT] 生成失败 (未知异常): {exc}", exc_info=True)
        return Command(
            update={
                "messages": [
                    ToolMessage(content=f"Error: {exc}", tool_call_id=tool_call_id),
                ]
            }
        )

    logger.info("[HTML-PPT] 开始上传到 MinIO...")
    upload_start = time.time()
    try:
        uploaded_object = invoke_sync_with_timeout(
            lambda: upload_bytes_to_minio_sync(
                html_content.encode("utf-8"),
                object_name,
                content_type=HTML_MIME_TYPE,
                bucket_name=get_user_minio_bucket_name(),
            ),
            timeout=35,
            label="html_ppt.upload",
        )
    except Exception as exc:
        logger.exception("[HTML-PPT] 上传 MinIO 异常")
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=f"Error: failed to upload HTML PPT: {exc}",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
    upload_duration = (time.time() - upload_start) * 1000

    if not uploaded_object:
        logger.error("[HTML-PPT] 上传 MinIO 失败")
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="Error: failed to upload HTML PPT to MinIO",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    logger.info(f"[HTML-PPT] 上传完成，耗时: {upload_duration:.2f}ms, object={uploaded_object}")

    payload = {
        "title": title.strip() or "HTML PPT",
        "artifact_path": artifact_path,
        "minio_object": uploaded_object,
        "template": template,
        "theme": theme,
    }
    payload_text = json.dumps(payload, ensure_ascii=False)

    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=f"HTML PPT generated successfully. Metadata: {payload_text}",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )
