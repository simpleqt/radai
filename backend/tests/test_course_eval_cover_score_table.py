"""封面得分表 / 大题结构解析测试（MinerU 单通道）。

HTML 样本直接取自 MinerU 对实际扫描卷的输出，含手写笔画造成的 colspan 噪声。
"""

from __future__ import annotations

from decimal import Decimal

from backend.application.course_eval.extraction.cover_score_table import (
    normalize_ocr_text,
    parse_cover_score_table,
    parse_html_table,
    parse_section_defs,
)

# 干净的一页：五个大题列 + 总分列，各题之和等于总分
CLEAN = (
    "<table><tr><td>大题</td><td>一</td><td>二</td><td>三</td><td>四</td>"
    "<td>五</td><td>总分</td></tr>"
    "<tr><td>得分</td><td></td><td>12</td><td>5</td><td>16</td><td>28</td>"
    "<td>61</td></tr></table>"
)

# 表头「三」被误判成 colspan=2，得分行也跟着多出一格
COLSPAN_NOISE = (
    "<table><tr><td rowspan=1 colspan=1>大题</td><td rowspan=1 colspan=1>一</td>"
    "<td rowspan=1 colspan=1>二</td><td rowspan=1 colspan=2>三</td>"
    "<td rowspan=1 colspan=1>四</td><td rowspan=1 colspan=1>五</td>"
    "<td rowspan=1 colspan=1>总分</td></tr>"
    "<tr><td rowspan=1 colspan=1>得分</td><td rowspan=1 colspan=1></td>"
    "<td rowspan=1 colspan=1>14</td><td rowspan=1 colspan=1>T</td>"
    "<td rowspan=1 colspan=1></td><td rowspan=1 colspan=1>19</td>"
    "<td rowspan=1 colspan=1>30</td><td rowspan=1 colspan=1>63</td></tr></table>"
)

# 手写「14」被读成「4」、「7」漏读：各题之和 61 ≠ 总分 78
MISREAD = (
    "<table><tr><td rowspan=1 colspan=1>大题</td><td rowspan=1 colspan=1>一</td>"
    "<td rowspan=1 colspan=1>二</td><td rowspan=1 colspan=2>三</td>"
    "<td rowspan=1 colspan=1>四</td><td rowspan=1 colspan=1>五</td>"
    "<td rowspan=1 colspan=1>总分</td></tr>"
    "<tr><td rowspan=1 colspan=1>得分</td><td rowspan=1 colspan=1></td>"
    "<td rowspan=1 colspan=1>4</td><td rowspan=1 colspan=2></td>"
    "<td rowspan=1 colspan=1>16</td><td rowspan=1 colspan=1>41</td>"
    "<td rowspan=1 colspan=1>78</td></tr>"
    "<tr><td rowspan=1 colspan=4></td><td rowspan=1 colspan=4></td></tr></table>"
)

# 大题旁的独立小得分框，不是汇总表
SIDE_BOX = "<table><tr><td>得分</td><td></td></tr></table>"


def test_parse_html_table_expands_colspan_into_grid_columns():
    rows = parse_html_table(COLSPAN_NOISE)
    header, score = rows[0], rows[1]
    assert [c.text for c in header] == ["大题", "一", "二", "三", "四", "五", "总分"]
    # 「三」占两列，所以「四」从第 4 列开始，得分行按网格列对齐
    assert [(c.col, c.span) for c in header][3] == (3, 2)
    assert header[4].col == 5
    assert [c.col for c in score] == [0, 1, 2, 3, 4, 5, 6, 7]


def test_clean_table_reads_all_scores_and_infers_blank_as_zero():
    row = parse_cover_score_table(CLEAN, 5)
    assert row is not None
    assert row.section_scores == {
        2: Decimal("12"),
        3: Decimal("5"),
        4: Decimal("16"),
        5: Decimal("28"),
    }
    assert row.total == Decimal("61")
    assert row.total_consistent is True
    # 各题之和已等于总分，空着的第 1 题只能是 0
    assert row.inferred_zero_orders == (1,)
    assert row.issues == ()


def test_colspan_noise_still_aligns_by_grid_column():
    row = parse_cover_score_table(COLSPAN_NOISE, 5)
    assert row is not None
    assert row.section_columns == 5
    assert row.section_scores == {
        2: Decimal("14"),
        4: Decimal("19"),
        5: Decimal("30"),
    }
    assert row.total == Decimal("63")
    assert row.total_consistent is True
    assert row.inferred_zero_orders == (1, 3)


def test_misread_digits_are_caught_by_total_cross_check():
    row = parse_cover_score_table(MISREAD, 5)
    assert row is not None
    assert row.total == Decimal("78")
    assert row.total_consistent is False
    # 校验不过时不补 0，整页交人工
    assert row.inferred_zero_orders == ()
    assert any("不一致" in issue for issue in row.issues)


def test_section_count_mismatch_returns_no_scores():
    row = parse_cover_score_table(CLEAN, 4)
    assert row is not None
    assert row.section_scores == {}
    assert row.section_columns == 5
    assert any("不符" in issue for issue in row.issues)


def test_side_score_box_is_not_a_summary_table():
    assert parse_cover_score_table(SIDE_BOX, 5) is None
    assert parse_cover_score_table("", 5) is None


