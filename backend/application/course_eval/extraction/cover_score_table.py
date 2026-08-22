"""封面得分表与大题结构解析（MinerU 单通道，纯函数）。

MinerU 的 `table_body` 是 HTML 字符串，手写笔画会让它把单元格误判出多余的
`colspan`，同一张 2×7 的得分表在实测里会给出 7~9 个单元格。所以这里：

- 先把 HTML 展开成网格，用**网格列区间**而不是单元格序号对齐表头与得分行；
- 大题数以 `ce_exam_sections` 声明的结构为真值（方案 §6.3.4），列数不符时整页转人工；
- 用总分列做交叉校验（Σ题分 == 总分）替代第二个识别通道：一致才给高置信度，
  不一致的整页进核对清单。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

_CN_ORDER = {c: i + 1 for i, c in enumerate("一二三四五六七八九十")}
_TOTAL_WORDS = ("总分", "总计", "合计", "总成绩")
_HEADER_WORDS = ("大题", "题号", "题次", "题目")
_SCORE_WORDS = ("得分", "分数", "评分")
_RE_NUMBER = re.compile(r"\d{1,3}(?:\.\d)?")
# MinerU 把手写数字经常识别成行内公式（"$2 0$"，字符间带空格）
_RE_MATH_SEGMENT = re.compile(r"\$([^$]*)\$")
_RE_SECTION_HEAD = re.compile(
    r"[（(]?([一二三四五六七八九十])[)）]?\s*[、,.．]\s*([^。．（(\n]{1,20})"
)
_RE_TOTAL_SCORE = re.compile(r"共\s*(\d{1,3}(?:\.\d)?)\s*分")
# 「10小题×2分=20分」的 =总分 写法（2023-2024 卷）
_RE_EQ_TOTAL = re.compile(r"[=＝]\s*(\d{1,3}(?:\.\d)?)\s*分")
_RE_SCORE_PATTERN = re.compile(r"(\d{1,3}(?:\.\d)?\s*分\s*[×xX*✕]\s*\d{1,2})")
# 「10小题×2分」：先题数后单题分（2023-2024 卷）
_RE_ITEMS_PATTERN = re.compile(r"(\d{1,2})\s*小?题\s*[×xX*✕]\s*(\d{1,3}(?:\.\d)?)\s*分")
_RE_MAJOR = re.compile(r"([\u4e00-\u9fa5]{2,10}专业)")


def normalize_ocr_text(text: str) -> str:
    """去掉 MinerU 行内公式包装并合并公式内被拆开的数字（"$2 0$" → "20"）。"""
    return _RE_MATH_SEGMENT.sub(
        lambda m: re.sub(r"(?<=[\d.])\s+(?=[\d.])", "", m.group(1)), text or ""
    )


@dataclass(frozen=True)
class _Cell:
    """逻辑单元格：文本 + 它占据的网格列区间 [col, col + span)。"""

    text: str
    col: int
    span: int


@dataclass(frozen=True)
class CoverScoreRow:
    """一张封面得分表的读数结果。"""

    section_scores: dict[int, Decimal] = field(default_factory=dict)
    total: Decimal | None = None
    # 表头识别到的题号列数（用于和声明的大题数对比）
    section_columns: int = 0
    # Σ题分 == 总分；None 表示没有可用的总分列
    total_consistent: bool | None = None
    # 总分一致时，空白列按算术补 0 的题号
    inferred_zero_orders: tuple[int, ...] = ()
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class SectionDef:
    """从试卷正文识别出的大题结构。"""

    order_no: int
    question_type: str
    total_score: Decimal | None = None
    score_pattern: str | None = None
    applicable_major: str | None = None


class _TableParser(HTMLParser):
    """把 MinerU 的 HTML 表格解析成逻辑单元格 + 网格列坐标。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] | None = None
        self._text: list[str] = []
        self._span = 1
        self._rowspan = 1
        # 被上方 rowspan 占用的列：{列号: 剩余行数}
        self._occupied: dict[int, int] = {}
        self._cursor = 0
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
            self._cursor = 0
            return
        if tag in ("td", "th"):
            values = dict(attrs)
            self._span = _positive_int(values.get("colspan"))
            self._rowspan = _positive_int(values.get("rowspan"))
            self._text = []
            self._in_cell = True

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell and self._row is not None:
            while self._occupied.get(self._cursor):
                self._cursor += 1
            cell = _Cell(
                text=normalize_ocr_text("".join(self._text)).strip(),
                col=self._cursor,
                span=self._span,
            )
            self._row.append(cell)
            if self._rowspan > 1:
                for offset in range(self._span):
                    self._occupied[self._cursor + offset] = self._rowspan - 1
            self._cursor += self._span
            self._in_cell = False
            return
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._occupied = {
                col: left - 1 for col, left in self._occupied.items() if left > 1
            }

    def close(self) -> None:  # noqa: D102
        super().close()
        if self._row:
            self.rows.append(self._row)
            self._row = None


