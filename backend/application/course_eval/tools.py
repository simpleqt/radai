"""课程评价智能体工具（流水线执行者）——兼容门面。

读工具：真实读库（不再占位）。
写工具：transition_task / extract_grade_sheet / extract_paper / extract_syllabus /
       run_validation / suggest_goal_mapping / compute_statistics / generate_report。

铁律（对齐 §8.3 的升级版）：
- 写工具全部走 course_eval **service 层**（task_service.transition /
  material_service / report_service.compute 等），状态机护栏 + 审计 +
  assert_can_edit_data 天然生效；operator_id 取自 runtime_context.user_id，
  绝不让模型裸写 ORM。
- 数字正确性：解析 / 计算 / 渲染全是确定性代码，LLM 只做大纲抽取、映射建议、
  报告定性文字。LLM 产出的映射建议不落库，教师经 REST PUT /mappings 确认。
- 写工具在 registry.yaml 声明 risk_level: write / side_effect: db_write，
  匿名与访客调用会被平台 guardrail 硬拒。

实现已按职责拆分：

- 试卷 OCR 领域逻辑 → ``backend.application.course_eval.paper_ocr``
- 工具闭包与组装 → ``backend.application.course_eval.agent_tools``（read /
  extraction / pipeline / dialog 四组 + ``_shared`` 运行时辅助）

本模块仅保留既有 import 路径（``backend/config/tools/registry.yaml`` 的
provider 与测试）所需的 re-export；测试如需 patch 运行时辅助，请打在
``agent_tools._shared``（或具体工具模块）上。
"""

from __future__ import annotations

from backend.application.course_eval.agent_tools import (
    _get_tool,
    get_achievement_tool,
    get_apply_mappings_tool,
    get_attach_material_tool,
    get_check_report_quality_tool,
    get_compute_statistics_tool,
    get_confirm_scores_tool,
    get_course_eval_tools,
    get_course_stats_tool,
    get_create_semester_task_tool,
    get_draft_report_narrative_tool,
    get_export_report_pdf_tool,
    get_extract_grade_sheet_tool,
    get_extract_paper_tool,
    get_extract_syllabus_tool,
    get_generate_report_tool,
    get_list_course_eval_tasks_tool,
    get_load_previous_reports_tool,
    get_run_validation_tool,
    get_score_grid_tool,
    get_suggest_goal_mapping_tool,
    get_transition_task_tool,
    get_update_scores_tool,
)
from backend.application.course_eval.agent_tools._shared import (
    _auto_material_type,
    _extract_text_bytes,
    _json,
    _llm_json_call,
    _llm_json_parse,
    _load_task,
    _open_session,
    _operator_id,
    _read_material_bytes,
    _read_snapshot,
    _resolve_file_display,
)
from backend.application.course_eval.extraction.cover_score_table import (
    CoverScoreRow,
    SectionDef,
)
from backend.application.course_eval.paper_ocr import (
    _CONF_INFERRED_ZERO,
    _CONF_NO_TOTAL,
    _CONF_TOTAL_MATCH,
    _CONF_TOTAL_MISMATCH,
    _STRUCTURE_BODY_PAGES,
    PaperOcrResult,
    _alignment_total,
    _cover_table_htmls,
    _mineru_content_list,
    _ocr_texts,
    _page_index,
    align_covers_to_students,
    build_cover_cells,
    compose_sub_pdf,
    ocr_paper_structure,
    parse_paper_ocr,
    resolve_applicable_orders,
    section_full_scores_by_major,
)

__all__ = [
    # 试卷 OCR 领域（paper_ocr）
    "PaperOcrResult",
    "parse_paper_ocr",
    "resolve_applicable_orders",
    "align_covers_to_students",
    "build_cover_cells",
    "ocr_paper_structure",
    "compose_sub_pdf",
    "section_full_scores_by_major",
    "CoverScoreRow",
    "SectionDef",
    "_STRUCTURE_BODY_PAGES",
    "_CONF_TOTAL_MATCH",
    "_CONF_INFERRED_ZERO",
    "_CONF_NO_TOTAL",
    "_CONF_TOTAL_MISMATCH",
    "_page_index",
    "_ocr_texts",
    "_cover_table_htmls",
    "_alignment_total",
    "_mineru_content_list",
    # 共享运行时辅助（agent_tools._shared）
    "_json",
    "_operator_id",
    "_open_session",
    "_read_material_bytes",
    "_load_task",
    "_llm_json_parse",
    "_llm_json_call",
    "_auto_material_type",
    "_resolve_file_display",
    "_read_snapshot",
    "_extract_text_bytes",
    # 工具组装与单工具入口（agent_tools）
    "get_course_eval_tools",
    "_get_tool",
    "get_list_course_eval_tasks_tool",
    "get_course_stats_tool",
    "get_achievement_tool",
    "get_suggest_goal_mapping_tool",
    "get_draft_report_narrative_tool",
    "get_check_report_quality_tool",
    "get_load_previous_reports_tool",
    "get_transition_task_tool",
    "get_extract_grade_sheet_tool",
    "get_extract_paper_tool",
    "get_extract_syllabus_tool",
    "get_run_validation_tool",
    "get_compute_statistics_tool",
    "get_generate_report_tool",
    "get_export_report_pdf_tool",
    "get_create_semester_task_tool",
    "get_attach_material_tool",
    "get_score_grid_tool",
    "get_update_scores_tool",
    "get_confirm_scores_tool",
    "get_apply_mappings_tool",
]
