"""课程达成度评价流水线的展示 presenter。

改造前这条流水线的 20 个工具全部落进通用分支：步骤名是「使用工具 extract_paper」，
结果摘要是原始 JSON 的前 320 字符。这里给每个工具一个人读标题、关键指标和
结构化视图；三个需要教师回应的环节（试卷得分核对、数据校验、目标映射）附动作卡。
"""

from __future__ import annotations

from typing import Any

from backend.application.agent_executors.tool_presenters.common import (
    json_error_display,
)
from backend.application.agent_executors.tool_presenters.types import (
    DisplayAction,
    DisplayActionOption,
    DisplayChip,
    ToolDisplay,
    ToolPresenterInput,
)

PHASES: dict[str, str] = {
    "create_semester_task": "task.create",
    "attach_material": "material.bind",
    "extract_grade_sheet": "material.extract",
    "extract_paper": "material.extract",
    "extract_syllabus": "material.extract",
    "run_validation": "data.validate",
    "get_score_grid": "score.review",
    "update_scores": "score.review",
    "confirm_scores": "score.review",
    "suggest_goal_mapping": "mapping",
    "apply_mappings": "mapping",
    "compute_statistics": "stats",
    "get_course_stats": "stats",
    "get_achievement": "stats",
    "generate_report": "report",
    "draft_report_narrative": "report",
    "check_report_quality": "report",
    "transition_task": "meta",
    "list_course_eval_tasks": "meta",
    "load_previous_reports": "meta",
}

LABELS: dict[str, str] = {
    "create_semester_task": "创建学期任务",
    "attach_material": "绑定材料",
    "extract_grade_sheet": "解析课程成绩单",
    "extract_paper": "解析答题试卷",
    "extract_syllabus": "抽取教学大纲课程目标",
    "run_validation": "数据校验",
    "get_score_grid": "读取分数网格",
    "update_scores": "修正得分",
    "confirm_scores": "确认得分",
    "suggest_goal_mapping": "生成试题-目标映射建议",
    "apply_mappings": "落库目标映射",
    "compute_statistics": "计算统计快照",
    "get_course_stats": "读取成绩分布",
    "get_achievement": "读取课程目标达成度",
    "generate_report": "生成报告草稿",
    "draft_report_narrative": "起草报告定性文字",
    "check_report_quality": "报告质检",
    "transition_task": "推进任务状态",
    "list_course_eval_tasks": "列出学期任务",
    "load_previous_reports": "读取历史达成度",
}

_TASK_STATUS_LABELS: dict[str, str] = {
    "created": "已创建",
    "materials_ready": "材料就绪",
    "extracted": "已抽取",
    "needs_review": "待教师复核",
    "mapped": "映射完成",
    "computed": "已计算",
    "report_draft": "报告草稿",
    "report_final": "报告定稿",
    "archived": "已归档",
}


# ── 小工具 ────────────────────────────────────────────────────────────


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any) -> str:
    return str(value or "").strip()


def _short_id(value: Any, size: int = 8) -> str:
    text = _str(value)
    return text[:size] if text else ""


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in (value or []) if isinstance(row, dict)]


def _kv(label: str, value: Any, tone: str = "info") -> dict[str, Any]:
    return {"label": label, "value": _str(value) or "—", "tone": tone}


def _label(tool_name: str) -> str:
    return LABELS.get(tool_name, tool_name)


# ── 各工具 presenter ──────────────────────────────────────────────────


def _create_semester_task(data: dict[str, Any], name: str) -> ToolDisplay:
    status = _str(data.get("status"))
    chips = [DisplayChip(text=f"状态: {_TASK_STATUS_LABELS.get(status, status)}")]
    if data.get("auto_generated"):
        chips.append(DisplayChip(text="课程代码自动生成", tone="warning"))
    return ToolDisplay(
        label=_label(name),
        status_text=f"{_str(data.get('course_name'))} · {_str(data.get('semester'))} · {_str(data.get('class_no'))} 班",
        chips=chips,
        view="kv",
        data={
            "rows": [
                _kv("课程", data.get("course_name")),
                _kv("课程代码", data.get("course_code")),
                _kv("学期", data.get("semester")),
                _kv("教学班", data.get("class_no")),
            ]
        },
    )


