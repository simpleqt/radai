"""试卷结构与逐题得分自动提取（MinerU 单通道 + 总分交叉校验）。

读数只走 MinerU：大题结构与封面得分都从 OCR 输出解析，视觉模型不参与识别。
「第二通道」由确定性的总分交叉校验承担——Σ题分 == 总分列才允许 confirmed，
其余整页进核对清单，宁可让教师补一笔也不静默写错数。

本模块是纯领域逻辑（解析/对齐/落库前的 cells 组装），不做任何 ORM 写入；
数据库读写由 agent_tools 层的工具闭包完成。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from decimal import Decimal

from backend.application.course_eval.extraction.cover_score_table import (
    CoverScoreRow,
    SectionDef,
    parse_cover_score_table,
    parse_section_defs,
)
from backend.config.loggers import get_logger

logger = get_logger(__name__)

_STRUCTURE_BODY_PAGES = 6  # 随封面一起送 OCR 的正文页数，够覆盖到最后一个大题标题

_CONF_TOTAL_MATCH = Decimal("0.90")  # 总分自洽
_CONF_INFERRED_ZERO = Decimal("0.60")  # 总分自洽时空白列按算术补 0
_CONF_NO_TOTAL = Decimal("0.55")  # 没读到总分，无法交叉校验
_CONF_TOTAL_MISMATCH = Decimal("0.40")  # 各题之和与总分不符


@dataclass(frozen=True)
class PaperOcrResult:
    """一次 MinerU 调用解析出的试卷结构与封面读数。"""

    cover_rows: dict[int, CoverScoreRow]
    section_defs: list[SectionDef]
    section_count: int
    warnings: list[str]


def _page_index(item: dict) -> int | None:
    """取 content_list 条目的页序（子 PDF 内 0 起，与原 PDF 页号无关）。"""
    for key in ("page_idx", "page", "page_no"):
        value = item.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _ocr_texts(content_list: list) -> list[str]:
    kinds = {"text", "aside_text", "header", "title", "footer"}
    return [
        str(item.get("text"))
        for item in content_list
        if isinstance(item, dict)
        and str(item.get("type", "")).lower() in kinds
        and item.get("text")
    ]


def _cover_table_htmls(content_list: list, cover_count: int) -> dict[int, list[str]]:
    """封面页表格 HTML，按子 PDF 页序归组（子 PDF 前 cover_count 页是封面）。"""
    tables: dict[int, list[str]] = {}
    for item in content_list:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).lower() != "table":
            continue
        page = _page_index(item)
        if page is None or not 0 <= page < cover_count:
            continue
        body = item.get("table_body") or item.get("text") or ""
        if isinstance(body, str) and body.strip():
            tables.setdefault(page, []).append(body)
    return tables


def parse_paper_ocr(
    content_list: list,
    *,
    cover_count: int,
    declared_section_count: int | None = None,
) -> PaperOcrResult:
    """把 MinerU 输出解析成大题结构 + 每份试卷封面的得分行（纯函数）。"""
    warnings: list[str] = []
    tables = _cover_table_htmls(content_list, cover_count)
    section_defs = parse_section_defs(_ocr_texts(content_list))
    section_count = declared_section_count or len(section_defs)
    if section_count <= 0:
        warnings.append("未能从试卷识别出大题结构，封面得分表无法按题号对齐")
        return PaperOcrResult({}, section_defs, 0, warnings)

    cover_rows: dict[int, CoverScoreRow] = {}
    for cover_idx in range(cover_count):
        parsed_rows = [
            row
            for row in (
                parse_cover_score_table(html, section_count)
                for html in tables.get(cover_idx, [])
            )
            if row is not None
        ]
        # 同一页可能还有大题旁的独立得分小框，取真正读到数的那张汇总表
        row = next((r for r in parsed_rows if r.section_scores), None) or next(
            iter(parsed_rows), None
        )
        if row is None:
            warnings.append(f"第{cover_idx + 1}份试卷封面未识别到「大题/得分」表")
            continue
        cover_rows[cover_idx] = row
        warnings.extend(f"第{cover_idx + 1}份试卷：{issue}" for issue in row.issues)
    return PaperOcrResult(cover_rows, section_defs, section_count, warnings)


def resolve_applicable_orders(result: PaperOcrResult) -> tuple[list[int], list[str]]:
    """确定本教学班实际考的大题。

    试卷里有专业专属大题（「一、汉译英（工程力学专业）」与
    「三、证明题（非工程力学专业）」二选一）。专属大题在全部样本封面上都没有得分时
    按不考处理，否则它的满分会把整卷得分率系统性拉低。
    """
    warnings: list[str] = []
    restricted = {d.order_no: d for d in result.section_defs if d.applicable_major}
    scored = {
        order for row in result.cover_rows.values() for order in row.section_scores
    }
    applicable: list[int] = []
    for order in range(1, result.section_count + 1):
        limit = restricted.get(order)
        if limit is not None and order not in scored:
            warnings.append(
                f"第{order}题（{limit.question_type}，限{limit.applicable_major}）"
                "在全部样本试卷上均无得分，按本教学班不考该题处理"
            )
            continue
        applicable.append(order)
    return applicable, warnings


def _alignment_total(row: CoverScoreRow | None) -> tuple[Decimal | None, bool]:
    """对齐用总分：总分列读到了就用它；总分列不可读（手写无法 OCR）但各题分
    读全时退回各题之和。返回 (总分, 是否为推算值)。"""
    if row is None:
        return None, False
    if row.total is not None:
        return row.total, False
    if row.section_scores and len(row.section_scores) == row.section_columns:
        return sum(row.section_scores.values(), Decimal(0)), True
    return None, False


def align_covers_to_students(
    cover_rows: dict[int, CoverScoreRow], cover_count: int, students: list
) -> tuple[dict[int, str], list[str]]:
    """封面 → 学生。封面总分与成绩单期末成绩同源，能唯一对上就用它对齐。"""
    warnings: list[str] = []
    pool = [st for st in students if str(st.flag or "") not in ("缺考", "违纪")]
    by_final: dict[Decimal, list] = {}
    for st in pool:
        if st.final_score is None:
            continue
        by_final.setdefault(Decimal(st.final_score), []).append(st)

    candidates: dict[int, list] = {}
    for cover_idx in range(cover_count):
        total, derived = _alignment_total(cover_rows.get(cover_idx))
        candidates[cover_idx] = (
            list(by_final.get(Decimal(total), [])) if total is not None else []
        )
        if derived and candidates[cover_idx]:
            warnings.append(
                f"第{cover_idx + 1}份试卷总分列无法识别，"
                f"按各题之和 {total} 对齐学生，请在核对阶段确认"
            )

    assigned: dict[int, str] = {}
    used: set[str] = set()
    # 候选唯一的封面先占位，避免它的学生被同分的另一份卷子先拿走
    for cover_idx in sorted(candidates, key=lambda i: len(candidates[i]) or 99):
        available = [st for st in candidates[cover_idx] if st.student_id not in used]
        if not available:
            continue
        picked = available[0]
        assigned[cover_idx] = picked.student_id
        used.add(picked.student_id)
        if len(available) > 1:
            names = "、".join(st.name for st in available[:3])
            warnings.append(
                f"第{cover_idx + 1}份试卷的卷面总分对应多名同分学生（{names}），"
                f"暂记到 {picked.name}，请在核对阶段确认"
            )

    leftovers = [st for st in pool if st.student_id not in used]
    for cover_idx in range(cover_count):
        if cover_idx in assigned:
            continue
        if not leftovers:
            warnings.append(f"第{cover_idx + 1}份试卷没有可对应的学生，未落库")
            continue
        st = leftovers.pop(0)
        assigned[cover_idx] = st.student_id
        warnings.append(
            f"第{cover_idx + 1}份试卷的卷面总分与成绩单期末成绩对不上，"
            f"暂按名单顺序记到 {st.name}，请在核对阶段确认"
        )
    return assigned, warnings


def build_cover_cells(
    *,
    cover_rows: dict[int, CoverScoreRow],
    cover_to_student: dict[int, str],
    cover_pages: list[int],
    sections_by_order: dict[int, str],
    full_scores: dict[int, Decimal],
    applicable_orders: list[int],
) -> tuple[list[dict], list[str]]:
    """封面读数 → 可落库 cells（含置信度与状态）。"""
    cells: list[dict] = []
    warnings: list[str] = []
    for cover_idx, student_id in sorted(cover_to_student.items()):
        row = cover_rows.get(cover_idx)
        if row is None:
            continue
        page_ref = (
            f"p{cover_pages[cover_idx]:02d}"
            if cover_idx < len(cover_pages)
            else f"cover{cover_idx + 1}"
        )
        missing: list[int] = []
        for order in applicable_orders:
            section_id = sections_by_order.get(order)
            if not section_id:
                continue
            value = row.section_scores.get(order)
            inferred = value is None and order in row.inferred_zero_orders
            if value is None and not inferred:
                missing.append(order)
                continue
            if inferred:
                confidence, status = _CONF_INFERRED_ZERO, "pending_review"
            elif row.total_consistent is True:
                confidence, status = _CONF_TOTAL_MATCH, "confirmed"
            elif row.total_consistent is False:
                confidence, status = _CONF_TOTAL_MISMATCH, "pending_review"
            else:
                confidence, status = _CONF_NO_TOTAL, "pending_review"
            score = value if value is not None else Decimal(0)
            cells.append(
                {
                    "student_id": student_id,
                    "section_id": section_id,
                    "score": score,
                    "raw_score": score,
                    "full_score": full_scores.get(order) or Decimal(0),
                    "source": "ocr_table",
                    "confidence": confidence,
                    "status": status,
                    "channel_agree": row.total_consistent,
                    "page_ref": page_ref,
                }
            )
        if missing:
            orders = "、".join(f"第{o}题" for o in missing)
            warnings.append(
                f"第{cover_idx + 1}份试卷的 {orders} 未识别到得分，请在核对阶段补录"
            )
    return cells, warnings


async def _mineru_content_list(pdf_bytes: bytes) -> tuple[list, list[str]]:
    """跑一次 MinerU，返回 (content_list, warnings)。失败时返回空列表而不抛。"""
    import tempfile
    from pathlib import Path as _P

    from backend.config.settings import settings
    from backend.core.tools.ocr.base_ocr import OCRConfig
    from backend.core.tools.ocr.mineru_ocr import MinerUOCR

    warnings: list[str] = []
    cfg = OCRConfig(
        backend=settings.mineru_backend,
        lang=settings.mineru_lang,
        method=settings.mineru_method,
        formula_enable=settings.mineru_formula_enable,
        table_enable=settings.mineru_table_enable,
    )
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    # 扫描件子 PDF 可达数十 MB,同步写盘会阻塞事件循环,离线到线程执行。
    await asyncio.to_thread(_P(tmp_path).write_bytes, pdf_bytes)
    ocr = MinerUOCR(api_url=settings.mineru_api_url, config=cfg)
    try:
        task_id = await ocr.submit_task_async(tmp_path)
        result = await ocr.wait_for_completion_async(
            task_id, timeout=900, poll_interval=3
        )
        if not result.is_success():
            warnings.append(f"MinerU 任务未完成：{result.status}")
            return [], warnings
        data = await ocr.get_task_data_async(task_id, include_fields=["content_list"])
        return list(data.content_list or []), warnings
    except Exception as exc:  # noqa: BLE001
        logger.warning("[CourseEvalTool] MinerU 识别试卷失败: %s", exc)
        warnings.append(f"MinerU 自动识别暂不可用（{exc}）")
        return [], warnings
    finally:
        await ocr.close()
        _P(tmp_path).unlink(missing_ok=True)


async def ocr_paper_structure(
    *,
    pdf_bytes: bytes,
    cover_pages: list[int],
    body_pages: list[int],
    declared_section_count: int | None = None,
) -> PaperOcrResult:
    """封面页 + 首份试卷正文页合成子 PDF 跑一次 MinerU，解析结构与封面得分。"""
    sub_pdf = await asyncio.to_thread(
        compose_sub_pdf, pdf_bytes, list(cover_pages) + list(body_pages)
    )
    content_list, warnings = await _mineru_content_list(sub_pdf)
    if not content_list:
        return PaperOcrResult({}, [], 0, warnings)
    result = parse_paper_ocr(
        content_list,
        cover_count=len(cover_pages),
        declared_section_count=declared_section_count,
    )
    return PaperOcrResult(
        result.cover_rows,
        result.section_defs,
        result.section_count,
        warnings + result.warnings,
    )


def compose_sub_pdf(pdf_bytes: bytes, page_indices: list[int]) -> bytes:
    """用 PyMuPDF 抽指定页合成子 PDF（缩小 MinerU 输入）。"""
    import io

    import pymupdf

    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = pymupdf.open()
    try:
        for idx in page_indices:
            out.insert_pdf(src, from_page=idx, to_page=idx)
        buf = io.BytesIO()
        out.save(buf)
        return buf.getvalue()
    finally:
        src.close()
        out.close()


def section_full_scores_by_major(
    section_defs: list[SectionDef],
) -> dict[str, Decimal]:
    """按专业汇总卷面满分（专属大题只计给对应专业）。"""
    majors = sorted({d.applicable_major for d in section_defs if d.applicable_major})
    totals: dict[str, Decimal] = {}
    for major in majors:
        total = Decimal(0)
        for definition in section_defs:
            if definition.total_score is None:
                continue
            if definition.applicable_major in (None, major):
                total += Decimal(definition.total_score)
        totals[major] = total
    return totals
