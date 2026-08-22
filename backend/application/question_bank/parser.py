"""docx 题库模板解析：固定标记切分 + 逐题校验。

模板纪律（与教师模板说明一致）：
- 每题一个块，字段用 ``【题型】【题干】【选项】【答案】【解析】【难度】【知识点】【章节】【来源】``
  固定标记开头；题与题之间用独立 ``====`` 分隔行隔开；
- 受力图/结构简图直接内嵌在题干位置，解析时按文档顺序锚定到所属题目；
- 答案/解析必须教师自填，解析器只做结构化，不生成不推断。

两条提取路径产出同一种「行序列」再统一切分：
1. pandoc（``docx→markdown``，OMML 公式自动转 LaTeX、图片导出 media/）——生产可用，
   失败自动回退；
2. python-docx 逐段提取（图片经 ``a:blip`` 关系锚定）——任何环境可用的兜底，
   也是单测的确定性路径。

校验分级：error 题不入库（缺题干/缺答案/题型无法识别），warning 由教师在预览
界面修正后放行。图片字节经 ``images_payload`` 返回，由服务层落 MinIO 并以稳定
object_name 替换占位符——解析阶段不产生持久化副作用以外的身份信息。
"""

from __future__ import annotations

import io
import os
import re
import uuid
from dataclasses import dataclass, field

from backend.config.loggers import get_logger

logger = get_logger(__name__)

MAX_QUESTIONS_PER_BATCH = 500
MAX_IMAGES_PER_QUESTION = 6

QUESTION_TYPES = ("选择题", "填空题", "判断题", "计算题", "作图题", "简答题")

_TYPE_ALIASES = {
    "选择题": "选择题",
    "单选题": "选择题",
    "多选题": "选择题",
    "填空题": "填空题",
    "填空": "填空题",
    "判断题": "判断题",
    "判断": "判断题",
    "计算题": "计算题",
    "计算": "计算题",
    "作图题": "作图题",
    "作图": "作图题",
    "简答题": "简答题",
    "简答": "简答题",
}

_DIFFICULTY_ALIASES = {
    "简单": "easy",
    "容易": "easy",
    "易": "easy",
    "easy": "easy",
    "中等": "medium",
    "一般": "medium",
    "medium": "medium",
    "较难": "hard",
    "难": "hard",
    "困难": "hard",
    "hard": "hard",
}

_MARKER_RE = re.compile(r"^【(题型|题干|选项|答案|解析|难度|知识点|章节|来源)】\s*(.*)$")
_SEPARATOR_RE = re.compile(r"^\s*={3,}\s*$")
_OPTION_START_RE = re.compile(r"^([A-H])\s*[.、．:：]\s*(.*)$")
_OPTION_SPLIT_RE = re.compile(r"\s*(?=[A-H][.、．])")
_KP_SPLIT_RE = re.compile(r"[;；,，、]")
_IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp)$", re.IGNORECASE)
# pandoc markdown 行内/独立图片：![alt](path)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

_MARKER_FIELDS = ("题型", "题干", "选项", "答案", "解析", "难度", "知识点", "章节", "来源")


@dataclass
class _Line:
    text: str = ""
    image_bytes: bytes | None = None
    image_ext: str = "png"


@dataclass
class ParsedImage:
    placeholder_id: str
    ext: str
    data: bytes


@dataclass
class ParsedQuestion:
    question_type: str | None = None
    stem: str = ""
    options: list[dict[str, str]] = field(default_factory=list)
    answer: str = ""
    analysis: str = ""
    score: str = ""
    difficulty: str | None = None
    chapter: str = ""
    knowledge_points: list[str] = field(default_factory=list)
    source_name: str = ""
    images: list[ParsedImage] = field(default_factory=list)
    issues: list[dict[str, str]] = field(default_factory=list)

    def issue(self, level: str, message: str) -> None:
        self.issues.append({"level": level, "message": message})

    @property
    def has_error(self) -> bool:
        return any(item["level"] == "error" for item in self.issues)


@dataclass
class ParseResult:
    questions: list[ParsedQuestion] = field(default_factory=list)
    parser_path: str = "python-docx"

    @property
    def valid_count(self) -> int:
        return sum(1 for question in self.questions if not question.has_error)


