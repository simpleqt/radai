from __future__ import annotations

import json
import logging
import re
import time
import uuid
from html import escape
from pathlib import Path
from typing import Annotated, Any

from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT

from backend.core.agents.state import ThreadState
from backend.core.skills.native_skill_tools import normalize_skill_tool_name
from backend.core.agents.thread_key import parse_internal_thread_key
from backend.storage import get_user_minio_bucket_name, upload_bytes_to_minio_sync

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = BACKEND_ROOT / "skills" / "public" / "html-ppt"
FULL_DECKS_DIR = SKILL_ROOT / "templates" / "full-decks"
GENERIC_DECK_TEMPLATE = SKILL_ROOT / "templates" / "deck.html"
ASSETS_DIR = SKILL_ROOT / "assets"
THEMES_DIR = SKILL_ROOT / "assets" / "themes"
ASSETS_URL_PREFIX = "/static/html-ppt-assets"
HTML_MIME_TYPE = "text/html; charset=utf-8"


def _list_available_templates() -> list[str]:
    return sorted(
        path.name
        for path in FULL_DECKS_DIR.iterdir()
        if path.is_dir() and (path / "index.html").exists()
    )


def _list_available_themes() -> list[str]:
    return sorted(path.stem for path in THEMES_DIR.glob("*.css"))


def _read_text_file(file_path: Path) -> str:
    return file_path.read_text(encoding="utf-8").strip()


def _rewrite_asset_urls(html_content: str) -> str:
    patterns = [
        (r'href="(?:\.\./)+assets/', f'href="{ASSETS_URL_PREFIX}/'),
        (r'src="(?:\.\./)+assets/', f'src="{ASSETS_URL_PREFIX}/'),
        (r'data-theme-base="(?:\.\./)+assets/', f'data-theme-base="{ASSETS_URL_PREFIX}/'),
        (r'href="assets/', f'href="{ASSETS_URL_PREFIX}/'),
        (r'src="assets/', f'src="{ASSETS_URL_PREFIX}/'),
        (r'data-theme-base="assets/', f'data-theme-base="{ASSETS_URL_PREFIX}/'),
    ]
    for pattern, replacement in patterns:
        html_content = re.sub(pattern, replacement, html_content)
    return html_content


def _inline_template_stylesheet(html_content: str, template_dir: Path) -> str:
    style_file = template_dir / "style.css"
    if not style_file.exists():
        return html_content

    style_text = style_file.read_text(encoding="utf-8").strip()
    if not style_text:
        return html_content

    style_block = f"<style data-source=\"template-style\">\n{style_text}\n</style>"
    html_content, replaced = re.subn(
        r'<link[^>]+href="\.?/style\.css"[^>]*>',
        style_block,
        html_content,
        count=1,
    )
    if replaced:
        return html_content
    return html_content.replace("</head>", f"{style_block}\n</head>", 1)


def _ensure_theme_link(html_content: str, theme: str) -> str:
    theme_href = f"{ASSETS_URL_PREFIX}/themes/{theme}.css"

    if 'id="theme-link"' in html_content:
        return re.sub(
            r'(<link[^>]+id="theme-link"[^>]+href=")[^"]*(")',
            rf"\1{theme_href}\2",
            html_content,
            count=1,
        )

    theme_link = f'<link rel="stylesheet" id="theme-link" href="{theme_href}">'
    base_link_match = re.search(r'<link[^>]+href="[^"]*base\.css"[^>]*>', html_content)
    if base_link_match:
        insert_at = base_link_match.end()
        return html_content[:insert_at] + "\n" + theme_link + html_content[insert_at:]
    return html_content.replace("</head>", f"{theme_link}\n</head>", 1)


def _set_title(html_content: str, title: str) -> str:
    safe_title = escape(title, quote=False)
    if re.search(r"<title>.*?</title>", html_content, flags=re.DOTALL):
        return re.sub(
            r"<title>.*?</title>",
            f"<title>{safe_title}</title>",
            html_content,
            count=1,
            flags=re.DOTALL,
        )
    return html_content.replace("</head>", f"<title>{safe_title}</title>\n</head>", 1)


def _strip_demo_slides(html_content: str) -> str:
    html_content = re.sub(r"<body[^>]*>", "", html_content, count=1, flags=re.IGNORECASE | re.DOTALL)
    html_content = re.sub(r"</body>\s*</html>\s*$", "", html_content, count=1, flags=re.IGNORECASE | re.DOTALL)
    html_content = re.sub(
        r"<div class=\"deck\">[\s\S]*?</div>\s*(?:<script[^>]*></script>\s*)*$",
        "",
        html_content,
        count=1,
        flags=re.IGNORECASE,
    )
    return html_content.strip()