def _attach_material(data: dict[str, Any], name: str) -> ToolDisplay:
    material_type = _str(data.get("material_type"))
    type_labels = {"grade_sheet": "课程成绩单", "syllabus": "教学大纲", "paper": "答题试卷"}
    readable = type_labels.get(material_type, material_type)
    return ToolDisplay(
        label=_label(name),
        status_text=f"已绑定{readable}",
        chips=[DisplayChip(text=f"版本 v{_int(data.get('version_no'), 1)}")],
        view="kv",
        data={
            "rows": [
                _kv("材料类型", readable),
                _kv("材料编号", _short_id(data.get("material_id"))),
            ]
        },
    )


def _extract_grade_sheet(data: dict[str, Any], name: str) -> ToolDisplay:
    records = _int(data.get("records"))
    written = _int(data.get("written"))
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}

    rows = [_kv("识别记录", f"{records} 条"), _kv("入库学生", f"{written} 人")]
    rows.extend(_kv(str(key), value) for key, value in list(summary.items())[:8])

    return ToolDisplay(
        label=_label(name),
        status_text=f"识别 {records} 条成绩记录，入库 {written} 名学生",
        chips=[
            DisplayChip(text=f"记录 {records}"),
            DisplayChip(text=f"学生 {written}", tone="success" if written else "warning"),
        ],
        view="kv",
        data={"rows": rows},
    )


def _extract_paper(data: dict[str, Any], name: str) -> ToolDisplay:
    sections = _rows(data.get("sections"))
    sample = _int(data.get("paper_sample_n"))
    cells = _int(data.get("score_cells"))
    pending = _int(data.get("pending_review_cells"))
    warnings = [w for w in (data.get("auto_extract_warnings") or []) if _str(w)]

    chips = [
        DisplayChip(text=f"试卷 {sample} 份"),
        DisplayChip(text=f"大题 {len(sections)}"),
        DisplayChip(text=f"得分 {cells} 条"),
    ]
    if pending:
        chips.append(DisplayChip(text=f"待核对 {pending}", tone="warning"))
    if warnings:
        chips.append(DisplayChip(text=f"识别提醒 {len(warnings)}", tone="warning"))

    # 不在此处放确认动作：解析后紧接着的 get_score_grid 卡会带明细与确认按钮，
    # 两个环节各弹一张「待确认」会让教师重复核对同一批格子
    return ToolDisplay(
        label=_label(name),
        status_text=(
            f"{sample} 份试卷封面识别完成，写入 {cells} 条得分"
            + (f"，其中 {pending} 条待核对（明细见分数核对卡）" if pending else "")
        ),
        chips=chips,
        view="table",
        data={
            "columns": [
                {"key": "order_no", "label": "题号", "align": "right"},
                {"key": "question_type", "label": "题型"},
                {"key": "total_score", "label": "满分", "align": "right"},
                {"key": "counted", "label": "本班计入"},
            ],
            "rows": [
                {
                    "order_no": section.get("order_no"),
                    "question_type": _str(section.get("question_type")) or "—",
                    "total_score": section.get("total_score"),
                    "counted": "是" if section.get("counted_in_sample") else "否",
                }
                for section in sections
            ],
        },
    )


def _extract_syllabus(data: dict[str, Any], name: str) -> ToolDisplay:
    written = _int(data.get("written"))
    course = data.get("course") if isinstance(data.get("course"), dict) else {}
    formula = (
        data.get("score_formula") if isinstance(data.get("score_formula"), dict) else {}
    )

    rows = [_kv("课程目标", f"{written} 条")]
    rows.extend(_kv(str(key), value) for key, value in list(course.items())[:4])
    rows.extend(_kv(str(key), value) for key, value in list(formula.items())[:4])

    return ToolDisplay(
        label=_label(name),
        status_text=f"抽取并入库 {written} 条课程目标",
        chips=[DisplayChip(text=f"目标 {written}", tone="success" if written else "warning")],
        view="kv",
        data={"rows": rows},
    )