# ── 行序列提取 ────────────────────────────────────────────────────────────


# 与 qb-image 存储白名单（images.build_object_name）严格对齐的格式；
# bmp/svg 看似浏览器可渲染，但存储层与签名代理的 MIME 表不支持它们，
# 放行只会被静默改标 .png 裂图——一律剔除并记 warning 提示转存。
_SUPPORTED_IMAGE_EXTS = {"png", "jpg", "gif", "webp"}


def _ext_from_name(name: str) -> str:
    raw = str(name or "")
    match = _IMAGE_EXT_RE.search(raw)
    if match:
        ext = match.group(1).lower()
        return "jpg" if ext == "jpeg" else ext
    # 白名单外的真实后缀必须原样返回（供不支持格式剔除检查识别），
    # 只有完全没有后缀时才默认 png。
    suffix = os.path.splitext(raw)[1].lstrip(".").lower()
    if not suffix:
        return "png"
    return "jpg" if suffix == "jpeg" else suffix


def _is_unsupported_image_ext(ext: str) -> bool:
    return str(ext or "").lower().strip(".") not in _SUPPORTED_IMAGE_EXTS


def _extract_lines_python_docx(data: bytes) -> list[_Line]:
    from docx import Document

    document = Document(io.BytesIO(data))
    lines: list[_Line] = []
    for paragraph in document.paragraphs:
        text = paragraph.text or ""
        try:
            rids = paragraph._p.xpath('.//a:blip/@r:embed')
        except Exception:  # noqa: BLE001
            rids = []
        for rid in rids:
            try:
                part = paragraph.part.related_parts[rid]
            except Exception:  # noqa: BLE001
                continue
            ext = _ext_from_name(getattr(part, "partname", "") or "image.png")
            lines.append(
                _Line(
                    text=text,
                    image_bytes=part.blob,
                    image_ext=ext,
                )
            )
            # 段内文本与首图合并为一条行；一段多图时其余图片独立成行。
            text = ""
        if text:
            lines.append(_Line(text=text))
    return lines


def _extract_lines_pandoc(data: bytes) -> list[_Line] | None:
    """pandoc 主路径：docx→markdown（公式→LaTeX、图片导出 media/）。

    pandoc 二进制或 pypandoc 不可用时返回 None，由调用方回退 python-docx。
    """
    if os.getenv("QUESTION_BANK_DISABLE_PANDOC", "").strip().lower() in {
        "1",
        "on",
        "true",
    }:
        return None
    try:
        import tempfile
        from pathlib import Path

        import pypandoc

        with tempfile.TemporaryDirectory(prefix="qb-parse-") as tmp:
            docx_path = Path(tmp) / "source.docx"
            docx_path.write_bytes(data)
            media_dir = Path(tmp) / "media"
            markdown = pypandoc.convert_file(
                str(docx_path),
                "markdown",
                extra_args=[f"--extract-media={media_dir}"],
            )
            lines: list[_Line] = []
            for raw_line in str(markdown or "").splitlines():
                image_match = _MD_IMAGE_RE.search(raw_line)
                if image_match:
                    image_path = (Path(tmp) / image_match.group(1)).resolve()
                    # docx rels target 是文档内可控文本：绝对路径会整体覆盖
                    # tmp 前缀（Python Path 语义），读前做包含性校验。
                    try:
                        image_path.relative_to(Path(tmp).resolve())
                    except ValueError:
                        lines.append(_Line(text=raw_line))
                        continue
                    try:
                        image_bytes = image_path.read_bytes()
                    except OSError:
                        lines.append(_Line(text=raw_line))
                        continue
                    text = _MD_IMAGE_RE.sub("", raw_line).strip()
                    lines.append(
                        _Line(
                            text=text,
                            image_bytes=image_bytes,
                            image_ext=_ext_from_name(image_path.name),
                        )
                    )
                elif raw_line.strip():
                    lines.append(_Line(text=raw_line.rstrip()))
            return lines
    except Exception as exc:  # noqa: BLE001
        logger.info("[QuestionBank] pandoc 解析不可用，回退 python-docx: %s", exc)
        return None


