---
name: excel-experiment-analysis
description: 专用于实验数据分析的技能。当用户需要分析 Excel/CSV 格式的实验数据时使用，包括：查看服务器上预构建的实验数据集（生物炭降解性能、羟基自由基反应速率等）、探查数据结构、SQL 查询、统计分析、科研可视化图表生成。支持用户上传自定义数据文件。
tools:
  - list_experiment_datasets
  - inspect_excel_data
  - query_excel_data
  - summarize_excel_data
  - visualize_excel_data
---

# 实验数据智能分析技能

## 概述

本技能为你提供完整的实验数据分析工具链，面向环境科学、化学、材料等实验领域。
可以访问服务器上预置的实验数据集，也支持分析用户上传的 Excel/CSV 文件。

## 可用工具

### `list_experiment_datasets`
列出服务器上预注册的实验数据集。**每次分析前先调用此工具**，了解有哪些数据集可用。

### `inspect_excel_data`
探查数据文件结构：工作表名称、列定义、数据类型、行数、样本值。
**执行 SQL 查询前必须先调用，以获取正确的表名和列名。**

### `query_excel_data`
使用 SQL 查询数据（DuckDB 引擎，支持完整 SQL 语法）。
表名 = 工作表名，含特殊字符的列名需用双引号括起。

### `summarize_excel_data`
生成描述统计摘要：均值、标准差、分位数、缺失率、相关系数矩阵。

### `visualize_excel_data`
生成科研级可视化图表（scatter/bar/barh/line/box/violin/histogram/heatmap/bubble/radar）。
图表上传至服务器，返回可直接预览的图片链接。

---

## 标准分析工作流

```
1. list_experiment_datasets()          → 查看可用数据集
2. inspect_excel_data(dataset_code=…)  → 了解表结构和列名
3. summarize_excel_data(…)             → 获取整体统计摘要
4. query_excel_data(sql=…)             → 针对性数据查询
5. visualize_excel_data(…)             → 生成图表
6. 综合分析 → 输出实验结论和建议
```

---

## 常用 SQL 分析模式

### 生物炭降解数据分析

```sql
-- 按热解温度分组统计降解效率（均值 ± 标准差）
SELECT "热解温度", AVG("降解率") AS avg_rate,
       STDDEV("降解率") AS std_rate, COUNT(*) AS n
FROM Sheet1
GROUP BY "热解温度" ORDER BY "热解温度";

-- 筛选高效降解条件（降解率 > 90%）
SELECT * FROM Sheet1
WHERE "降解率" > 90
ORDER BY "降解率" DESC;

-- 两因素交叉分析（pH × 热解温度）
SELECT "pH", "热解温度", AVG("降解率") AS avg_rate
FROM Sheet1
GROUP BY "pH", "热解温度"
ORDER BY "pH", "热解温度";

-- Pearson 相关性（DuckDB 内置 corr 函数）
SELECT corr("热解温度", "降解率") AS r_temp_degradation,
       corr("催化剂浓度", "降解率") AS r_conc_degradation
FROM Sheet1;
```

### 羟基自由基反应速率分析

```sql
-- 按污染物类别统计反应速率
SELECT "污染物类别",
       AVG("反应速率常数") AS avg_k,
       MIN("反应速率常数") AS min_k,
       MAX("反应速率常数") AS max_k,
       COUNT(*) AS n
FROM Sheet1
GROUP BY "污染物类别"
ORDER BY avg_k DESC;

-- 排名前 20 的高活性污染物
SELECT "污染物名称", "反应速率常数", "污染物类别"
FROM Sheet1
ORDER BY "反应速率常数" DESC LIMIT 20;
```

---

## 图表选择指南

| 分析目标 | 推荐图表 | chart_type |
|---------|---------|-----------|
| 两变量相关性（如温度 vs 降解率） | 散点图 + 回归线 | `scatter` |
| 不同类别均值对比（带误差棒） | 柱状图 | `bar` |
| 反应速率数值排序（较多类别） | 水平柱状图 | `barh` |
| 多组数据分布比较 | 箱线图 | `box` |
| 分布形态（偏态/双峰） | 小提琴图 | `violin` |
| 多因素交互（两因素 × 响应值） | 热力图 | `heatmap` |
| 数值列相关性矩阵 | 相关热力图 | `heatmap`（不指定 x/y/group） |
| 单变量频率分布 | 直方图 | `histogram` |
| 多维综合评价 | 雷达图 | `radar` |
| 时间序列 / 条件梯度 | 折线图 | `line` |