def _run_validation(data: dict[str, Any], name: str) -> ToolDisplay:
    issues = _rows(data.get("issues"))
    error_count = _int(data.get("error_count"))
    warning_count = sum(1 for issue in issues if _str(issue.get("level")) == "warning")
    passed = bool(data.get("passed"))

    chips = []
    if error_count:
        chips.append(DisplayChip(text=f"错误 {error_count}", tone="error"))
    if warning_count:
        chips.append(DisplayChip(text=f"提醒 {warning_count}", tone="warning"))
    if passed:
        chips.append(DisplayChip(text="全部通过", tone="success"))

    if not issues:
        return ToolDisplay(
            label=_label(name),
            status_text="校验通过，未发现问题",
            chips=[DisplayChip(text="全部通过", tone="success")],
            view="text",
            data={"text": "成绩单与试卷得分数据一致，可继续目标映射。"},
        )

    action = None
    if error_count:
        action = DisplayAction(
            kind="choose",
            prompt=f"数据校验发现 {error_count} 项错误，处理后才能继续统计。",
            options=[
                DisplayActionOption(
                    label="逐条说明问题",
                    message="请逐条说明校验错误的具体原因和建议的处理方式。",
                ),
                DisplayActionOption(
                    label="我已知悉，继续",
                    message="这些校验问题我已知悉，请继续推进后续流程。",
                    tone="warning",
                ),
            ],
        )

    return ToolDisplay(
        label=_label(name),
        status_text=f"发现 {len(issues)} 项问题" + ("" if passed else "，建议教师确认后继续"),
        chips=chips,
        view="issues",
        data={
            "items": [
                {
                    "code": _str(issue.get("rule_id")),
                    "level": _str(issue.get("level")) or "info",
                    "message": _str(issue.get("detail")),
                    "locator": _str(issue.get("target")),
                }
                for issue in issues
            ]
        },
        action=action,
    )


def _get_score_grid(data: dict[str, Any], name: str) -> ToolDisplay:
    cells = _rows(data.get("low_confidence_cells"))
    row_issues = _rows(data.get("row_checks_failed"))
    students = _int(data.get("students"))
    sections = _int(data.get("sections"))

    chips = [DisplayChip(text=f"学生 {students}"), DisplayChip(text=f"大题 {sections}")]
    if cells:
        chips.append(DisplayChip(text=f"存疑 {len(cells)}", tone="warning"))
    if row_issues:
        chips.append(DisplayChip(text=f"行校验失败 {len(row_issues)}", tone="error"))

    action = None
    if cells or row_issues:
        action = DisplayAction(
            kind="confirm",
            prompt=(
                f"{len(cells)} 处得分未能自动确认"
                + (f"，{len(row_issues)} 名学生各题之和与总分不符" if row_issues else "")
                + "，请核对。"
            ),
            options=[
                DisplayActionOption(
                    label="确认识别无误",
                    message="这些自动识别的得分我确认无误，请把待核对项置为已确认。",
                    tone="success",
                )
            ],
            input_template="我要修正得分：<学生姓名> 第 <题号> 题 改为 <分数> 分",
        )

    return ToolDisplay(
        label=_label(name),
        status_text=(
            f"{students} 名学生 × {sections} 道大题"
            + (f"，{len(cells)} 处待核对" if cells else "，无待核对项")
        ),
        chips=chips,
        view="table",
        data={
            "columns": [
                {"key": "student", "label": "学生"},
                {"key": "section", "label": "题号"},
                {"key": "score", "label": "识别得分", "align": "right"},
                {"key": "confidence", "label": "置信度", "align": "right"},
            ],
            "rows": [
                {
                    "student": _str(cell.get("student_name")) or _short_id(cell.get("student_id")),
                    "section": _str(cell.get("section_label"))
                    or _short_id(cell.get("section_id"), 6),
                    "score": _str(cell.get("score")),
                    "confidence": _str(cell.get("confidence")) or "—",
                }
                for cell in cells
            ],
        }
        if cells
        else {},
        action=action,
    )


