"""HTML PPT 大纲层:用户请求提取、默认大纲生成、大纲块解析与有效性判定。(拆自 html_ppt_tool.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.core.agents.state import ThreadState
from langchain.tools import ToolRuntime
from langgraph.typing import ContextT
from typing import Any
import re
from backend.application.tools.providers.html_ppt_assets import (
    _clean_tool_text_arg,
)

logger = get_logger(__name__)


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


def _resolve_slide_image_source(raw_ref: str) -> str:
    """Resolve a slide image reference to a browser-facing URL.

    Accepts request-scoped ``[IMAGE:xxx]`` whitelist tokens, raw http(s)/API
    URLs, and MinIO object references.  Unresolvable references are skipped so
    a missing image never breaks the deck.
    """

    ref = str(raw_ref or "").strip().strip("\"'")
    if not ref:
        return ""
    token = ref
    if token.startswith("[IMAGE:") and token.endswith("]"):
        from backend.application.agent_executors.image_url_registry import (
            get_registry,
        )

        resolved = get_registry().resolve(token)
        return str(resolved or "").strip()
    if ref.startswith(("http://", "https://", "/api/", "data:")):
        from backend.application.image_pipeline import normalize_image_url

        return normalize_image_url(ref)
    return ""


def _parse_slides_outline(slides_outline: str) -> list[dict[str, Any]]:
    slides: list[dict[str, Any]] = []
    for block in _split_outline_blocks(slides_outline):
        title = ""
        kicker = ""
        notes = ""
        paragraphs: list[str] = []
        bullets: list[str] = []
        images: list[dict[str, str]] = []
        layout = ""

        image_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
        image_directive = re.compile(
            r"^\s*@image\s*:\s*(?P<src>[^\s|]+)(?:\s*\|\s*(?P<caption>.+?))?\s*$",
            flags=re.IGNORECASE,
        )

        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            directive_match = image_directive.match(line)
            if directive_match:
                src = _resolve_slide_image_source(directive_match.group("src"))
                if src:
                    images.append(
                        {
                            "src": src,
                            "caption": (directive_match.group("caption") or "").strip(),
                        }
                    )
                continue
            image_matches = list(image_pattern.finditer(line))
            if image_matches:
                for match in image_matches:
                    src = _resolve_slide_image_source(match.group(2))
                    if src:
                        images.append(
                            {
                                "src": src,
                                "caption": (match.group(1) or "").strip(),
                            }
                        )
                line = image_pattern.sub("", line).strip()
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
        slide: dict[str, Any] = {
            "title": title,
            "kicker": kicker,
            "paragraphs": paragraphs,
            "bullets": bullets,
            "notes": notes,
            "layout": layout,
        }
        if images:
            slide["images"] = images
        slides.append(slide)
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