---

---

## 跨数据集 JOIN 分析

将多个数据集加载到同一 DuckDB 连接，实现跨数据集联表查询。

### 表命名规则

多文件模式下表名格式：`{dataset_code}__{sheet_name}`

```
biochar_degradation__Sheet1       ← 生物炭数据集的 Sheet1
hydroxyl_radical_rates__Sheet1    ← 羟基自由基数据集的 Sheet1
```

> 无冲突时也可用短名（如 Sheet1），但推荐使用完整前缀名避免歧义。

### 三步完成 JOIN 分析

**Step 1：探查多数据集结构**

```python
inspect_excel_data(
    dataset_codes=["biochar_degradation", "hydroxyl_radical_rates"]
)
# → 返回两个数据集的完整表结构 + 自动生成 JOIN 示例
```

**Step 2：JOIN 查询**

```sql
-- 通过污染物名称关联两个数据集，分析·OH反应速率与降解效率的关系
SELECT
    a."污染物名称",
    b."OH reaction rate"          AS oh_rate,
    b."Log k"                     AS log_k,
    AVG(a."Degradation rate降解速率") AS avg_degradation
FROM biochar_degradation__Sheet1 a
JOIN hydroxyl_radical_rates__Sheet1 b
  ON a."污染物名称" = b."名字"
GROUP BY a."污染物名称", b."OH reaction rate", b."Log k"
ORDER BY oh_rate DESC
LIMIT 30
```

```python
query_excel_data(
    dataset_codes=["biochar_degradation", "hydroxyl_radical_rates"],
    sql="上面的 SQL"
)
```

**Step 3：JOIN 结果可视化**

```python
visualize_excel_data(
    dataset_codes=["biochar_degradation", "hydroxyl_radical_rates"],
    chart_type="scatter",
    x_column="log_k",
    y_columns=["avg_degradation"],
    title="·OH反应速率(Log k) 与降解率的相关性",
    sql="上面的聚合 SQL"
)
# → 直接对 JOIN 结果生成散点图
```

### 更多 JOIN 场景

```sql
-- 统计两个数据集中出现的共同污染物
SELECT a."污染物名称", COUNT(*) AS biochar_records,
       b."OH reaction rate", b."分类"
FROM biochar_degradation__Sheet1 a
INNER JOIN hydroxyl_radical_rates__Sheet1 b
  ON LOWER(a."污染物名称") = LOWER(b."名字")
GROUP BY a."污染物名称", b."OH reaction rate", b."分类"
ORDER BY biochar_records DESC

-- LEFT JOIN 保留所有生物炭降解记录，不存在速率数据时显示 NULL
SELECT a."污染物名称", a."Degradation rate降解速率",
       b."OH reaction rate"
FROM biochar_degradation__Sheet1 a
LEFT JOIN hydroxyl_radical_rates__Sheet1 b
  ON a."污染物名称" = b."名字"
WHERE a."Degradation rate降解速率" IS NOT NULL
```

---

## 注意事项

1. **列名含中文/特殊字符**：SQL 中需用双引号括起，如 `SELECT "热解温度" FROM Sheet1`
2. **不确定表名/列名**：先调用 `inspect_excel_data` 确认；多数据集先调用 `inspect_excel_data(dataset_codes=[...])`
3. **大数据量**：`query_excel_data` 默认限制 5000 行，`visualize_excel_data` 限制 500 行；可通过 `sql` 先聚合再可视化
4. **JOIN 前必须确认关联字段**：两个数据集中用于 JOIN 的列名/格式需一致，不一致时用 `LOWER()`/`TRIM()` 等函数处理
5. **多文件 JOIN 时 `visualize_excel_data` 必须提供 `sql` 参数**
6. **多次调用**：复杂分析可多次调用不同工具，逐步深入