def extract_lines(data: bytes) -> tuple[list[_Line], str]:
    lines = _extract_lines_pandoc(data)
    if lines is not None:
        return lines, "pandoc"
    return _extract_lines_python_docx(data), "python-docx"


# ── 切分与字段解析 ────────────────────────────────────────────────────────


class _QuestionBuilder:
    """单题字段的顺序累积器（按标记切换当前字段）。"""

    def __init__(self) -> None:
        self.fields: dict[str, list[str]] = {name: [] for name in _MARKER_FIELDS}
        self.current: str | None = None
        self.images: list[ParsedImage] = []
        self.image_in_stem = True
        self.stray_text = False
        self.saw_marker = False
        self.dropped_image_count = 0
        self.unsupported_image_exts: set[str] = set()

    def set_marker(self, name: str, inline: str) -> None:
        self.current = name
        self.saw_marker = True
        if inline.strip():
            self.fields[name].append(inline.strip())

    def append_text(self, text: str) -> None:
        if self.current is None:
            # 块内首个标记之前出现的正文：并入题干并记警告。
            self.stray_text = True
            self.fields["题干"].append(text.strip())
            return
        self.fields[self.current].append(text.strip())

    def append_image(self, image_bytes: bytes, ext: str) -> None:
        if len(self.images) >= MAX_IMAGES_PER_QUESTION:
            # 超限丢弃计数：此前的「超出部分已忽略」warning 是死代码
            #（先 return 再在题外判断 len>max 恒假），现在真实累计。
            self.dropped_image_count += 1
            return
        if _is_unsupported_image_ext(ext):
            self.unsupported_image_exts.add(str(ext).lower().strip("."))
            return
        self.images.append(
            ParsedImage(
                placeholder_id=f"img_{uuid.uuid4().hex[:12]}",
                ext=ext,
                data=image_bytes,
            )
        )
        if self.current not in {None, "题干"}:
            self.image_in_stem = False

    def field_text(self, name: str) -> str:
        return "\n".join(part for part in self.fields[name] if part).strip()


def _parse_options(raw: str) -> list[dict[str, str]]:
    """把选项字段解析为 [{key, text}]；每行/每段一个 ``A.`` 起始的选项。"""
    options: list[dict[str, str]] = []
    if not raw.strip():
        return options
    segments: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # 一行内多个选项（如 "A. 1 B. 2"）按选项起始符切分。
        segments.extend([seg.strip() for seg in _OPTION_SPLIT_RE.split(line) if seg.strip()])
    for segment in segments:
        match = _OPTION_START_RE.match(segment)
        if match:
            options.append({"key": match.group(1), "text": match.group(2).strip()})
        elif options:
            options[-1]["text"] = f"{options[-1]['text']} {segment}".strip()
        # 既无起始符又无累积上下文的片段视为格式噪声，跳过。
    return options


def _normalize_type(raw: str) -> str | None:
    return _TYPE_ALIASES.get(raw.strip())


def _normalize_difficulty(raw: str) -> str | None:
    return _DIFFICULTY_ALIASES.get(raw.strip())


def _split_knowledge_points(raw: str) -> list[str]:
    parts = [part.strip() for part in _KP_SPLIT_RE.split(raw)]
    seen: dict[str, None] = {}
    for part in parts:
        if part:
            seen.setdefault(part, None)
    return list(seen.keys())