def _update_scores(data: dict[str, Any], name: str) -> ToolDisplay:
    updated = _int(data.get("updated"))
    return ToolDisplay(
        label=_label(name),
        status_text=f"已按你的指正修正 {updated} 处得分，下游统计已回退重算",
        chips=[DisplayChip(text=f"已修正 {updated}", tone="success")],
        view="text",
        data={"text": f"修正 {updated} 处得分，来源标记为人工，置信度 1.0，已写审计。"},
    )


def _confirm_scores(data: dict[str, Any], name: str) -> ToolDisplay:
    confirmed = _int(data.get("confirmed"))
    return ToolDisplay(
        label=_label(name),
        status_text=f"{confirmed} 处待核对得分已按你的确认置为已确认（分值未变）",
        chips=[DisplayChip(text=f"已确认 {confirmed}", tone="success")],
        view="text",
        data={"text": f"{confirmed} 处待核对得分已确认，可继续校验或推进映射。"},
    )


def _suggest_goal_mapping(data: dict[str, Any], name: str) -> ToolDisplay:
    suggestions = _rows(data.get("suggestions"))
    excluded = _str(data.get("excluded_sections"))

    chips = [DisplayChip(text=f"建议 {len(suggestions)} 条")]
    if excluded:
        chips.append(DisplayChip(text="含本班不考题剔除", tone="warning"))

    action = (
        DisplayAction(
            kind="confirm",
            prompt="以下映射建议尚未落库，确认后才会用于达成度计算。",
            options=[
                DisplayActionOption(
                    label="确认这份映射",
                    message="这份试题-目标映射建议我确认无误，请落库并继续计算统计。",
                    tone="success",
                )
            ],
            input_template="映射要调整：第 <题号> 题 应对应 <课程目标>，权重 <数值>",
        )
        if suggestions
        else None
    )

    return ToolDisplay(
        label=_label(name),
        status_text=f"生成 {len(suggestions)} 条映射建议（未落库）"
        + (f"；{excluded}" if excluded else ""),
        chips=chips,
        view="table",
        data={
            "columns": [
                {"key": "section", "label": "大题"},
                {"key": "goal", "label": "课程目标"},
                {"key": "weight", "label": "权重", "align": "right"},
            ],
            "rows": [
                {
                    "section": _str(item.get("section_label"))
                    or _short_id(item.get("section_id"), 6),
                    "goal": _str(item.get("goal_label"))
                    or _short_id(item.get("goal_id"), 6),
                    "weight": item.get("weight"),
                }
                for item in suggestions
            ],
        },
        action=action,
    )


def _apply_mappings(data: dict[str, Any], name: str) -> ToolDisplay:
    updated = _int(data.get("updated"))
    return ToolDisplay(
        label=_label(name),
        status_text=f"{updated} 条映射已按你的确认落库，下游统计已回退重算",
        chips=[DisplayChip(text=f"已落库 {updated}", tone="success")],
        view="text",
        data={"text": f"{updated} 条试题-目标映射已确认落库。"},
    )


def _compute_statistics(data: dict[str, Any], name: str) -> ToolDisplay:
    snapshot_id = _short_id(data.get("snapshot_id"))
    return ToolDisplay(
        label=_label(name),
        status_text="统计快照已生成，后续报告数字全部引用这份不可变快照",
        chips=[DisplayChip(text=f"快照 {snapshot_id}", tone="success")] if snapshot_id else [],
        view="text",
        data={"text": "成绩分布与各题得分率已计算并固化为快照。"},
    )


def _get_course_stats(data: dict[str, Any], name: str) -> ToolDisplay:
    final_stats = (
        data.get("final_score_stats")
        if isinstance(data.get("final_score_stats"), dict)
        else {}
    )
    section_rates = data.get("section_rates")
    blocking = data.get("blocking_issues")

    stat_labels = {
        "average": "平均分",
        "max": "最高分",
        "min": "最低分",
        "pass_rate": "及格率",
        "excellent_rate": "优秀率",
        "std": "标准差",
        "count": "样本数",
    }
    rows = [
        _kv(stat_labels.get(str(key), str(key)), value)
        for key, value in list(final_stats.items())[:8]
    ]

    chips = []
    if final_stats.get("average") is not None:
        chips.append(DisplayChip(text=f"平均分 {final_stats['average']}"))
    if final_stats.get("pass_rate") is not None:
        chips.append(DisplayChip(text=f"及格率 {final_stats['pass_rate']}"))
    if isinstance(section_rates, list) and section_rates:
        chips.append(DisplayChip(text=f"大题得分率 {len(section_rates)} 项"))
    if blocking:
        chips.append(DisplayChip(text="存在阻塞项", tone="warning"))

    return ToolDisplay(
        label=_label(name),
        status_text="已读取成绩分布快照" if rows else "暂无统计快照",
        chips=chips,
        view="kv",
        data={"rows": rows},
    )


