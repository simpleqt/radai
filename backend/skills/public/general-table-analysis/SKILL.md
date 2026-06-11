---
name: general-table-analysis
description: 通用表格分析能力。用于分析用户上传或系统已有的 Excel/CSV 表格，包括表结构探查、SQL 查询、统计摘要、缺失值分析、分组聚合、排序筛选和可视化图表生成。不限定专业领域。
tools:
  - inspect_excel_data
  - query_excel_data
  - summarize_excel_data
  - visualize_excel_data
---

# 通用表格分析能力

## 概述

本技能用于处理通用 Excel/CSV 表格数据，不绑定任何专业方向。只要用户上传了表格文件，或明确要求分析表格、统计数据、筛选排序、分组汇总、生成图表，就应使用本技能。

优先使用用户上传的文件。运行时通常会提供 `file_id`，请优先把 `file_id` 传给表格分析工具；不要编造本地文件路径。

## 可用工具

### `inspect_excel_data`
探查表格结构：工作表名、表名、列名、数据类型、行数、样本值。

在执行 SQL、统计或绘图前，必须先调用本工具确认表名和列名。

### `query_excel_data`
使用 SQL 查询表格数据。适合筛选、排序、Top N、分组聚合、窗口函数和复杂条件查询。

### `summarize_excel_data`
生成描述统计摘要，包括数值列统计、分类列频次、缺失值和相关性矩阵。

### `visualize_excel_data`
生成图表，支持 scatter、bar、barh、line、box、violin、histogram、heatmap、bubble、radar 等类型。

## 标准工作流

```text
1. inspect_excel_data(file_id=...)        -> 确认表名、列名、数据类型
2. summarize_excel_data(file_id=...)      -> 获取整体统计摘要
3. query_excel_data(file_id=..., sql=...) -> 按用户问题做定向查询
4. visualize_excel_data(file_id=..., ...) -> 用户要求画图时生成图表
5. 综合工具结果回答用户
```

## SQL 规则

1. SQL 表名使用 `inspect_excel_data` 返回的表名。
2. CSV 默认表名通常是 `data`。
3. 列名包含中文、空格或特殊字符时，必须用双引号包裹，例如 `"销售额"`。
4. 查询结果过大时先聚合或加 `LIMIT`。
5. 不要使用未确认存在的表名或列名。

## 回答要求

1. 直接围绕用户问题回答，不要泛泛介绍表格工具。
2. 涉及数值时基于工具结果，不要臆造。
3. 如果列名或工作表不存在，说明可用列名，并给出下一步建议。
4. 如果工具生成图表，保留返回的 Markdown 图片链接，方便前端预览。
5. 如果用户只是上传表格并说“分析一下”，先给整体摘要、异常/缺失情况、关键数值列和可进一步分析方向。