def test_missing_total_column_disables_cross_check():
    html = (
        "<table><tr><td>大题</td><td>一</td><td>二</td></tr>"
        "<tr><td>得分</td><td>8</td><td>9</td></tr></table>"
    )
    row = parse_cover_score_table(html, 2)
    assert row is not None
    assert row.section_scores == {1: Decimal("8"), 2: Decimal("9")}
    assert row.total_consistent is None
    assert any("总分" in issue for issue in row.issues)


def test_conflicting_digits_in_one_span_are_dropped():
    html = (
        "<table><tr><td>大题</td><td colspan=2>一</td><td>总分</td></tr>"
        "<tr><td>得分</td><td>7</td><td>9</td><td>16</td></tr></table>"
    )
    row = parse_cover_score_table(html, 1)
    assert row is not None
    assert row.section_scores == {}
    assert any("多个数字" in issue for issue in row.issues)


def test_normalize_ocr_text_merges_math_wrapped_digits():
    """MinerU 把手写数字包成行内公式（字符间带空格），要还原成一个数。"""
    assert normalize_ocr_text("$2 0$") == "20"
    assert normalize_ocr_text(" $5 4$ ") == " 54 "
    assert normalize_ocr_text("$1 2 . 5$") == "12.5"
    # 公式外的空格不动，普通文本原样返回
    assert normalize_ocr_text("12 19") == "12 19"
    assert normalize_ocr_text("得分") == "得分"


def test_math_wrapped_scores_read_as_single_number():
    """2023-2024 真实卷：得分被 MinerU 输出为 $2 0$，此前被当成两个数字拒收。"""
    html = (
        "<table><tr><td>大题</td><td>一</td><td>二</td><td>三</td><td>总分</td></tr>"
        "<tr><td>得分</td><td>6</td><td> $2 0$ </td><td> $2 0$ </td><td>46</td></tr>"
        "</table>"
    )
    row = parse_cover_score_table(html, 3)
    assert row is not None
    assert row.section_scores == {
        1: Decimal("6"),
        2: Decimal("20"),
        3: Decimal("20"),
    }
    assert row.total == Decimal("46")
    assert row.total_consistent is True


def test_unreadable_total_cell_yields_none_total():
    """总分列是手写图片（<img>）时读不出总分，但各题分保留。"""
    html = (
        "<table><tr><td>大题</td><td>一</td><td>二</td><td>三</td><td>总分</td></tr>"
        "<tr><td>得分</td><td>12</td><td>19</td><td>47</td>"
        '<td><img src="images/x.jpg"/></td></tr></table>'
    )
    row = parse_cover_score_table(html, 3)
    assert row is not None
    assert row.section_scores == {
        1: Decimal("12"),
        2: Decimal("19"),
        3: Decimal("47"),
    }
    assert row.total is None
    assert row.total_consistent is None


def test_parse_section_defs_items_times_score_format():
    """2023-2024 卷标题写法：「10小题×2分=20分」（题数在前、=总分）。"""
    texts = [
        "一、填空题（10小题×2分=20分）",
        "二、作图题（2小题×10分=20分）",
        "三、计算题（4小题×15分=60分）",
        "一、填空题（10小题×2分=20分）得分",  # 同页粘连「得分」
    ]
    defs = parse_section_defs(texts)
    assert [d.order_no for d in defs] == [1, 2, 3]
    assert [d.question_type for d in defs] == ["填空题", "作图题", "计算题"]
    assert [d.total_score for d in defs] == [
        Decimal("20"),
        Decimal("20"),
        Decimal("60"),
    ]
    # 分值模式归一成「单题分×题数」
    assert [d.score_pattern for d in defs] == ["2分×10", "10分×2", "15分×4"]
    assert all(d.applicable_major is None for d in defs)


def test_parse_section_defs_items_format_without_equals_total():
    """「5小题×4分」没写 =总分 时按乘积推算满分。"""
    defs = parse_section_defs(["二、简答题（5小题×4分）"])
    assert defs[0].total_score == Decimal("20")
    assert defs[0].score_pattern == "4分×5"


def test_parse_section_defs_votes_across_pages():
    texts = [
        "一、汉译英。（1分×10，共10分）（工程力学专业）",
        "一、汉译英。（1分×10，共10分）（工程力学专业)",
        "二、填空题。（2分×10，共20分)",
        "三、证明题（5分×2，共10分）（非工程力学专业）",
        "四、画图题。（每小题10分，共20分)",
        "五、计算题。（10分×2+15分×2，共50分)",
        "1、强度 2、材料力学",  # 不是大题标题
    ]
    defs = parse_section_defs(texts)
    assert [d.order_no for d in defs] == [1, 2, 3, 4, 5]
    assert [d.question_type for d in defs] == [
        "汉译英",
        "填空题",
        "证明题",
        "画图题",
        "计算题",
    ]
    assert [d.total_score for d in defs] == [
        Decimal("10"),
        Decimal("20"),
        Decimal("10"),
        Decimal("20"),
        Decimal("50"),
    ]
    assert defs[0].applicable_major == "工程力学专业"
    assert defs[2].applicable_major == "非工程力学专业"
    assert defs[1].applicable_major is None
    assert defs[0].score_pattern == "1分×10"