def _get_achievement(data: dict[str, Any], name: str) -> ToolDisplay:
    items = _rows(data.get("items"))
    blocked = [item for item in items if _str(item.get("blocking"))]

    chips = [DisplayChip(text=f"课程目标 {len(items)}")]
    if blocked:
        chips.append(DisplayChip(text=f"未能计算 {len(blocked)}", tone="warning"))

    return ToolDisplay(
        label=_label(name),
        status_text=(
            f"{len(items)} 个课程目标"
            + (f"，其中 {len(blocked)} 个因数据缺失未能计算" if blocked else "达成度计算完成")
        ),
        chips=chips,
        view="table",
        data={
            "columns": [
                {"key": "goal", "label": "课程目标"},
                {"key": "achievement", "label": "达成度", "align": "right"},
                {"key": "support", "label": "支撑分", "align": "right"},
                {"key": "note", "label": "备注"},
            ],
            "rows": [
                {
                    "goal": _str(item.get("goal_code")),
                    "achievement": item.get("achievement") if item.get("achievement") is not None else "—",
                    "support": item.get("support_score") if item.get("support_score") is not None else "—",
                    "note": _str(item.get("blocking")) or "—",
                }
                for item in items
            ],
        },
    )


def _generate_report(data: dict[str, Any], name: str) -> ToolDisplay:
    report_id = _short_id(data.get("report_id"))
    achievements = data.get("goal_achievements")
    blocking = data.get("blocking_issues")

    chips = [DisplayChip(text=f"报告 {report_id}", tone="success")] if report_id else []
    if isinstance(achievements, list) and achievements:
        chips.append(DisplayChip(text=f"目标 {len(achievements)}"))
    if blocking:
        chips.append(DisplayChip(text="存在阻塞项", tone="warning"))

    return ToolDisplay(
        label=_label(name),
        status_text="报告草稿已生成并落库，数字全部引用统计快照",
        chips=chips,
        view="artifact",
        data={
            "title": f"课程达成度评价报告草稿 {report_id}".strip(),
            "kind": _str(data.get("status")) or "draft",
        },
    )


def _draft_report_narrative(data: dict[str, Any], name: str) -> ToolDisplay:
    draft = _str(data.get("draft"))
    section = _str(data.get("section"))
    section_labels = {
        "problems": "存在问题",
        "measures": "改进措施",
        "analysis": "结果分析",
    }
    return ToolDisplay(
        label=f"{_label(name)}（{section_labels.get(section, section or '正文')}）",
        status_text="草稿只含定性文字，数字由渲染器从统计快照填入",
        chips=[DisplayChip(text=f"{len(draft)} 字")] if draft else [],
        view="text",
        data={"text": draft},
    )


def _check_report_quality(data: dict[str, Any], name: str) -> ToolDisplay:
    issues = _rows(data.get("issues"))
    passed = bool(data.get("passed"))
    error_count = sum(1 for issue in issues if _str(issue.get("level")) == "error")
    warning_count = sum(1 for issue in issues if _str(issue.get("level")) == "warning")

    chips = []
    if error_count:
        chips.append(DisplayChip(text=f"错误 {error_count}", tone="error"))
    if warning_count:
        chips.append(DisplayChip(text=f"提醒 {warning_count}", tone="warning"))
    if passed:
        chips.append(DisplayChip(text="质检通过", tone="success"))

    if not issues:
        return ToolDisplay(
            label=_label(name),
            status_text="质检通过，未发现问题",
            chips=chips,
            view="text",
            data={"text": "QC-01~QC-10 规则集全部通过。"},
        )

    return ToolDisplay(
        label=_label(name),
        status_text=f"质检发现 {len(issues)} 项问题",
        chips=chips,
        view="issues",
        data={
            "items": [
                {
                    "code": _str(issue.get("rule_id")),
                    "level": _str(issue.get("level")) or "info",
                    "message": _str(issue.get("detail")),
                }
                for issue in issues
            ]
        },
    )


