"""Excel 实验数据分析工具集

为智能体提供 5 个 LangChain 工具：
  1. list_experiment_datasets  — 发现服务器上预注册的实验数据集
  2. inspect_excel_data        — 探查文件/数据集的表结构（支持多数据集）
  3. query_excel_data          — SQL 查询数据（支持跨数据集 JOIN）
  4. summarize_excel_data      — 生成统计摘要
  5. visualize_excel_data      — 可视化图表生成

数据源支持（三种模式）：
  - dataset_code:  使用单个服务器预注册数据集
  - dataset_codes: 使用多个数据集（跨数据集 JOIN 模式）
  - file_path:     用户上传的文件（绝对路径）
"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.domain.tools.runtime_context import ToolRuntimeContext
from langchain_core.tools import StructuredTool

from backend.application.tools.providers.excel_analysis_access import (  # noqa: F401  (兼容 re-export)
    _EXCEL_ACCESS_CONTEXT,
    _ExcelAccessContext,
    _TOOL_PARAM_TAG_RE,
    _agent_aliases,
    _clean_string_list,
    _clean_tool_string,
    _current_access_context,
    _dataset_grants,
    _dataset_matches_agent,
    _error,
    _filter_accessible_datasets,
    _is_builtin_project_table_path,
    _is_guest_access,
    _lookup_dataset_path,
    _resolve_file_path,
    _resolve_multi_file_paths,
)
from backend.application.tools.providers.excel_analysis_query import (  # noqa: F401  (兼容 re-export)
    _MAX_MD_ROWS,
    _serialize_query_result,
    inspect_excel_data,
    list_experiment_datasets,
    query_excel_data,
)
from backend.application.tools.providers.excel_analysis_insight import (  # noqa: F401  (兼容 re-export)
    _SUMMARY_COLUMN_LABELS,
    _build_chart_payload,
    _format_table_statistics,
    summarize_excel_data,
    visualize_excel_data,
)

logger = get_logger(__name__)


def _scope_excel_tool(tool_item, access: _ExcelAccessContext):
    original_coroutine = getattr(tool_item, "coroutine", None)
    if original_coroutine is None:
        return tool_item

    async def invoke_scoped(**kwargs):
        token = _EXCEL_ACCESS_CONTEXT.set(access)
        try:
            return await original_coroutine(**kwargs)
        finally:
            _EXCEL_ACCESS_CONTEXT.reset(token)

    return StructuredTool.from_function(
        coroutine=invoke_scoped,
        name=tool_item.name,
        description=tool_item.description,
        args_schema=tool_item.args_schema,
        return_direct=tool_item.return_direct,
        response_format=tool_item.response_format,
    )


def get_excel_analysis_tools(
    *,
    runtime_context: ToolRuntimeContext | None = None,
) -> list:
    """返回绑定请求用户身份的 Excel 分析工具列表。"""
    tools = [
        list_experiment_datasets,
        inspect_excel_data,
        query_excel_data,
        summarize_excel_data,
        visualize_excel_data,
    ]
    if runtime_context is None:
        return tools

    access = _ExcelAccessContext(
        user_id=str(runtime_context.user_id or "").strip(),
        role=str(runtime_context.role or "user").strip().lower() or "user",
        agent_code=str(runtime_context.agent_code or "").strip().lower(),
    )
    return [_scope_excel_tool(tool_item, access) for tool_item in tools]
