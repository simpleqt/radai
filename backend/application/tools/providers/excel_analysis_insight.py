"""Excel 分析洞察工具:表级统计汇总与图表可视化。(拆自 excel_data_analysis_tool.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.application.tools.providers.excel_engine import execute_multi_sql
from backend.application.tools.providers.excel_engine import execute_sql
from backend.application.tools.providers.excel_engine import get_schema
from backend.application.tools.providers.excel_engine import get_table_statistics
from langchain_core.tools import tool
from typing import Any
from typing import Optional
import asyncio
import uuid
from backend.application.tools.providers.excel_analysis_access import (
    _clean_string_list,
    _clean_tool_string,
    _current_access_context,
    _error,
    _resolve_file_path,
    _resolve_multi_file_paths,
)

logger = get_logger(__name__)


_SUMMARY_COLUMN_LABELS = {
    "column_name": "列名",
    "column_type": "数据类型",
    "count": "总行数",
    "null_count": "缺失数",
    "null_percentage": "缺失率(%)",
    "approx_unique": "估算唯一值",
    "min": "最小值",
    "q25": "25%分位",
    "q50": "中位数",
    "q75": "75%分位",
    "max": "最大值",
    "avg": "均值",
    "std": "标准差",
}


def _format_table_statistics(
    file_path: str,
    table_name: str,
    columns: list[str] | None,
    *,
    heading: str,
) -> list[str]:
    statistics = get_table_statistics(file_path, table_name, columns)
    summary = statistics["summary"]
    display_columns = [
        name for name in _SUMMARY_COLUMN_LABELS if name in summary.columns
    ]
    display = summary[display_columns].rename(columns=_SUMMARY_COLUMN_LABELS).round(4)
    lines = [
        f"\n{heading} 表：`{table_name}`（共 {statistics['row_count']} 行，"
        f"{statistics['column_count']} 列）",
        "\n**全表统计：**",
        display.to_markdown(index=False),
    ]

    missing = summary.loc[
        summary["null_count"] > 0,
        ["column_name", "null_count", "null_percentage"],
    ].rename(
        columns={
            "column_name": "列名",
            "null_count": "缺失数",
            "null_percentage": "缺失率(%)",
        }
    )
    if missing.empty:
        lines.append("\n✅ 无缺失值")
    else:
        lines.extend(
            ["\n**缺失值统计（仅列出有缺失的列）：**", missing.to_markdown(index=False)]
        )

    correlation = statistics["correlation"]
    if not correlation.empty:
        lines.extend(
            ["\n**数值列相关系数矩阵：**", correlation.round(3).to_markdown()]
        )
    omitted = statistics["correlation_omitted_columns"]
    if omitted:
        lines.append(
            "\n_相关性矩阵最多分析前 20 个数值列，未纳入："
            + ", ".join(omitted)
            + "_"
        )
    return lines


@tool("summarize_excel_data")
async def summarize_excel_data(
    dataset_code: Optional[str] = None,
    dataset_codes: Optional[list[str]] = None,
    file_id: Optional[str] = None,
    file_path: Optional[str] = None,
    sheet_name: Optional[str] = None,
    columns: Optional[list[str]] = None,
) -> str:
    """生成 Excel 数据的统计摘要。

    对数值列：count、均值(mean)、标准差(std)、最小值、四分位数、最大值
    对分类列：count、唯一值数、最高频值及频次
    还会自动计算：缺失率、数值列之间的相关系数矩阵

    支持多数据集模式（dataset_codes），对每个数据集分别生成摘要。

    Args:
        dataset_code:  单数据集编码（与其他来源三选一）
        dataset_codes: 多数据集编码，分别生成各自摘要
        file_id:       用户上传表格文件 ID（推荐）
        file_path:     文件路径或 MinIO object_name
        sheet_name:    指定工作表名（不填则对所有表生成摘要）
        columns:       指定要分析的列名列表（不填则分析所有列）
    """
    dataset_code = _clean_tool_string(dataset_code)
    dataset_codes = _clean_string_list(dataset_codes)
    file_id = _clean_tool_string(file_id)
    file_path = _clean_tool_string(file_path)
    sheet_name = _clean_tool_string(sheet_name)
    columns = _clean_string_list(columns)

    # 多数据集模式：对每个数据集分别生成摘要
    if dataset_codes and len(dataset_codes) >= 1:
        all_lines = [f"## 统计摘要（{len(dataset_codes)} 个数据集）\n"]
        for code in dataset_codes:
            abs_path_i, label_i = await _resolve_file_path(code, None)
            if not abs_path_i:
                all_lines.append(f"\n⚠️ 数据集 `{code}` 不存在，跳过")
                continue
            all_lines.append(f"\n---\n### 数据集：{label_i} (`{code}`)\n")
            schema_i = await asyncio.to_thread(get_schema, abs_path_i)
            if not schema_i:
                all_lines.append("⚠️ 无法获取表结构")
                continue
            tables_i = [sheet_name] if sheet_name and sheet_name in schema_i else list(schema_i.keys())
            for table in tables_i:
                try:
                    all_lines.extend(
                        await asyncio.to_thread(
                            _format_table_statistics,
                            abs_path_i,
                            table,
                            columns,
                            heading="####",
                        )
                    )
                except Exception as e:
                    all_lines.append(f"\n⚠️ 表 `{table}` 分析失败: {e}")
        return "\n".join(all_lines)

    effective_code = dataset_code or (dataset_codes[0] if dataset_codes else None)
    abs_path, label = await _resolve_file_path(effective_code, file_path, file_id)
    if not abs_path:
        return _error(label)

    try:
        schema = await asyncio.to_thread(get_schema, abs_path)
        if not schema:
            return _error("无法获取文件结构")

        # 确定要分析的表
        tables = [sheet_name] if sheet_name and sheet_name in schema else list(schema.keys())
        lines = [f"## 统计摘要：{label}\n"]

        for table in tables:
            try:
                lines.extend(
                    await asyncio.to_thread(
                        _format_table_statistics,
                        abs_path,
                        table,
                        columns,
                        heading="###",
                    )
                )

            except Exception as e:
                lines.append(f"\n⚠️ 表 `{table}` 分析失败: {e}")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("[ExcelTool] summarize_excel_data 失败")
        return _error(f"统计摘要生成时出错: {e}")


async def _build_chart_payload(
    df: Any,
    *,
    chart_type: str,
    x_column: str | None,
    y_columns: list[str] | None,
    group_by: str | None,
    title: str | None,
) -> dict | None:
    """构建 ECharts 交互 payload；失败仅记日志，图表回落为纯 PNG。"""
    try:
        from backend.application.tools.providers.excel_echarts_generator import (
            build_echarts_payload,
        )

        return await asyncio.to_thread(
            build_echarts_payload,
            df,
            chart_type=chart_type,
            x_col=x_column,
            y_cols=y_columns or [],
            group_by=group_by,
            title=title,
        )
    except Exception:
        logger.warning("[ExcelTool] ECharts payload 生成失败，回落纯 PNG", exc_info=True)
        return None


@tool("visualize_excel_data")
async def visualize_excel_data(
    dataset_code: Optional[str] = None,
    dataset_codes: Optional[list[str]] = None,
    file_id: Optional[str] = None,
    file_path: Optional[str] = None,
    chart_type: str = "auto",
    x_column: Optional[str] = None,
    y_columns: Optional[list[str]] = None,
    group_by: Optional[str] = None,
    title: Optional[str] = None,
    sql: Optional[str] = None,
    sheet_name: Optional[str] = None,
    max_rows: int = 500,
    style: Optional[dict] = None,
) -> str:
    """生成实验数据的可视化图表，支持跨数据集 JOIN 结果可视化。

    支持图表类型（chart_type）：
      - auto      根据数据特征自动选择（推荐）
      - scatter   散点图（两连续变量相关性）
      - bar       柱状图（均值±标准差，适合分类比较）
      - barh      水平柱状图（适合反应速率排序等）
      - line      折线图（趋势分析）
      - box       箱线图（分布比较）
      - violin    小提琴图（分布形态）
      - histogram 直方图（频率分布）
      - heatmap   热力图（相关性矩阵 或 双因素交互）
      - bubble    气泡图（三维关系）
      - radar     雷达图（多维对比）

    跨数据集可视化：
      dataset_codes=["biochar_degradation", "hydroxyl_radical_rates"]
      sql='SELECT a."污染物名称", b."OH reaction rate", a."Degradation rate降解速率"
           FROM biochar_degradation__Sheet1 a
           JOIN hydroxyl_radical_rates__Sheet1 b ON a."污染物名称" = b."名字"'
      → 对 JOIN 结果直接生成散点图等

    Args:
        dataset_code:  单数据集编码
        dataset_codes: 多数据集编码（JOIN 模式，必须同时提供 sql 参数）
        file_id:       用户上传表格文件 ID（推荐）
        file_path:     文件路径或 MinIO object_name
        chart_type:    图表类型
        x_column:      X 轴列名
        y_columns:     Y 轴列名列表
        group_by:      分组/着色列名
        title:         图表标题
        sql:           先执行 SQL（支持 JOIN），再可视化结果
        sheet_name:    指定工作表（不填则用第一个表，多文件模式无效）
        max_rows:      最多取多少行参与绘图（默认 500）
        style:         matplotlib rcParams 覆盖
    """
    dataset_code = _clean_tool_string(dataset_code)
    dataset_codes = _clean_string_list(dataset_codes)
    file_id = _clean_tool_string(file_id)
    file_path = _clean_tool_string(file_path)
    chart_type = _clean_tool_string(chart_type) or "auto"
    x_column = _clean_tool_string(x_column)
    y_columns = _clean_string_list(y_columns)
    group_by = _clean_tool_string(group_by)
    title = _clean_tool_string(title)
    sql = _clean_tool_string(sql)
    sheet_name = _clean_tool_string(sheet_name)

    # ── 多文件 JOIN 模式（必须有 sql）────────────────────────────────
    if dataset_codes and len(dataset_codes) > 1:
        if not sql:
            return _error("多数据集可视化必须提供 sql 参数（JOIN 查询），用于指定要绘制的数据")
        file_paths, prefixes, err = await _resolve_multi_file_paths(dataset_codes)
        if err:
            return _error(err)
        try:
            from backend.application.tools.providers.excel_chart_generator import (
                generate_chart,
                save_chart,
            )
            df = await asyncio.to_thread(
                execute_multi_sql,
                file_paths,
                prefixes,
                sql + f" LIMIT {max_rows}",
            )
            if df.empty:
                return _error("JOIN 查询结果为空，无法生成图表")
            label = " ⋈ ".join(dataset_codes)
            title_str = title or f"{label} — {chart_type} 图"
            num_cols = df.select_dtypes("number").columns.tolist()
            cat_cols = df.select_dtypes(exclude="number").columns.tolist()
            if not x_column and cat_cols:
                x_column = cat_cols[0]
            if not y_columns and num_cols:
                y_columns = num_cols[:2]
            png_bytes = await asyncio.to_thread(
                generate_chart,
                df,
                chart_type=chart_type,
                x_col=x_column,
                y_cols=y_columns or [],
                group_by=group_by,
                title=title_str,
                style=style,
            )
            option_payload = await _build_chart_payload(
                df,
                chart_type=chart_type,
                x_column=x_column,
                y_columns=y_columns,
                group_by=group_by,
                title=title_str,
            )
            chart_url = await save_chart(
                png_bytes,
                user_id=_current_access_context().user_id or "public",
                chart_id=uuid.uuid4().hex[:16],
                option_json=option_payload,
            )
            return (f"**跨数据集图表已生成** ✅\n\n"
                    f"- 数据集：{label}\n- 图表类型：{chart_type}\n"
                    f"- 数据行数：{len(df)}\n\n![{title_str}]({chart_url})\n")
        except Exception as e:
            logger.exception("[ExcelTool] visualize_excel_data 多文件失败")
            return _error(f"多文件图表生成时出错: {e}")

    # ── 单文件模式 ────────────────────────────────────────────────────
    effective_code = dataset_code or (dataset_codes[0] if dataset_codes else None)
    abs_path, label = await _resolve_file_path(effective_code, file_path, file_id)
    if not abs_path:
        return _error(label)

    try:
        from backend.application.tools.providers.excel_chart_generator import (
            generate_chart,
            save_chart,
        )

        schema = await asyncio.to_thread(get_schema, abs_path)
        if not schema:
            return _error("无法获取文件结构")

        # 获取数据
        if sql:
            df = await asyncio.to_thread(execute_sql, abs_path, sql)
        else:
            table = sheet_name if sheet_name and sheet_name in schema else next(iter(schema))
            df = await asyncio.to_thread(
                execute_sql,
                abs_path,
                f'SELECT * FROM "{table}" LIMIT {max_rows}',
            )

        if df.empty:
            return _error("查询到的数据为空，无法生成图表")

        # 自动补全缺失的 x/y 配置
        num_cols = df.select_dtypes("number").columns.tolist()
        cat_cols = df.select_dtypes(exclude="number").columns.tolist()

        if not x_column and cat_cols:
            x_column = cat_cols[0]
        if not y_columns and num_cols:
            y_columns = num_cols[:2]

        # 生成图表
        title_str = title or f"{label} — {chart_type} 图"
        png_bytes = await asyncio.to_thread(
            generate_chart,
            df,
            chart_type=chart_type,
            x_col=x_column,
            y_cols=y_columns or [],
            group_by=group_by,
            title=title_str,
            style=style,
        )
        option_payload = await _build_chart_payload(
            df,
            chart_type=chart_type,
            x_column=x_column,
            y_columns=y_columns,
            group_by=group_by,
            title=title_str,
        )

        # 保存并返回 API 代理 URL（不暴露 MinIO 直链）
        chart_url = await save_chart(
            png_bytes,
            user_id=_current_access_context().user_id or "public",
            chart_id=uuid.uuid4().hex[:16],
            option_json=option_payload,
        )
        logger.info("[ExcelTool] 图表已生成: url=%s", chart_url)

        output = "**图表已生成** ✅\n\n"
        output += f"- 数据集：{label}\n"
        output += f"- 图表类型：{chart_type}\n"
        output += f"- 数据行数：{len(df)}\n"
        output += f"- 图表标题：{title_str}\n\n"
        output += f"![{title_str}]({chart_url})\n"

        return output

    except Exception as e:
        logger.exception("[ExcelTool] visualize_excel_data 失败")
        return _error(f"生成图表时出错: {e}")