def _transition_task(data: dict[str, Any], name: str) -> ToolDisplay:
    status = _str(data.get("status"))
    readable = _TASK_STATUS_LABELS.get(status, status)
    return ToolDisplay(
        label=_label(name),
        status_text=f"任务状态已推进到「{readable}」",
        chips=[DisplayChip(text=readable, tone="success")] if readable else [],
        view="kv",
        data={"rows": [_kv("当前状态", readable)]},
    )


def _list_course_eval_tasks(data: dict[str, Any], name: str) -> ToolDisplay:
    items = _rows(data.get("items"))
    return ToolDisplay(
        label=_label(name),
        status_text=f"共 {_int(data.get('total'), len(items))} 个学期任务",
        chips=[DisplayChip(text=f"任务 {len(items)}")],
        view="table",
        data={
            "columns": [
                {"key": "semester", "label": "学期"},
                {"key": "class_no", "label": "教学班"},
                {"key": "status", "label": "状态"},
            ],
            "rows": [
                {
                    "semester": _str(item.get("semester")),
                    "class_no": _str(item.get("class_no")),
                    "status": _TASK_STATUS_LABELS.get(
                        _str(item.get("status")), _str(item.get("status"))
                    ),
                }
                for item in items
            ],
        },
    )


def _load_previous_reports(data: dict[str, Any], name: str) -> ToolDisplay:
    goals = _rows(data.get("goals"))
    return ToolDisplay(
        label=_label(name),
        status_text=f"读取到 {len(goals)} 条历史课程目标与权重（不含报告正文）",
        chips=[DisplayChip(text=f"目标 {len(goals)}")],
        view="table",
        data={
            "columns": [
                {"key": "goal_code", "label": "课程目标"},
                {"key": "weight", "label": "考试权重", "align": "right"},
            ],
            "rows": [
                {
                    "goal_code": _str(goal.get("goal_code")),
                    "weight": _str(goal.get("weight_exam")) or "—",
                }
                for goal in goals
            ],
        },
    )


_HANDLERS = {
    "create_semester_task": _create_semester_task,
    "attach_material": _attach_material,
    "extract_grade_sheet": _extract_grade_sheet,
    "extract_paper": _extract_paper,
    "extract_syllabus": _extract_syllabus,
    "run_validation": _run_validation,
    "get_score_grid": _get_score_grid,
    "update_scores": _update_scores,
    "confirm_scores": _confirm_scores,
    "suggest_goal_mapping": _suggest_goal_mapping,
    "apply_mappings": _apply_mappings,
    "compute_statistics": _compute_statistics,
    "get_course_stats": _get_course_stats,
    "get_achievement": _get_achievement,
    "generate_report": _generate_report,
    "draft_report_narrative": _draft_report_narrative,
    "check_report_quality": _check_report_quality,
    "transition_task": _transition_task,
    "list_course_eval_tasks": _list_course_eval_tasks,
    "load_previous_reports": _load_previous_reports,
}


def _make_presenter(tool_name: str):
    handler = _HANDLERS[tool_name]

    def presenter(payload: ToolPresenterInput) -> ToolDisplay | None:
        parsed = payload.parsed_output()
        if parsed is None:
            return None
        if _str(parsed.get("error")):
            error = json_error_display(payload)
            if error is None:
                return None
            # 保留人读步骤名，只把结论换成错误原因。
            return ToolDisplay(
                label=_label(tool_name),
                status_text=error.status_text,
                chips=error.chips,
                view="text",
                data=error.data,
            )
        return handler(parsed, tool_name)

    return presenter


def install() -> None:
    from backend.application.agent_executors import tool_presenters  # noqa: PLC0415

    tool_presenters.register_many(
        {name: _make_presenter(name) for name in _HANDLERS},
        phases=PHASES,
    )
