"""基础力学课程智能体 · 教师提问分析（application 层编排）。

- ``collector``: 提问旁路采集 + 确定性统计（趋势/精确高频/关键词）。
- ``clustering``: LLM 语义聚类与教学总结（失败降级为确定性结果）。
- ``service``: 分析报告生命周期（生成/查询/删除）。
"""

from backend.application.question_analytics.collector import (
    TEACHER_ANALYTICS_AGENT_CODE,
    overview_stats,
    question_page,
    record_question,
    schedule_question_collection,
    should_collect_question,
)
from backend.application.question_analytics.service import (
    create_report,
    delete_report,
    get_report,
    list_reports,
)

__all__ = [
    "TEACHER_ANALYTICS_AGENT_CODE",
    "should_collect_question",
    "record_question",
    "schedule_question_collection",
    "question_page",
    "overview_stats",
    "create_report",
    "list_reports",
    "get_report",
    "delete_report",
]
