from __future__ import annotations

from backend.application.agent_executors.image_url_registry import (
    get_registry,
    reset_registry,
)
from backend.application.tools.providers.html_ppt_tool import (
    _parse_slides_outline,
    _render_slide_html,
    _render_slide_images,
)


def _with_registered_image(url: str, caption: str = "图1") -> str:
    reset_registry()
    return get_registry().register(url, caption)


def test_outline_parses_markdown_image_token() -> None:
    token = _with_registered_image("/api/v1/charts/public/abc.png?exp=1&sig=s", "图1")
    outline = f"""# 受力分析
- 画出受力图
![图1]({token})
---
# 下一页
正文内容
"""
    slides = _parse_slides_outline(outline)

    assert slides[0]["title"] == "受力分析"
    assert slides[0]["images"][0]["src"] == "/api/v1/charts/public/abc.png?exp=1&sig=s"
    assert slides[0]["images"][0]["caption"] == "图1"
    assert "![" not in "\n".join(slides[0]["paragraphs"])
    assert slides[1]["title"] == "下一页"


def test_outline_parses_image_directive() -> None:
    outline = """# 弯矩图
@image: https://example.com/m.png | 弯矩图示意
- 最大弯矩 10 kN·m
"""
    slides = _parse_slides_outline(outline)

    assert slides[0]["images"] == [
        {"src": "https://example.com/m.png", "caption": "弯矩图示意"}
    ]
    assert slides[0]["bullets"] == ["最大弯矩 10 kN·m"]


def test_outline_skips_unresolved_image_token() -> None:
    reset_registry()
    outline = """# 受力分析
![未知图]([IMAGE:999])
正文
"""
    slides = _parse_slides_outline(outline)

    assert "images" not in slides[0]
    assert "正文" in slides[0]["paragraphs"]


def test_render_slide_images_emits_figure() -> None:
    html = _render_slide_images(
        [{"src": "https://example.com/a.png", "caption": "剪力图"}]
    )

    assert 'class="slide-figure"' in html
    assert 'src="https://example.com/a.png"' in html
    assert "<figcaption>剪力图</figcaption>" in html


def test_render_slide_html_includes_figure() -> None:
    slide = {
        "title": "内力图",
        "kicker": "",
        "paragraphs": ["按微分关系推导"],
        "bullets": [],
        "notes": "",
        "layout": "",
        "images": [{"src": "/api/v1/charts/u/1.png?exp=1&sig=x", "caption": "弯矩图"}],
    }
    html = _render_slide_html(1, 2, slide, "基础力学")

    assert 'class="slide-figure"' in html
    assert 'src="/api/v1/charts/u/1.png?exp=1&amp;sig=x"' in html
    assert "按微分关系推导" in html
