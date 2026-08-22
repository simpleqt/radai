"""课程评价智能体工具集的组装入口。

工具实现按职责分布在同包模块中：

- ``read_tools``：6 个只读工具（真实读库）
- ``extraction_tools``：成绩单 / 试卷 / 大纲三个解析落库工具
- ``pipeline_tools``：状态迁移 / 校验 / 映射建议 / 统计 / 报告
- ``dialog_tools``：对话式建任务 / 绑材料 / 网格核对 / 改分 / 确认
- ``_shared``：共享运行时辅助（会话、材料读取、LLM JSON 调用等）

``backend.application.course_eval.tools`` 是保持既有 import 路径
（registry.yaml 的 provider 与测试）的门面。
"""

from __future__ import annotations

from backend.application.course_eval.agent_tools.dialog_tools import (
    build_dialog_tools,
)
from backend.application.course_eval.agent_tools.extraction_tools import (
    build_extraction_tools,
)
from backend.application.course_eval.agent_tools.pipeline_tools import (
    build_pipeline_tools,
)
from backend.application.course_eval.agent_tools.read_tools import build_read_tools


def get_course_eval_tools(*, runtime_context=None):
    """构建课程评价流水线工具集（读 + 写），顺序与历史版本保持一致。"""
    read_tools = build_read_tools(runtime_context)
    (
        transition_task,
        run_validation,
        suggest_goal_mapping,
        compute_statistics,
        generate_report,
        export_report_pdf,
    ) = build_pipeline_tools(runtime_context)
    extract_grade_sheet, extract_paper, extract_syllabus = build_extraction_tools(
        runtime_context
    )
    return [
        *read_tools,
        transition_task,
        extract_grade_sheet,
        extract_paper,
        extract_syllabus,
        run_validation,
        suggest_goal_mapping,
        compute_statistics,
        generate_report,
        export_report_pdf,
        *(
            build_dialog_tools(runtime_context)
            if runtime_context is not None
            else []
        ),
    ]


def _get_tool(name: str, runtime_context=None):
    return next(
        item
        for item in get_course_eval_tools(runtime_context=runtime_context)
        if item.name == name
    )


def get_list_course_eval_tasks_tool(*, runtime_context=None):
    return _get_tool("list_course_eval_tasks", runtime_context)


def get_course_stats_tool(*, runtime_context=None):
    return _get_tool("get_course_stats", runtime_context)


def get_achievement_tool(*, runtime_context=None):
    return _get_tool("get_achievement", runtime_context)


def get_suggest_goal_mapping_tool(*, runtime_context=None):
    return _get_tool("suggest_goal_mapping", runtime_context)


def get_draft_report_narrative_tool(*, runtime_context=None):
    return _get_tool("draft_report_narrative", runtime_context)


def get_check_report_quality_tool(*, runtime_context=None):
    return _get_tool("check_report_quality", runtime_context)


def get_load_previous_reports_tool(*, runtime_context=None):
    return _get_tool("load_previous_reports", runtime_context)


def get_transition_task_tool(*, runtime_context=None):
    return _get_tool("transition_task", runtime_context)


def get_extract_grade_sheet_tool(*, runtime_context=None):
    return _get_tool("extract_grade_sheet", runtime_context)


def get_extract_paper_tool(*, runtime_context=None):
    return _get_tool("extract_paper", runtime_context)


def get_extract_syllabus_tool(*, runtime_context=None):
    return _get_tool("extract_syllabus", runtime_context)


def get_run_validation_tool(*, runtime_context=None):
    return _get_tool("run_validation", runtime_context)


def get_compute_statistics_tool(*, runtime_context=None):
    return _get_tool("compute_statistics", runtime_context)


def get_generate_report_tool(*, runtime_context=None):
    return _get_tool("generate_report", runtime_context)


def get_export_report_pdf_tool(*, runtime_context=None):
    return _get_tool("export_report_pdf", runtime_context)


def get_create_semester_task_tool(*, runtime_context=None):
    return _get_tool("create_semester_task", runtime_context)


def get_attach_material_tool(*, runtime_context=None):
    return _get_tool("attach_material", runtime_context)


def get_score_grid_tool(*, runtime_context=None):
    return _get_tool("get_score_grid", runtime_context)


def get_update_scores_tool(*, runtime_context=None):
    return _get_tool("update_scores", runtime_context)


def get_confirm_scores_tool(*, runtime_context=None):
    return _get_tool("confirm_scores", runtime_context)


def get_apply_mappings_tool(*, runtime_context=None):
    return _get_tool("apply_mappings", runtime_context)
