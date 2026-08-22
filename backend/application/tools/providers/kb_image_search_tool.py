"""KB image search tool (HTTP client for the omnigraph knowledge service).

Retrieval (media embedding + per-KB media chunk search) executes inside
omnigraph; this module only registers returned image URLs into the current
request's image whitelist and renders the whitelist for the model.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from backend.application.agent_executors.image_url_registry import get_registry
from backend.application.knowledge.internal_client import (
    KnowledgeInternalError,
    internal_image_search,
)

logger = logging.getLogger(__name__)

try:
    KB_IMAGE_SEARCH_TIMEOUT_SECONDS = max(
        5.0,
        float(os.getenv("KB_IMAGE_SEARCH_TIMEOUT_SECONDS", "45") or "45"),
    )
except (TypeError, ValueError):
    KB_IMAGE_SEARCH_TIMEOUT_SECONDS = 45.0
KB_IMAGE_SEARCH_TOTAL_TIMEOUT_SECONDS = KB_IMAGE_SEARCH_TIMEOUT_SECONDS + 15.0

_MAX_WHITELIST_ENTRIES = 8


def _render_image_whitelist(entries: list[dict[str, str]]) -> str:
    if not entries:
        return ""
    lines: list[str] = []
    for index, entry in enumerate(entries[:_MAX_WHITELIST_ENTRIES], start=1):
        desc_parts: list[str] = []
        if entry["caption"]:
            desc_parts.append(f"图注：{entry['caption']}")
        if entry.get("media_ref") and entry["media_ref"] not in entry["caption"]:
            desc_parts.append(f"图号：{entry['media_ref']}")
        if entry.get("visual"):
            desc_parts.append(f"内容：{entry['visual']}")
        if entry.get("source"):
            desc_parts.append(f"来源：{entry['source']}")
        if not desc_parts:
            desc_parts.append(f"图片{index}")
        lines.append(f"- {entry['token']} | {' | '.join(desc_parts)}")
    return (
        "## 知识库图片检索结果（本轮图片白名单）\n\n"
        + "\n".join(lines)
        + "\n\n> **规则**：只能引用上方列出的 `[IMAGE:xxx]`；"
        "引用时使用 `![图片说明]([IMAGE:xxx])`，并附图注与来源；"
        "严禁输出 MinIO 内网地址或未在白名单中的 URL。"
        "\n\n可直接引用示例：\n"
        + "\n".join(
            f"- `![{entry['caption'] or f'图{i+1}'}]({entry['token']})`"
            for i, entry in enumerate(entries[:3])
        )
    )


@tool("search_kb_images", parse_docstring=True)
async def search_kb_images_tool(
    query: str,
    kb_ids: Annotated[list[str] | None, InjectedToolArg] = None,
    top_k: int = 8,
    config: RunnableConfig = None,
) -> str:
    """Search the authorized knowledge bases for original images (figures, tables, formulas).

    Use this tool when the question or the requested output needs an original
    figure from the knowledge base (e.g. 受力图、内力图、弯矩图) and the
    retrieved text context does not already contain the needed image.  The
    returned images are registered in the current image whitelist and can be
    embedded in the answer or in a PPT slides_outline via
    `![图题]([IMAGE:xxx])` or `@image: <token>`.

    Args:
        query: Image content or figure description to search, e.g. `简支梁受力图`.
        kb_ids: Knowledge-base IDs; when omitted, the agent's default/selected
            knowledge bases are used.
        top_k: Maximum number of media chunks to return per knowledge base.
    """
    registry = None
    if config and isinstance(config, dict):
        registry = config.get("configurable", {}).get("image_url_registry")
    if registry is None:
        registry = get_registry()

    query = str(query or "").strip()
    if not query:
        return "query 不能为空。"
    top_k = max(1, min(int(top_k or 8), 30))

    effective_kb_ids = (
        [str(kb).strip() for kb in (kb_ids or []) if str(kb).strip()] or None
    )
    if effective_kb_ids is None and config and isinstance(config, dict):
        configured_ids = (
            config.get("configurable", {}).get("knowledge_base_ids")
            or config.get("configurable", {}).get("kb_ids")
        )
        if isinstance(configured_ids, (list, tuple)) and configured_ids:
            effective_kb_ids = [str(kb) for kb in configured_ids]

    try:
        response = await asyncio.wait_for(
            internal_image_search(
                query=query,
                kb_ids=effective_kb_ids,
                top_k=top_k,
            ),
            timeout=KB_IMAGE_SEARCH_TOTAL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[search_kb_images] 知识服务图片检索总超时 (%.1fs)",
            KB_IMAGE_SEARCH_TOTAL_TIMEOUT_SECONDS,
        )
        return "图片检索超时，请稍后重试或换用更具体的图片描述。"
    except KnowledgeInternalError as exc:
        logger.warning("[search_kb_images] 知识服务图片检索失败: %s", exc)
        return "图片检索服务暂时不可用，请稍后重试。"

    entries: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in response.get("results") or []:
        url = str(item.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        caption = str(item.get("caption") or "").strip()
        token = registry.register(url, caption or "图片")
        source = str(item.get("source") or "").strip()
        kb_name = str(item.get("kb_name") or "").strip()
        if kb_name and source:
            source = f"{kb_name}｜{source}"
        elif kb_name:
            source = kb_name
        entries.append(
            {
                "token": str(token),
                "url": url,
                "caption": caption,
                "media_ref": str(item.get("media_ref") or "").strip(),
                "visual": str(item.get("visual") or "").strip()[:120],
                "source": source,
            }
        )

    whitelist = _render_image_whitelist(entries)
    if not whitelist:
        return (
            "未检索到相关原图；若图示对理解必要，可调用 "
            "`generate_mechanics_diagram` 生成示意受力图/内力图。"
        )
    return whitelist


__all__ = ["search_kb_images_tool"]
