"""HTML PPT 渲染层:slide 分类、Markdown 表格/行内渲染、单页与动态整册 HTML 生成。(拆自 html_ppt_tool.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from html import escape
from typing import Any
import re
from backend.application.tools.providers.html_ppt_assets import (
    ASSETS_DIR,
    THEMES_DIR,
    _list_available_themes,
    _read_text_file,
)
from backend.application.tools.providers.html_ppt_outline import (
    _parse_slides_outline,
)

logger = get_logger(__name__)


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


def _render_slide_images(images: list[dict[str, str]]) -> str:
    """Render slide figures from resolved image URLs."""

    if not images:
        return ""
    figures: list[str] = []
    for item in images:
        src = str(item.get("src") or "").strip()
        if not src:
            continue
        caption = escape(str(item.get("caption") or "").strip())
        caption_html = f"<figcaption>{caption}</figcaption>" if caption else ""
        figures.append(
            f'<figure class="slide-figure">'
            f'<img src="{escape(src, quote=True)}" alt="{caption}" loading="lazy">'
            f"{caption_html}</figure>"
        )
    if not figures:
        return ""
    return '<div class="slide-figures">' + "".join(figures) + "</div>"


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
    figures_html = _render_slide_images(list(slide.get("images") or []))
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
    {figures_html}
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
    {figures_html}
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
    {figures_html}
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
    {figures_html}
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
    body_html += figures_html
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
.slide-figures{{
  display:flex;
  flex-wrap:wrap;
  gap:18px;
  margin-top:20px;
  align-items:flex-start;
  justify-content:center;
}}
.slide-figure{{
  margin:0;
  max-width:min(92%,720px);
  padding:10px;
  border:1px solid var(--border);
  border-radius:12px;
  background:var(--surface-1,rgba(255,255,255,.04));
}}
.slide-figure img{{
  display:block;
  max-width:100%;
  height:auto;
  max-height:52vh;
  object-fit:contain;
  border-radius:8px;
}}
.slide-figure figcaption{{
  margin-top:8px;
  font-size:.86em;
  color:var(--text-2,var(--dim));
  text-align:center;
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