def _safe_html_filename(filename: str | None, title: str) -> str:
    raw_name = _clean_tool_text_arg(filename) or _clean_tool_text_arg(title) or "html-ppt"
    raw_name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", raw_name)
    raw_name = re.sub(r"[\s-]+", "-", raw_name).strip(".-")
    if not raw_name:
        raw_name = "html-ppt"
    if raw_name.lower().endswith(".html.html"):
        raw_name = raw_name[:-5]
    if not raw_name.lower().endswith(".html"):
        raw_name = f"{raw_name}.html"
    return raw_name


def _clean_tool_text_arg(raw: Any) -> str:
    """Clean model-emitted free-text tool args without truncating content."""
    text = str(raw or "").strip().strip("\"'`")
    if not text:
        return ""
    text = re.sub(r"</?parameter[^>]*(?:>|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?tool_call[^>]*(?:>|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\r\n?", "\n", text)
    return text.strip()


def _resolve_runtime_thread_id(runtime: ToolRuntime[ContextT, ThreadState]) -> str | None:
    """Resolve internal thread_id from runtime context/config."""
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id:
        return str(thread_id)

    config = getattr(runtime, "config", None) or {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
    if thread_id:
        return str(thread_id)

    return None


def _message_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(part for part in chunks if part).strip()
    return str(content or "").strip()


def _get_latest_user_request(runtime: ToolRuntime[ContextT, ThreadState]) -> str:
    messages = runtime.state.get("messages") if runtime.state else None
    if not isinstance(messages, list):
        return ""

    for message in reversed(messages):
        if getattr(message, "type", None) == "human":
            return _message_to_text(getattr(message, "content", ""))
        if isinstance(message, dict) and message.get("type") == "human":
            return _message_to_text(message.get("content"))
    return ""


def _clean_text_excerpt(text: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _default_outline_for_request(title: str, source_text: str, template: str) -> str:
    clean_title = title.strip() or "项目汇报"
    excerpt = _clean_text_excerpt(source_text or clean_title, limit=140)
    lower_source = f"{clean_title} {source_text}".lower()

    if any(keyword in lower_source for keyword in ["电路", "前端", "csa", "asic", "探测器", "电子学"]):
        return f"""# {clean_title}
{excerpt}
- 聚焦方案目标、关键模块与实现约束
- 输出适合汇报的结构化演示内容
---
# 目录
- 设计背景与需求约束
- 总体架构与信号链路
- 关键电路模块设计
- 风险、验证与后续计划
---
# 设计背景与需求
本页说明方案要解决的问题、应用场景和系统边界。
- 面向核电子学信号读出场景
- 关注低噪声、高增益、宽动态范围与稳定性
- 兼顾可实现性、可调试性与后续扩展
---
# 总体架构与信号链路
本页说明从探测器输入到后端处理之间的主要链路。
- 输入接口与偏置网络
- 电荷灵敏前置放大/成形放大
- 增益与基线控制
- ADC/后端采集接口
---
# 关键模块设计
本页突出最值得汇报的几个核心模块。
- 前端放大级：关注等效噪声与带宽
- 成形与滤波级：平衡计数率和分辨率
- 电源与地设计：抑制串扰与纹波
- 标定与测试接口：便于系统联调
---
# 风险与下一步
本页总结主要风险、验证重点和后续动作。
- 建立噪声、线性度、动态范围验证项
- 明确原理图、仿真、样机测试的阶段目标
- 保留参数可调能力，降低后续迭代成本
---
# 总结
{clean_title} 的核心是围绕性能指标、实现约束和工程可落地性做平衡。
- 方案需要同时兼顾性能、稳定性与可验证性
- 建议按模块分阶段推进设计与测试
"""

    if "weekly-report" in template or any(keyword in lower_source for keyword in ["周报", "进展", "report", "汇报"]):
        return f"""# {clean_title}
{excerpt}
- 本次汇报聚焦阶段成果、风险与下一步计划
---
# 本周重点
- 已完成的关键事项
- 当前最重要的进展变化
- 对目标的整体影响
---
# 关键进展
- 任务推进情况
- 结果与数据表现
- 与预期相比的差异
---
# 风险与问题
- 当前阻塞点
- 资源或依赖风险
- 需要协同支持的事项
---
# 下周计划
- 优先级最高的工作
- 关键里程碑
- 预期输出结果
---
# 总结
- 保持有效推进
- 聚焦关键问题收敛
"""

    return f"""# {clean_title}
{excerpt}
- 围绕主题整理核心问题、关键观点与结论
---
# 目录
- 背景与目标
- 核心内容展开
- 关键要点总结
- 建议与下一步
---
# 背景与目标
本页说明问题背景、目标和适用范围。
- 说明当前需求与目标
- 交代需要解决的核心问题
- 给出分析边界与前提
---
# 核心内容
本页展开最重要的方案、观点或分析内容。
- 提炼 3-4 个最重要的核心点
- 用结构化方式帮助听众快速理解
- 突出关键判断与价值
---
# 关键要点
本页总结执行或落地时最值得关注的事项。
- 关键风险
- 关键收益
- 关键依赖条件
---
# 总结与建议
{clean_title} 需要围绕目标、约束和执行路径综合推进。
- 明确优先级
- 分阶段落地
- 持续验证结果
"""


def _split_outline_blocks(slides_outline: str) -> list[str]:
    normalized = re.sub(r"\r\n?", "\n", str(slides_outline or "")).strip()
    if not normalized:
        return []
    return [block.strip() for block in re.split(r"\n\s*---+\s*\n", normalized) if block.strip()]


def _parse_slides_outline(slides_outline: str) -> list[dict[str, Any]]:
    slides: list[dict[str, Any]] = []
    for block in _split_outline_blocks(slides_outline):
        title = ""
        kicker = ""
        notes = ""
        paragraphs: list[str] = []
        bullets: list[str] = []
        layout = ""

        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if not title:
                    title = line.lstrip("#").strip()
                else:
                    paragraphs.append(line.lstrip("#").strip())
                continue
            if re.match(r"^@(kicker|layout|notes)\s*:", line, flags=re.IGNORECASE):
                key, value = line[1:].split(":", 1)
                key = key.strip().lower()
                value = value.strip()
                if key == "kicker":
                    kicker = value
                elif key == "layout":
                    layout = value.lower()
                elif key == "notes":
                    notes = value
                continue
            if re.match(r"^(备注|讲稿|说明|notes?)\s*[:：]", line, flags=re.IGNORECASE):
                notes = re.split(r"[:：]", line, maxsplit=1)[1].strip()
                continue
            if re.match(r"^[-*]\s+", line) or re.match(r"^\d+\.\s+", line):
                bullets.append(re.sub(r"^([-*]|\d+\.)\s+", "", line).strip())
                continue
            paragraphs.append(line)

        title = title or (paragraphs.pop(0) if paragraphs else "未命名页面")
        slides.append(
            {
                "title": title,
                "kicker": kicker,
                "paragraphs": paragraphs,
                "bullets": bullets,
                "notes": notes,
                "layout": layout,
            }
        )
    return slides


def _outline_is_too_weak(slides_outline: str) -> bool:
    """Detect malformed/over-truncated outlines before rendering a broken deck."""
    outline = str(slides_outline or "").strip()
    if len(outline) < 80:
        return True
    slides = _parse_slides_outline(outline)
    if len(slides) < 3:
        return True
    meaningful_chars = sum(
        len(str(slide.get("title") or ""))
        + sum(len(str(item or "")) for item in slide.get("paragraphs") or [])
        + sum(len(str(item or "")) for item in slide.get("bullets") or [])
        for slide in slides
    )
    return meaningful_chars < 120


def _resolve_effective_outline(
    *,
    title: str,
    source_text: str,
    template: str,
    slides_outline: str | None,
) -> str:
    effective_outline = _clean_tool_text_arg(slides_outline)
    if not effective_outline or _outline_is_too_weak(effective_outline):
        if effective_outline:
            logger.warning(
                "[HTML-PPT] slides_outline 过短或结构不足，改用默认大纲: length=%d preview=%r",
                len(effective_outline),
                _clean_text_excerpt(effective_outline, limit=80),
            )
        effective_outline = _default_outline_for_request(
            title=title,
            source_text=source_text,
            template=template,
        )
    return effective_outline


def _slide_kind(index: int, total: int, slide: dict[str, Any]) -> str:
    explicit = str(slide.get("layout") or "").strip().lower()
    if explicit:
        return explicit

    title = str(slide.get("title") or "").lower()
    if index == 1:
        return "cover"
    if index == total and re.search(r"(q&a|thanks|thank you|总结|结语|问答|谢谢)", title):
        return "closing"
    if re.search(r"(目录|agenda|outline|路线图)", title):
        return "agenda"
    if index == total:
        return "closing"

    bullet_count = len(slide.get("bullets") or [])
    paragraph_count = len(slide.get("paragraphs") or [])
    if bullet_count >= 4:
        return "cards"
    if bullet_count >= 1 and paragraph_count >= 1:
        return "split"
    return "bullets"


def _is_md_table_row(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2


def _is_md_table_separator(text: str) -> bool:
    if not _is_md_table_row(text):
        return False
    inner = text.strip()[1:-1]
    return all(c in "-|: " for c in inner)


def _render_md_table_html(rows: list[str]) -> str:
    separator_indices: set[int] = set()
    for i, row in enumerate(rows):
        if _is_md_table_separator(row):
            separator_indices.add(i)

    if not separator_indices:
        first_sep = None
    else:
        first_sep = min(separator_indices)

    html_rows: list[str] = []
    for i, row in enumerate(rows):
        if i in separator_indices:
            continue
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        is_header = first_sep is not None and i < first_sep
        tag = "th" if is_header else "td"
        cell_html = "".join(
            f"<{tag}>{_render_inline_markdown(cell)}</{tag}>" for cell in cells
        )
        html_rows.append(f"<tr>{cell_html}</tr>")

    return (
        '<div class="table-wrapper">'
        '<table class="md-table">'
        + "".join(html_rows)
        + "</table></div>"
    )


def _render_inline_markdown(text: str) -> str:
    source = str(text or "")
    if not source:
        return ""

    placeholders: dict[str, str] = {}

    def stash(value: str) -> str:
        token = f"__HTMLPPT_TOKEN_{len(placeholders)}__"
        placeholders[token] = value
        return token

    # Do not surface raw markdown image URLs in PPT text. Keep only a compact caption.
    source = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        lambda m: stash(
            f'<span class="image-ref">[图片：{escape((m.group(1) or "未命名图片").strip() or "未命名图片")}]</span>'
        ),
        source,
    )
    source = re.sub(
        r"(?<!!)\[([^\]]+)\]\(([^)]+)\)",
        lambda m: stash(f'<span class="link-ref">{escape((m.group(1) or "").strip() or "链接")}</span>'),
        source,
    )

    # Protect code and math first so markdown emphasis parsing won't break them.
    source = re.sub(
        r"`([^`]+)`",
        lambda m: stash(f"<code>{escape(m.group(1))}</code>"),
        source,
    )
    source = re.sub(
        r"\$\$([\s\S]+?)\$\$",
        lambda m: stash(f'<span class="math-block">\\[{escape(m.group(1).strip())}\\]</span>'),
        source,
    )
    source = re.sub(
        r"(?<!\$)\$([^\n$]+?)\$(?!\$)",
        lambda m: stash(f'<span class="math-inline">\\({escape(m.group(1).strip())}\\)</span>'),
        source,
    )

    escaped = escape(source)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", escaped)

    for token, value in placeholders.items():
        escaped = escaped.replace(escape(token), value)
        escaped = escaped.replace(token, value)

    return escaped


def _render_text_block(text: str, *, css_class: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    block_math_match = re.fullmatch(r"\$\$([\s\S]+?)\$\$", raw)
    if block_math_match:
        formula = escape(block_math_match.group(1).strip())
        return f'<div class="{css_class} math-block-wrapper">\\[{formula}\\]</div>'

    rendered = _render_inline_markdown(raw)
    return f'<p class="{css_class}">{rendered}</p>'


def _render_paragraphs(paragraphs: list[str]) -> str:
    if not paragraphs:
        return ""
    rendered: list[str] = []
    i = 0
    first_prose = True
    while i < len(paragraphs):
        paragraph = paragraphs[i]
        if _is_md_table_row(paragraph):
            table_rows: list[str] = []
            while i < len(paragraphs) and _is_md_table_row(paragraphs[i]):
                table_rows.append(paragraphs[i])
                i += 1
            rendered.append(_render_md_table_html(table_rows))
            continue
        css_class = "lede" if first_prose else "dim mt-m"
        first_prose = False
        rendered.append(_render_text_block(paragraph, css_class=css_class))
        i += 1
    return "\n".join(rendered)


def _render_bullet_list(bullets: list[str], *, css_class: str = "stack mt-l") -> str:
    if not bullets:
        return ""
    items = "\n".join(
        f'<li class="card card-accent"><span class="dim">{_render_inline_markdown(item)}</span></li>'
        for item in bullets
    )
    return f'<ul class="{css_class}" style="list-style:none;padding:0;margin:0">{items}</ul>'


def _render_slide_html(index: int, total: int, slide: dict[str, Any], deck_title: str) -> str:
    title = escape(str(slide.get("title") or f"Slide {index}"))
    kicker_text = str(slide.get("kicker") or "").strip() or f"{deck_title} · {index:02d}"
    kicker = f'<p class="kicker">{escape(kicker_text)}</p>'
    notes = str(slide.get("notes") or "").strip()
    notes_html = f'<div class="notes">{_render_inline_markdown(notes)}</div>' if notes else ""
    footer = (
        f'<div class="deck-footer"><span class="dim2">{escape(deck_title)}</span>'
        f'<span class="slide-number" data-current="{index}" data-total="{total}"></span></div>'
    )
    paragraphs = list(slide.get("paragraphs") or [])
    bullets = list(slide.get("bullets") or [])
    kind = _slide_kind(index, total, slide)

    if kind == "cover":
        lead = _render_inline_markdown(paragraphs[0]) if paragraphs else "围绕主题整理核心观点与关键信息。"
        bullet_tags = ""
        if bullets:
            bullet_tags = '<div class="row wrap mt-m">' + "".join(
                f'<span class="pill pill-accent">{_render_inline_markdown(item)}</span>' for item in bullets[:4]
            ) + "</div>"
        return f"""
  <section class="slide" data-title="{title}">
    {kicker}
    <h1 class="h1">{title}</h1>
    <p class="lede mt-m">{lead}</p>
    {bullet_tags}
    {footer}
    {notes_html}
  </section>""".rstrip()

    if kind == "agenda":
        agenda_rows = "".join(
            f'<div class="agenda-row"><span class="num">{i:02d}</span><span class="t">{_render_inline_markdown(item)}</span><span class="d">section</span></div>'
            for i, item in enumerate(bullets[:6], start=1)
        )
        if not agenda_rows:
            agenda_rows = '<div class="card mt-l"><p class="dim">本页用于概览整份演示的结构安排。</p></div>'
        return f"""
  <section class="slide" data-title="{title}">
    {kicker}
    <h2 class="h2">{title}</h2>
    {_render_paragraphs(paragraphs)}
    <div class="stack mt-l">{agenda_rows}</div>
    {footer}
    {notes_html}
  </section>""".rstrip()

    if kind == "cards":
        columns = 3 if len(bullets) <= 3 else 2
        cards = "".join(
            f'<div class="card card-accent"><h4>{i:02d}</h4><p class="dim">{_render_inline_markdown(item)}</p></div>'
            for i, item in enumerate(bullets, start=1)
        )
        return f"""
  <section class="slide" data-title="{title}">
    {kicker}
    <h2 class="h2">{title}</h2>
    {_render_paragraphs(paragraphs)}
    <div class="grid g{columns} mt-l">{cards}</div>
    {footer}
    {notes_html}
  </section>""".rstrip()

    if kind == "split":
        left = _render_paragraphs(paragraphs)
        right_items = "".join(
            f'<div class="card card-accent"><p class="dim">{_render_inline_markdown(item)}</p></div>'
            for item in bullets
        )
        return f"""
  <section class="slide" data-title="{title}">
    {kicker}
    <h2 class="h2">{title}</h2>
    <div class="grid g2 mt-l" style="align-items:start">
      <div>{left}</div>
      <div class="stack">{right_items}</div>
    </div>
    {footer}
    {notes_html}
  </section>""".rstrip()

    if kind == "closing":
        summary = _render_inline_markdown(paragraphs[0]) if paragraphs else "谢谢观看，欢迎交流。"
        bullet_tags = ""
        if bullets:
            bullet_tags = '<div class="row wrap mt-l" style="justify-content:center">' + "".join(
                f'<span class="pill">{_render_inline_markdown(item)}</span>' for item in bullets[:4]
            ) + "</div>"
        return f"""
  <section class="slide center tc" data-title="{title}">
    <div>
      {kicker}
      <h2 class="h2">{title}</h2>
      <p class="lede" style="margin:16px auto">{summary}</p>
      {bullet_tags}
    </div>
    {footer}
    {notes_html}
  </section>""".rstrip()

    bullet_cards = "".join(
        f'<div class="card card-accent"><p class="dim">{_render_inline_markdown(item)}</p></div>'
        for item in bullets
    )
    body_html = _render_paragraphs(paragraphs)
    if bullet_cards:
        body_html += f'\n<div class="stack mt-l">{bullet_cards}</div>'
    return f"""
  <section class="slide" data-title="{title}">
    {kicker}
    <h2 class="h2">{title}</h2>
    {body_html}
    {footer}
    {notes_html}
  </section>""".rstrip()


def _render_dynamic_html_deck(
    template: str,
    theme: str,
    title: str,
    slides_outline: str,
) -> str:
    slides = _parse_slides_outline(slides_outline)
    if not slides:
        raise ValueError("slides_outline is empty after parsing")
    logger.info("[HTML-PPT] 解析完成: slides=%d outline_length=%d", len(slides), len(slides_outline))

    fonts_css = _read_text_file(ASSETS_DIR / "fonts.css")
    base_css = _read_text_file(ASSETS_DIR / "base.css")
    animations_css = _read_text_file(ASSETS_DIR / "animations" / "animations.css")
    theme_css = _read_text_file(THEMES_DIR / f"{theme}.css")
    runtime_js = _read_text_file(ASSETS_DIR / "runtime.js")
    theme_names = ",".join(_list_available_themes())

    deck_html = "\n".join(
        _render_slide_html(index, len(slides), slide, title.strip() or "HTML PPT")
        for index, slide in enumerate(slides, start=1)
    )

    safe_title = escape(title, quote=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="{escape(theme)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title>
<script>
window.MathJax = {{
  tex: {{
    inlineMath: [['\\\\(', '\\\\)']],
    displayMath: [['\\\\[', '\\\\]']],
    processEscapes: true
  }},
  options: {{
    skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
  }},
  chtml: {{
    scale: 0.95
  }}
}};
</script>
<script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>
<style data-source="fonts">
{fonts_css}
</style>
<style data-source="base">
{base_css}
</style>
<style data-source="animations">
{animations_css}
</style>
<style id="theme-inline" data-theme-name="{escape(theme)}">
{theme_css}
</style>
<style data-source="markdown-math">
.slide strong{{font-weight:800;color:var(--text-1)}}
.slide em{{font-style:italic}}
.slide del{{text-decoration:line-through}}
.slide code{{
  font-family:var(--font-mono);
  background:var(--surface-2);
  border:1px solid var(--border);
  border-radius:8px;
  padding:2px 8px;
  font-size:.92em;
}}
.image-ref,
.link-ref{{
  color:var(--accent);
  font-weight:600;
}}
.math-block-wrapper,
.math-block{{
  display:block;
  margin-top:18px;
  margin-bottom:18px;
  overflow-wrap:anywhere;
}}
.math-inline{{
  display:inline-block;
  vertical-align:middle;
  max-width:100%;
}}
.MathJax{{
  outline:0;
}}
.table-wrapper{{
  overflow-x:auto;
  margin-top:16px;
  border-radius:10px;
  border:1px solid var(--border);
}}
.md-table{{
  width:100%;
  border-collapse:collapse;
  font-size:.88em;
  line-height:1.5;
}}
.md-table th,
.md-table td{{
  padding:8px 14px;
  text-align:left;
  border-bottom:1px solid var(--border);
  border-right:1px solid var(--border);
  color:var(--text-1);
}}
.md-table th:last-child,
.md-table td:last-child{{
  border-right:none;
}}
.md-table tr:last-child td{{
  border-bottom:none;
}}
.md-table th{{
  background:var(--surface-2);
  font-weight:700;
  color:var(--accent);
  letter-spacing:.02em;
}}
.md-table tr:nth-child(even) td{{
  background:var(--surface-1,rgba(255,255,255,.03));
}}
</style>
</head>
<body data-themes="{escape(theme_names)}">
<div class="deck">
{deck_html}
</div>
<script>
{runtime_js}
</script>
</body>
</html>"""


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
            block may contain a slide title, paragraphs, bullet items, and notes.
    """
    template = normalize_skill_tool_name(template)
    theme = normalize_skill_tool_name(theme)
    title = _clean_tool_text_arg(title) or "HTML PPT"
    filename = _clean_tool_text_arg(filename) if filename else None
    slides_outline = _clean_tool_text_arg(slides_outline) if slides_outline else None
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

    logger.info(f"[HTML-PPT] 开始上传到 MinIO...")
    upload_start = time.time()
    uploaded_object = upload_bytes_to_minio_sync(
        html_content.encode("utf-8"),
        object_name,
        content_type=HTML_MIME_TYPE,
        bucket_name=get_user_minio_bucket_name(),
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