def _positive_int(raw: str | None) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return value if value > 0 else 1


def parse_html_table(html: str) -> list[list[_Cell]]:
    """HTML 表格 → 逐行逻辑单元格（带网格列坐标）。非表格返回空列表。"""
    if not html or "<t" not in html.lower():
        return []
    parser = _TableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - HTMLParser 对残缺标签容错，兜底不抛
        return parser.rows
    return parser.rows


def _numbers_in(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for raw in _RE_NUMBER.findall(text or ""):
        try:
            values.append(Decimal(raw))
        except InvalidOperation:
            continue
    return values


def _value_in_span(
    row: list[_Cell], col: int, span: int
) -> tuple[Decimal | None, bool]:
    """取得分行中落在 [col, col+span) 区间内的唯一数字。

    返回 (值, 是否冲突)。区间里出现两个不同数字时判冲突（宁缺毋滥，转人工）。
    """
    found: list[Decimal] = []
    for cell in row:
        if cell.col + cell.span <= col or cell.col >= col + span:
            continue
        found.extend(_numbers_in(cell.text))
    distinct = {v for v in found}
    if not distinct:
        return None, False
    if len(distinct) > 1:
        return None, True
    return found[0], False


def _find_row(rows: list[list[_Cell]], words: Iterable[str]) -> list[_Cell] | None:
    for row in rows:
        if not row:
            continue
        head = row[0].text.replace(" ", "")
        if any(word in head for word in words):
            return row
    return None


def _is_total_cell(text: str) -> bool:
    compact = text.replace(" ", "")
    return any(word in compact for word in _TOTAL_WORDS)


def parse_cover_score_table(html: str, section_count: int) -> CoverScoreRow | None:
    """解析封面「大题/得分」表。不是得分表时返回 None。

    section_count 是声明的大题数（真值）。表头题号列数与它不符时不取数，
    只回一条 issue，交核对阶段处理。
    """
    rows = parse_html_table(html)
    if len(rows) < 2:
        return None
    header = _find_row(rows, _HEADER_WORDS)
    score_row = _find_row(rows, _SCORE_WORDS)
    if header is None or score_row is None or header is score_row:
        return None

    total_cell = next(
        (cell for cell in reversed(header) if _is_total_cell(cell.text)), None
    )
    limit = total_cell.col if total_cell is not None else None
    question_cells = [
        cell
        for cell in header[1:]
        if (limit is None or cell.col < limit) and not _is_total_cell(cell.text)
    ]

    issues: list[str] = []
    if total_cell is None:
        issues.append("得分表缺少总分列，无法交叉校验")
    if len(question_cells) != section_count:
        issues.append(
            f"得分表识别到 {len(question_cells)} 个题号列，"
            f"与声明的 {section_count} 个大题不符"
        )
        return CoverScoreRow(
            section_columns=len(question_cells),
            issues=tuple(issues),
        )

    scores: dict[int, Decimal] = {}
    blanks: list[int] = []
    for order, cell in enumerate(question_cells, start=1):
        value, conflict = _value_in_span(score_row, cell.col, cell.span)
        if conflict:
            issues.append(f"第{order}题所在单元格识别出多个数字")
        if value is None:
            blanks.append(order)
            continue
        scores[order] = value

    total: Decimal | None = None
    if total_cell is not None:
        total, total_conflict = _value_in_span(
            score_row, total_cell.col, total_cell.span
        )
        if total_conflict:
            issues.append("总分单元格识别出多个数字")

    consistent: bool | None = None
    inferred: tuple[int, ...] = ()
    if total is not None:
        consistent = sum(scores.values(), Decimal(0)) == total
        if consistent and blanks:
            # 各题之和已等于总分，空白列只能是 0（仍需教师确认）
            inferred = tuple(blanks)
        if not consistent:
            issues.append(
                f"各题之和 {sum(scores.values(), Decimal(0))} 与总分 {total} 不一致"
            )
    return CoverScoreRow(
        section_scores=scores,
        total=total,
        section_columns=len(question_cells),
        total_consistent=consistent,
        inferred_zero_orders=inferred,
        issues=tuple(issues),
    )


def _parse_section_total(
    line: str, items_match: re.Match | None
) -> Decimal | None:
    """大题满分：「共20分」→「=20分」→「10小题×2分」推算，逐级回退。"""
    for regex in (_RE_TOTAL_SCORE, _RE_EQ_TOTAL):
        found = regex.search(line)
        if found:
            try:
                return Decimal(found.group(1))
            except InvalidOperation:
                continue
    if items_match:
        try:
            return Decimal(items_match.group(1)) * Decimal(items_match.group(2))
        except InvalidOperation:
            return None
    return None


def _parse_score_pattern(line: str, items_match: re.Match | None) -> str | None:
    """分值模式统一成「单题分×题数」（「10小题×2分」也归一成「2分×10」）。"""
    pattern_match = _RE_SCORE_PATTERN.search(line)
    if pattern_match:
        return pattern_match.group(1).replace(" ", "")
    if items_match:
        return f"{items_match.group(2)}分×{items_match.group(1)}"
    return None


def parse_section_defs(texts: Iterable[str]) -> list[SectionDef]:
    """从试卷正文文本行识别大题结构（「二、填空题。（2分×10，共20分）」）。

    同一大题在多份试卷里会重复出现，OCR 偶发错字，按多数票定稿。
    """
    votes: dict[int, list[tuple[str, Decimal | None, str | None, str | None]]] = {}
    for raw in texts:
        line = (raw or "").strip()
        if not line:
            continue
        head = _RE_SECTION_HEAD.match(line)
        if head is None:
            continue
        order = _CN_ORDER.get(head.group(1))
        if order is None:
            continue
        question_type = head.group(2).strip().strip("。.、").replace(" ", "")
        if not question_type or len(question_type) > 16:
            continue
        line = normalize_ocr_text(line)
        items_match = _RE_ITEMS_PATTERN.search(line)
        total = _parse_section_total(line, items_match)
        pattern = _parse_score_pattern(line, items_match)
        major_match = _RE_MAJOR.search(line)
        votes.setdefault(order, []).append(
            (
                question_type,
                total,
                pattern,
                major_match.group(1) if major_match else None,
            )
        )

    defs: list[SectionDef] = []
    for order in sorted(votes):
        candidates = votes[order]
        question_type = Counter(c[0] for c in candidates).most_common(1)[0][0]
        totals = [c[1] for c in candidates if c[1] is not None]
        total = Counter(totals).most_common(1)[0][0] if totals else None
        patterns = [c[2] for c in candidates if c[2]]
        pattern = Counter(patterns).most_common(1)[0][0] if patterns else None
        majors = [c[3] for c in candidates if c[3]]
        major = Counter(majors).most_common(1)[0][0] if majors else None
        defs.append(
            SectionDef(
                order_no=order,
                question_type=question_type,
                total_score=total,
                score_pattern=pattern,
                applicable_major=major,
            )
        )
    return defs
