"""成绩单 PDF 坐标法解析（M-02）。

PDF 文本层顺序错乱，必须按 word 级 bbox 重建表格（§1.3 / §6.2）：
- 每行并排两名学生（左栏 + 右栏），按 x 坐标把数字分配给最近的左侧学号；
- 列偏移自标定：学号 x0 聚 2 簇（左右栏），每侧数字 x0 聚 3 簇（平时/期末/总评）；
- 缺考行只有 1 个数字（平时=0），允许数字个数 < 3；
- 姓名列宽度可变（2~4 字），用「学号右侧、数字列左侧」的 CJK 词识别。

extract_words / cluster_rows / detect_columns / assign_cells 四个阶段可独立测试，
其中后三个是纯函数（不碰 PDF）。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import NamedTuple, Sequence

import pymupdf

from backend.config.loggers import get_logger

logger = get_logger(__name__)

_RE_SID = re.compile(r"^\d{12}$")
_RE_NUM = re.compile(r"^\d{1,3}(\.\d+)?$")
_RE_FLAG = re.compile(r"^(缺考|违纪|缓考|免修)$")

# 姓名列与学号的相对位置：姓名在学号右侧、第一个数字列之前
_NAME_BEFORE_COL = 1  # 姓名位于第 1 个数字列之前


class Word(NamedTuple):
    """带 bbox 的词。"""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float


class ColumnSpec(NamedTuple):
    """列定义：名称与 x 中心。"""

    name: str
    x: float


class GradeRecord(NamedTuple):
    """单条成绩记录。"""

    student_no: str
    name: str
    regular: Decimal | None
    final: Decimal | None
    total: Decimal | None
    flag: str = ""


class GradeSheetResult:
    """成绩单解析结果：记录 + 头部元数据 + 总结 + 问题清单。"""

    def __init__(
        self,
        *,
        records: list[GradeRecord],
        meta: dict,
        summary: dict,
        issues: list[dict],
    ) -> None:
        self.records = records
        self.meta = meta
        self.summary = summary
        self.issues = issues

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"GradeSheetResult(records={len(self.records)}, "
            f"meta={self.meta}, issues={len(self.issues)})"
        )


# ── 阶段 1：抽取 ──


def extract_words(pdf_bytes: bytes, page_no: int) -> list[Word]:
    """PyMuPDF 取带 bbox 的词序列。"""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[page_no]
        return [
            Word(text=w[4], x0=w[0], y0=w[1], x1=w[2], y1=w[3])
            for w in page.get_text("words")
        ]
    finally:
        doc.close()


# ── 阶段 2：行聚类 ──


def cluster_rows(words: list[Word], *, y_tol: float = 3.0) -> list[list[Word]]:
    """按 y 坐标聚类成行（贪心合并，行间最小间距取 2*y_tol）。

    不用固定整数 band（round(y/带宽) 会把 y 差 0.5pt 的同伴拆到边界两侧）：
    按 y0 升序贪心，当前词与「行代表 y0」之差 < 2*y_tol 则并入当前行。
    成绩单行距 18.6pt，默认阈值 6pt 足够稳定分行。
    """
    threshold = max(y_tol * 2, 1.0)
    ordered = sorted(words, key=lambda w: w.y0)
    rows: list[list[Word]] = []
    for w in ordered:
        if rows and abs(w.y0 - rows[-1][0].y0) < threshold:
            rows[-1].append(w)
        else:
            rows.append([w])
    # 行内按 x 排序
    return [sorted(row, key=lambda w: w.x0) for row in rows]


# ── 阶段 3：列检测 ──


def _kmeans_1d(values: list[float], k: int, *, max_iter: int = 50) -> list[float]:
    """一维 k-means，返回 k 个中心（升序）。"""
    if not values:
        return []
    k = min(k, len(set(round(v, 1) for v in values)))
    sorted_vals = sorted(values)
    # 均匀初始化
    centers = [
        sorted_vals[int(i * (len(sorted_vals) - 1) / max(k - 1, 1))] for i in range(k)
    ]
    for _ in range(max_iter):
        clusters: list[list[float]] = [[] for _ in range(k)]
        for v in sorted_vals:
            idx = min(range(k), key=lambda i: abs(v - centers[i]))
            clusters[idx].append(v)
        new_centers = [
            sum(cluster) / len(cluster) if cluster else centers[i]
            for i, cluster in enumerate(clusters)
        ]
        if all(abs(a - b) < 1e-6 for a, b in zip(centers, new_centers)):
            break
        centers = new_centers
    return sorted(centers)


def detect_columns(
    rows: list[list[Word]],
    *,
    header_keywords: Sequence[str] = ("学号", "平时", "期末", "总成绩", "标志"),
) -> list[ColumnSpec]:
    """表头语义优先：用表头词 x 中心定列锚点；找不到才回退到数字聚类。

    返回 [学号, 平时, 期末, 总成绩, 标志]（左栏）；右栏 = 左栏 + 栏距。
    栏距由学号 x0 聚类（两簇中心之差）得到。
    """
    # 学号聚类 → 左右栏锚点
    all_words = [w for row in rows for w in row]
    sid_x0 = [w.x0 for w in all_words if _RE_SID.fullmatch(w.text)]
    if len(sid_x0) >= 2:
        centers = _kmeans_1d(sid_x0, 2)
        left_sid = centers[0]
    else:
        left_sid = 61.9  # 回退到实测默认值（§1.3）

    # 表头词定位：找「平时」「期末」等（学号列以数据聚类中心为准，表头位置不可靠）
    header_pos: dict[str, float] = {}
    for row in rows:
        for w in row:
            text = w.text.strip()
            for kw in header_keywords:
                if text == kw and kw not in header_pos:
                    if kw == "学号":
                        continue  # 学号列锚点用数据聚类 left_sid，不用表头
                    header_pos[kw] = w.x0
                    break

    def _column(name: str, default_x: float) -> ColumnSpec:
        x = header_pos.get(name)
        if x is None:
            x = default_x
        return ColumnSpec(name=name, x=x)

    columns = [
        ColumnSpec(name="学号", x=left_sid),
        _column("平时", left_sid + 123.4),  # 185.3 - 61.9
        _column("期末", left_sid + 151.1),  # 213.0 - 61.9
        _column("总成绩", left_sid + 182.4),  # 244.3 - 61.9
        _column("标志", left_sid + 210.1),  # 272 - 61.9
    ]
    # 数字列自标定：只用**左栏**数字（距左栏学号 100~250pt），聚 k=3；
    # 若混入右栏数字，k=3 会把左右两栏共 6 列错误折叠（实测缺陷）。
    left_nums = [
        w.x0
        for row in rows[:8]
        for w in row
        if _RE_NUM.fullmatch(w.text) and left_sid + 100 < w.x0 < left_sid + 250
    ]
    if len(left_nums) >= 6:
        num_centers = sorted(_kmeans_1d(left_nums, 3))
        if len(num_centers) == 3:
            for name, center in zip(("平时", "期末", "总成绩"), num_centers):
                if name in header_pos:
                    continue
                for idx, col in enumerate(columns):
                    if col.name == name:
                        columns[idx] = ColumnSpec(name, center)
                        break
    return columns


# ── 阶段 4：单元格归属 ──


def assign_cells(
    rows: list[list[Word]], columns: list[ColumnSpec]
) -> list[GradeRecord]:
    """词落到列，输出行记录。

    每行并排两名学生：学号词锚定学生，数字按最近列中心归属，
    姓名取「学号右侧、第一个数字列之前」的 CJK 词。
    **右栏学生的列中心整体平移栏距**（右栏 = 左栏 + gap），否则右栏数字全部错位。
    """
    by_name = {c.name: c for c in columns}
    left_sid_x = by_name["学号"].x
    num_cols = [by_name["平时"].x, by_name["期末"].x, by_name["总成绩"].x]

    records: list[GradeRecord] = []
    for row in rows:
        sids = [w for w in row if _RE_SID.fullmatch(w.text)]
        if not sids:
            continue
        for sid in sorted(sids, key=lambda w: w.x0):
            # 右栏判定：该学号距左栏学号中心明显更远
            is_right = sid.x0 - left_sid_x > 100.0
            shift = sid.x0 - left_sid_x if is_right else 0.0
            col_centers = [x + shift for x in num_cols]

            # 属于该学生的词：x0 在 [sid.x1, 下一个学号 x0) 区间
            next_sid_x = min(
                (w.x0 for w in sids if w.x0 > sid.x0), default=float("inf")
            )
            region = [w for w in row if sid.x1 <= w.x0 < next_sid_x and w is not sid]
            name = ""
            nums: list[tuple[float, Decimal]] = []
            flag = ""
            for w in region:
                if _RE_NUM.fullmatch(w.text):
                    nums.append((w.x0, Decimal(w.text)))
                elif _RE_FLAG.fullmatch(w.text.strip()):
                    flag = w.text.strip()
                elif (
                    w.x0 < col_centers[0]  # 平时列左侧
                    and not name
                    and any("\u4e00" <= ch <= "\u9fff" for ch in w.text)
                ):
                    name = w.text.strip()
            # 数字按列中心归属
            values: dict[str, Decimal] = {}
            for x0, value in nums:
                idx = min(range(3), key=lambda i: abs(x0 - col_centers[i]))
                col_name = ("平时", "期末", "总成绩")[idx]
                values[col_name] = value
            records.append(
                GradeRecord(
                    student_no=sid.text,
                    name=name or "",
                    regular=values.get("平时"),
                    final=values.get("期末"),
                    total=values.get("总成绩"),
                    flag=flag,
                )
            )
    return records


# ── 阶段 5：编排 + 头部/总结 + 校验 ──


def _parse_header(text: str) -> dict:
    meta: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "课程编号" in line and "课程名称" in line:
            m = re.search(r"课程编号[:：]\s*(\S+)", line)
            n = re.search(r"课程名称[:：]\s*(\S+)", line)
            if m:
                meta["course_code"] = m.group(1)
            if n:
                meta["course_name"] = n.group(1)
        elif "成绩项目比例" in line:
            meta["score_ratio"] = line
            wm = re.search(r"平时成绩\s*(\d+)%", line)
            em = re.search(r"期末成绩\s*(\d+)%", line)
            if wm:
                meta["regular_weight"] = Decimal(wm.group(1)) / 100
            if em:
                meta["exam_weight"] = Decimal(em.group(1)) / 100
        elif "开课学期" in line:
            meta["semester"] = line
        elif "上课班级" in line:
            meta["class_no"] = line
    return meta


def _parse_summary(words: list[Word]) -> dict:
    """用 word 级坐标对齐「成绩总结」区的标签与数字（文本层顺序错乱，逐行解析不可靠）。

    结构（§1.3 实测）：标签行 y≈630、数字行 y≈645，
    数字 x0 距哪个标签 x0 最近即归属哪一列。
    """
    summary: dict = {}
    labels = [
        ("expected", "应考人数", 72.4),
        ("absent", "未考", 134.0),
        ("violation", "违纪", 163.2),
        ("b100_90", "100-90分", 184.2),
        ("b89_80", "89-80分", 247.3),
        ("b79_70", "79-70分", 321.0),
        ("b69_60", "69-60分", 383.5),
        ("b59_below", "59分以下", 430.6),
        ("average", "平均分", 496.2),
    ]
    # 收集数字词（只取「成绩总结」区 580 < y0 < 700 的词，排除正文与页脚页码；
    # 排除学号与四位数年份，注意不能用 startswith("20")——会误杀「20」这类真实成绩）
    _RE_YEAR = re.compile(r"^(?:19|20)\d{2}$")
    nums = [
        w
        for w in words
        if 580 < w.y0 < 700
        and _RE_NUM.fullmatch(w.text)
        and not _RE_SID.fullmatch(w.text)
        and not _RE_YEAR.fullmatch(w.text)
    ]
    for key, _label, default_x in labels:
        if not nums:
            continue
        # 最近邻：数字 x0 距哪个标签锚点最近即归属哪列（阈值过滤会串列）
        nearest = min(nums, key=lambda w: abs(w.x0 - default_x))
        if abs(nearest.x0 - default_x) > 30:
            continue
        value = nearest.text
        if key == "average":
            summary[key] = Decimal(value)
        else:
            summary[key] = int(value)

    # 5 段分布汇总（顺序固定：100-90 / 89-80 / 79-70 / 69-60 / 59以下）
    band_keys = ["b100_90", "b89_80", "b79_70", "b69_60", "b59_below"]
    if all(k in summary for k in band_keys):
        summary["bands"] = {
            "100-90": summary.pop("b100_90"),
            "89-80": summary.pop("b89_80"),
            "79-70": summary.pop("b79_70"),
            "69-60": summary.pop("b69_60"),
            "59以下": summary.pop("b59_below"),
        }
    return summary


def parse_grade_sheet(pdf_bytes: bytes) -> GradeSheetResult:
    """编排四个阶段 + 头部/总结抽取 + 学号归一化校验。"""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        meta = _parse_header(doc[0].get_text())
        summary_words = extract_words(pdf_bytes, doc.page_count - 1)
        summary = _parse_summary(summary_words)

        all_records: list[GradeRecord] = []
        for page_no in range(doc.page_count):
            words = extract_words(pdf_bytes, page_no)
            rows = cluster_rows(words)
            if not rows:
                continue
            columns = detect_columns(rows)
            all_records.extend(assign_cells(rows, columns))

        # 学号去重 + 归一化
        seen: set[str] = set()
        records: list[GradeRecord] = []
        for rec in all_records:
            sid = re.sub(r"[\s\u3000]", "", rec.student_no)
            if sid in seen:
                continue
            seen.add(sid)
            records.append(
                GradeRecord(
                    student_no=sid,
                    name=rec.name,
                    regular=rec.regular,
                    final=rec.final,
                    total=rec.total,
                    flag=rec.flag,
                )
            )

        issues: list[dict] = []
        logger.info(
            "成绩单解析完成: records=%d meta_keys=%s summary=%s",
            len(records),
            sorted(meta),
            summary,
        )
        return GradeSheetResult(
            records=records, meta=meta, summary=summary, issues=issues
        )
    finally:
        doc.close()