def _build_question(builder: _QuestionBuilder) -> ParsedQuestion:
    question = ParsedQuestion()
    raw_type = builder.field_text("题型")
    question.question_type = _normalize_type(raw_type)
    question.stem = builder.field_text("题干")
    question.answer = builder.field_text("答案")
    question.analysis = builder.field_text("解析")
    # 模板未设分值字段（分值场景少且原文常缺失），入库后可经标注补充。
    question.source_name = builder.field_text("来源")
    question.chapter = builder.field_text("章节")
    question.difficulty = _normalize_difficulty(builder.field_text("难度"))
    question.knowledge_points = _split_knowledge_points(builder.field_text("知识点"))
    question.options = _parse_options(builder.field_text("选项"))
    question.images = builder.images

    if not question.question_type:
        question.issue(
            "error",
            f"题型无法识别：{raw_type[:30] or '（缺失）'}，仅支持 {'/'.join(QUESTION_TYPES)}",
        )
    if not question.stem:
        question.issue("error", "题干为空")
    if not question.answer:
        question.issue("error", "答案为空（答案须教师自填，系统不生成）")
    if question.question_type == "选择题" and not question.options:
        question.issue(
            "warning", "选择题未能解析出选项（需 A. / B. 起始的选项行），请检查格式"
        )
    if not question.analysis:
        question.issue("warning", "解析为空，建议补充讲解过程")
    if not question.difficulty:
        question.issue("warning", "难度缺失或无法识别（简单/中等/较难），入库后可补标注")
    if not question.knowledge_points:
        question.issue("warning", "知识点为空，入库后可补标注")
    if builder.stray_text:
        question.issue(
            "warning", "题干标记之前存在游离文本，已并入题干，请核对模板格式"
        )
    if not builder.image_in_stem:
        question.issue(
            "warning", "存在题干区域之外的图片，已并入本题图集，请核对归属"
        )
    if builder.dropped_image_count:
        question.issue(
            "warning",
            f"图片超过 {MAX_IMAGES_PER_QUESTION} 张，已忽略 {builder.dropped_image_count} 张超出部分",
        )
    if builder.unsupported_image_exts:
        exts = "、".join(sorted(builder.unsupported_image_exts))
        question.issue(
            "warning",
            f"含 {exts} 格式图片，浏览器无法显示且已剔除；请在 Word 中将其另存为 png/jpg 后重新上传",
        )
    return question


def parse_question_docx(
    data: bytes,
    *,
    known_knowledge_points: set[str] | None = None,
) -> ParseResult:
    """解析一次上传的 docx。图片字节留在 ``ParsedQuestion.images``，由服务层落库。"""
    lines, parser_path = extract_lines(data)
    result = ParseResult(parser_path=parser_path)

    builders: list[_QuestionBuilder] = []
    builder = _QuestionBuilder()
    for line in lines:
        if _SEPARATOR_RE.match(line.text):
            builders.append(builder)
            builder = _QuestionBuilder()
            continue
        marker = _MARKER_RE.match(line.text.strip())
        if marker:
            builder.set_marker(marker.group(1), marker.group(2))
            continue
        if line.image_bytes:
            builder.append_image(line.image_bytes, line.image_ext)
        if line.text.strip():
            builder.append_text(line.text.strip())
    builders.append(builder)

    for item in builders:
        has_content = any(item.field_text(name) for name in _MARKER_FIELDS) or item.images
        if not has_content:
            continue
        if not item.saw_marker and not item.images:
            # 无任何标记且无图片的块是模板说明/前言散文，整块忽略——
            # 教师把官方模板原样上传时，说明段不会变成报错的"题目"。
            continue
        question = _build_question(item)
        if known_knowledge_points is not None:
            for kp in question.knowledge_points:
                if kp not in known_knowledge_points:
                    question.issue("warning", f"知识点「{kp}」不在字典中，入库后需补录")
        result.questions.append(question)
        if len(result.questions) >= MAX_QUESTIONS_PER_BATCH:
            logger.warning(
                "[QuestionBank] 单批题目超过 %d，超出部分忽略", MAX_QUESTIONS_PER_BATCH
            )
            break
    return result


def parsed_question_to_dict(question: ParsedQuestion, *, seq: int) -> dict:
    """把解析题序列化为批次 parsed_json 中的条目（图片以占位符表示）。"""
    return {
        "seq": seq,
        "question_type": question.question_type,
        "stem": question.stem,
        "options": question.options,
        "answer": question.answer,
        "analysis": question.analysis,
        "difficulty": question.difficulty,
        "chapter": question.chapter,
        "knowledge_points": question.knowledge_points,
        "source_name": question.source_name,
        "images": [
            {"placeholder_id": image.placeholder_id, "ext": image.ext}
            for image in question.images
        ],
        "issues": question.issues,
        "has_error": question.has_error,
    }
