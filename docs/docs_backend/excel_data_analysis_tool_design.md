# Excel 实验数据智能分析工具 — 详细设计与实现文档

> **版本**：v1.1 · **状态**：已全部实现 · **最后更新**：2026-07-09（对齐 `datasets.py`、`dataset_crud.py`、Skill 管理与 Agent 运行时）

---

## 目录

1. [功能概述](#1-功能概述)
2. [整体架构](#2-整体架构)
3. [数据库设计](#3-数据库设计)
4. [核心模块详解](#4-核心模块详解)
   - 4.1 [DuckDB 查询引擎](#41-duckdb-查询引擎-excel_enginepy)
   - 4.2 [可视化图表生成器](#42-可视化图表生成器-excel_chart_generatorpy)
   - 4.3 [图表图片服务](#43-图表图片服务-chart_imagespy)
   - 4.4 [Agent 分析工具集](#44-agent-分析工具集-excel_data_analysis_toolpy)
5. [Skill 定义](#5-skill-定义)
6. [Tool 注入机制](#6-tool-注入机制)
7. [数据集管理 REST API](#7-数据集管理-rest-api)
8. [前端集成](#8-前端集成)
9. [Agent-数据集绑定策略](#9-agent-数据集绑定策略)
10. [文件存储策略](#10-文件存储策略)
11. [数据清洗机制](#11-数据清洗机制)
12. [日志噪声抑制](#12-日志噪声抑制)
13. [已知问题与修复记录](#13-已知问题与修复记录)
14. [文件清单](#14-文件清单)
15. [使用示例](#15-使用示例)
16. [后续规划](#16-后续规划)

---

## 1. 功能概述

本模块为 CSH-AI（成理场地污染水文地质智学助手）智能体提供专用的实验数据分析能力，覆盖以下场景：

| 能力 | 描述 |
|------|------|
| **数据集管理** | 服务器预注册实验数据集，支持用户上传，按 Agent 隔离可见性 |
| **智能查询** | 基于 DuckDB 的完整 SQL 查询，支持 GROUP BY / JOIN / 窗口函数 |
| **跨数据集联表** | 多数据集加载到同一 DuckDB 连接，支持跨文件 JOIN 分析 |
| **统计分析** | 描述统计、缺失率分析、Pearson 相关矩阵 |
| **科研可视化** | matplotlib+seaborn 生成高分辨率科研图表（10 种类型） |
| **图表服务** | 图表通过 API Server 代理访问，无需暴露 MinIO 端口 |
| **数据清洗** | 自动处理 Excel 中的 `\xa0`、混合列等数据质量问题 |

### 预置数据集

| 数据集 | 编码 | 规模 | 绑定智能体 |
|--------|------|------|-----------|
| 生物炭制备条件与反应体系条件对污染物降解性能的影响 | `biochar_degradation` | 365 行 × 47 列 | `csh`（初始化表中仍写 `csh_full_feature`，CRUD 会做别名兼容） |
| 不同污染物与羟基自由基的反应速率 | `hydroxyl_radical_rates` | 823 行 × 9 列 | `csh`（同上） |

---

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     前端 (Vue 3 + TypeScript)                            │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  ChatBox.vue — 技能选择面板 (availableSkills)                      │  │
│  │  深度研究技能 | PPT 创建(HTML) | 学术文献搜索 | 实验数据分析 ←新增  │  │
│  └─────────────────────────┬─────────────────────────────────────────┘  │
│                             │  POST /api/v2/agent-runtime/query         │
│                             │  { selected_skills: ["excel-experiment-   │
│                             │    analysis"], query: "..." }              │
└─────────────────────────────┼───────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────────────┐
│                   API 层 (FastAPI :8082)                                 │
│                                                                         │
│  /api/v2/agent-runtime/query ─→ AgentService + WorkflowRegistry        │
│                                    └─→ ToolAssemblyPipeline             │
│                                          └─→ 注入 5 个分析工具           │
│                                                                         │
│  /api/v1/datasets/*       ───→  数据集管理 REST API (9 个端点)           │
│  /api/v1/charts/{uid}/{cid} →  图表图片代理服务                          │
└──────────┬─────────────────────────────────────────────────────────────┘
           │ Agent 调用工具
┌──────────▼─────────────────────────────────────────────────────────────┐
│                  分析工具层 (backend/application/tools/providers/)          │
│                                                                         │
│  list_experiment_datasets  ──→  MySQL 查询（按 agent_codes 过滤）        │
│  inspect_excel_data        ──→  ExcelQueryEngine.get_schema()           │
│  query_excel_data          ──→  ExcelQueryEngine.execute_sql()          │
│  summarize_excel_data      ──→  pandas describe() + corr()             │
│  visualize_excel_data      ──→  ChartGenerator + ChartImages            │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │           ExcelQueryEngine  (excel_engine.py)                    │  │
│  │  _clean_dataframe()   — 清洗 \xa0、混合列、推断数值类型            │  │
│  │  get_connection()     — 单文件 DuckDB 连接（哈希缓存）              │  │
│  │  get_multi_connection()— 多文件联合连接（跨 JOIN）                  │  │
│  │  execute_sql()        — SQL 查询，限制 5000 行                     │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │         ChartGenerator  (excel_chart_generator.py)               │  │
│  │  generate_chart()  — matplotlib+seaborn 生成 PNG                  │  │
│  │  save_chart()      — 存 MinIO 或本地，返回 /api/v1/charts/... URL │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
           ┌───────────────────┼──────────────────────┐
           ▼                   ▼                      ▼
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐
│  MySQL          │  │  MinIO :9004     │  │  本地目录（降级）          │
│  experiment_    │  │  user-files/     │  │  backend/data/charts/    │
│  datasets       │  │  charts/{uid}/   │  │  {user_id}/{chart_id}.png│
│  (元数据+绑定)  │  │  {chart_id}.png  │  │                          │
└─────────────────┘  └──────────────────┘  └──────────────────────────┘
```

---

## 3. 数据库设计

### 3.1 表定义

```sql
CREATE TABLE experiment_datasets (
    -- 主键
    id            VARCHAR(36)   NOT NULL PRIMARY KEY,
    code          VARCHAR(100)  NOT NULL UNIQUE     COMMENT '数据集编码，Agent 用于引用',
    name          VARCHAR(255)  NOT NULL,
    description   TEXT,

    -- 文件存储
    file_path     VARCHAR(512)  NOT NULL  COMMENT 'MinIO object_name 或本地绝对路径',
    file_type     VARCHAR(10)   DEFAULT 'xlsx',
    file_size     BIGINT,
    storage_type  VARCHAR(20)   DEFAULT 'local'  COMMENT 'local | minio',

    -- 自动解析的 Schema（加载时填充）
    sheet_names   JSON          COMMENT '工作表名称列表 ["Sheet1", ...]',
    column_schema JSON          COMMENT '各表列定义 {sheet: [{name,dtype,non_null,total,sample}]}',
    row_counts    JSON          COMMENT '各表行数 {sheet: count}',

    -- 分类与权限
    domain        VARCHAR(50)   COMMENT 'environmental | chemistry | physics | biology',
    tags          JSON          COMMENT '标签列表 ["生物炭", "AOPs", ...]',
    agent_codes   JSON          COMMENT '绑定 Agent 列表，[] 表示全局可见',
    is_public     TINYINT(1)    DEFAULT 1,
    created_by    VARCHAR(36),
    status        VARCHAR(20)   DEFAULT 'active'  COMMENT 'active | archived',

    -- 时间戳
    created_at    DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_dataset_code   (code),
    INDEX idx_dataset_domain (domain),
    INDEX idx_dataset_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='实验数据集注册表';
```

### 3.2 关键字段说明

**`agent_codes`（JSON 数组）**

```json
[]                                          // 全局可见（所有 Agent 均可使用）
["csh"]                                     // 仅 CSH-AI runtime profile
["csh_full_feature"]                        // 旧编码，DatasetCRUD 会映射到 csh
["csh", "ne"]                               // CSH + NE 两个 runtime profile
```

设计与 `agent_kb_bindings` 表对齐——空数组表示通用，非空表示专属。通过 `PUT /api/v1/datasets/{id}/agents` 随时修改，无需重启服务。

**`storage_type`**

- `local`：`file_path` 是服务器本地绝对路径
- `minio`：`file_path` 是 MinIO object_name（桶：`user-files`）

DuckDB 加载时 `_resolve_local_path()` 会自动处理两种类型：MinIO 路径先下载到 `backend/data/dataset_cache/` 本地缓存，再由 DuckDB 读取。

### 3.3 在线 DDL 迁移

表在 Server 首次启动时由 `init_datasets.py` 的 `init_dataset_table()` 创建。若服务持续运行期间新增列，执行以下迁移（幂等）：

```python
# init_datasets.py 内已包含
await conn.execute(text(
    "ALTER TABLE experiment_datasets ADD COLUMN IF NOT EXISTS "
    "agent_codes JSON COMMENT '绑定的智能体编码列表'"
))
```

如服务未重启，直接对线上数据库执行：
```sql
ALTER TABLE experiment_datasets ADD COLUMN agent_codes JSON;
UPDATE experiment_datasets SET agent_codes = '["csh_full_feature"]'
WHERE code IN ('biochar_degradation', 'hydroxyl_radical_rates');
```

`DatasetCRUD._agent_code_aliases()` 会把 `csh` 与 `csh_full_feature`、`ne` 与 `ne_full_feature` 互相映射；新数据建议直接写运行时编码 `csh` / `ne`，存量预置数据保持兼容。

---

## 4. 核心模块详解

### 4.1 DuckDB 查询引擎（`excel_engine.py`）

**文件**：`backend/application/tools/providers/excel_engine.py`（629 行）

#### 4.1.1 数据加载与清洗流程

```
pd.read_excel(file_path, sheet_name=None)
    └─→ _clean_dataframe(df)          # 数据清洗（见 4.1.2）
         └─→ conn.register(table, df) # 注册为 DuckDB 视图
```

所有 Sheet 加载后统一注册为 DuckDB 视图，表名 = 工作表名（sanitize 处理特殊字符）。

#### 4.1.2 数据清洗（`_clean_dataframe`）

解决 Excel 中常见的数据质量问题：

| 问题 | 示例 | 处理方式 |
|------|------|---------|
| `\xa0` 不间断空格 | `'0.0658\xa0'` → 无法转 DOUBLE | `str.translate()` 替换为普通空格后 strip |
| `\u3000` 全角空格 | 日文/中文文档常见 | 同上替换 |
| 零宽字符 | `\u200b`、`\u200c`、`\ufeff` | 删除 |
| 空字符串 | `''` | 替换为 `pd.NA` |
| 混合列（标头混数据） | `kobs(min-1)`, `0.0024`, `0.0034` | **≥50% 非空值可转数值**时整列推断为 `float64`，不可转的变 `NaN` |

> **重要**：数据集1 的 `Degradation rate降解速率` 列是典型混合列——第一个非空值是 `kobs(min-1)` 标头，后续才是数值。旧实现要求"全部可转"导致列保持 VARCHAR，`AVG()` 报错。新实现以 50% 阈值宽松处理，该列自动推断为 `DOUBLE`。

#### 4.1.3 连接缓存

```python
_cache: dict[str, duckdb.DuckDBPyConnection] = {}     # 单文件连接
_multi_table_maps: dict[str, dict[str, list[str]]] = {}  # 多文件 table_map
_cache_lock = threading.Lock()
```

缓存键 = `md5(file_path + mtime + size)`，文件变动后 `invalidate_cache()` 清除。

#### 4.1.4 多文件联合连接（JOIN 模式）

```python
conn, table_map = get_multi_connection(
    file_paths=["datasets/biochar.xlsx", "datasets/hydroxyl.xlsx"],
    prefixes=["biochar_degradation", "hydroxyl_radical_rates"],
)
# table_map = {
#   "biochar_degradation": ["biochar_degradation__Sheet1"],
#   "hydroxyl_radical_rates": ["hydroxyl_radical_rates__Sheet1"],
# }
```

**表命名规则**：
- 每个 Sheet 必定注册带前缀名：`{dataset_code}__{sheet_name}`
- 若该 Sheet 名在所有文件中唯一，额外注册无前缀短名（便于 SQL）
- 两个文件都有 `Sheet1` → 只有 `biochar_degradation__Sheet1` 和 `hydroxyl_radical_rates__Sheet1`，无短名

#### 4.1.5 主要函数签名

```python
def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame
    """清洗非标准空白 + 宽松数值推断"""

def get_connection(file_path: str) -> duckdb.DuckDBPyConnection
    """单文件连接（支持 MinIO 路径自动下载）"""

def get_multi_connection(file_paths, prefixes) -> (conn, table_map)
    """多文件联合连接"""

def execute_sql(file_path, sql, max_rows=5000) -> pd.DataFrame
    """单文件 SQL 查询"""

def execute_multi_sql(file_paths, prefixes, sql, max_rows=5000) -> pd.DataFrame
    """多文件联合 SQL 查询"""

def get_schema(file_path) -> dict[str, list[dict]]
    """获取表结构 {table_name: [{name,dtype,non_null,total,sample}]}"""

def get_multi_schema(file_paths, prefixes) -> dict[str, dict[str, list[dict]]]
    """获取多文件联合 schema"""

def schema_to_text(schema) -> str
    """单文件 schema 格式化为 Markdown 表格"""

def multi_schema_to_text(multi_schema) -> str
    """多文件 schema 格式化 + 自动生成 JOIN 示例"""

def df_to_markdown(df, max_rows=50) -> str
    """DataFrame 转 Markdown 表格（含行数提示）"""

def invalidate_cache(file_path) -> None
    """清除指定文件的 DuckDB 连接缓存"""

def _resolve_local_path(file_path) -> str
    """MinIO object_name → 本地缓存路径（自动下载）"""
```

---

### 4.2 可视化图表生成器（`excel_chart_generator.py`）

**文件**：`backend/application/tools/providers/excel_chart_generator.py`（525 行）

#### 4.2.1 支持的图表类型

| `chart_type` | 图表类型 | 适用场景 |
|-------------|---------|---------|
| `scatter` | 散点图 + 回归线 | 两连续变量相关性，标注 r 值 |
| `bar` | 柱状图 + 误差棒 | 分类均值对比（均值 ± 标准差） |
| `barh` | 水平柱状图 | 数值排名对比（如反应速率 TOP N） |
| `line` | 折线图 | 趋势、多系列比较 |
| `box` | 箱线图 + 数据点叠加 | 多组分布比较，异常值可见 |
| `violin` | 小提琴图 | 分布形态（偏态/双峰检验） |
| `histogram` | 直方图 + KDE | 单变量频率分布 |
| `heatmap` | 热力图 | 相关性矩阵 OR 双因素交互响应面 |
| `bubble` | 气泡图 | 三维关系（大小编码第三维） |
| `radar` | 雷达图 | 多维指标综合对比 |
| `auto` | 自动推断 | 根据列类型自动选择 |

**自动推断逻辑** (`auto_detect_chart_type`)：
- 两列均为连续数值 → `scatter`
- 一列分类 + 一列连续，分类 ≤30 类 → `bar`
- 单列连续数值 → `histogram`
- 其他情况 → `bar`

#### 4.2.2 科研图表样式

```python
_SCIENCE_RC = {
    "font.family": "WenQuanYi Micro Hei",  # 中文字体（自动降级 DejaVu Sans）
    "font.size": 11,
    "axes.titlesize": 13,
    "figure.dpi": 150,
    "axes.linewidth": 1.2,
    "axes.spines.top": False,   # 去除顶部/右侧边框
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.35,
    "figure.facecolor": "white",
    "axes.facecolor": "#f8f9fa",
    "savefig.bbox": "tight",
}
```

#### 4.2.3 图表存储与 URL 生成

```python
async def save_chart(png_bytes, *, user_id=None, chart_id=None) -> str:
    """
    存储策略：
    1. 上传到 MinIO: charts/{user_id}/{chart_id}.png（5s 超时）
    2. MinIO 失败降级本地: backend/data/charts/{user_id}/{chart_id}.png
    
    返回带时效签名的 URL: /api/v1/charts/{user_id}/{chart_id}?exp=...&sig=...
    （由 FastAPI 代理，不暴露 MinIO 地址和端口）
    """
```

> **为何不用 MinIO presigned URL**：MinIO 端口（9004）在生产环境中仅内网可达，浏览器 `<img>` 标签直接请求 `202.115.138.101:9004` 会失败。改为通过 API Server（8082）代理后，URL 始终可访问。

---

### 4.3 图表图片服务（`chart_images.py`）

**文件**：`backend/api/routers/chart_images.py`（142 行）

#### 4.3.1 端点说明

```
GET /api/v1/charts/{user_id}/{chart_id}
```

- **访问控制**：无需 Bearer Token，但必须携带未过期的 `exp` 与 HMAC `sig`；签名密钥取 `INTERNAL_API_SECRET`，缺省回退 `MINIO_SECRET_KEY`
- **缓存**：`Cache-Control: public, max-age=86400`（1 天）
- **响应**：`image/png` 字节流，支持浏览器 `<img>` 标签直接嵌入

#### 4.3.2 取图逻辑

```
请求 GET /api/v1/charts/test_user/abc123?exp=<unix_ts>&sig=<hmac>
    │
    ├─→ MinIO: client.get_object("user-files", "charts/test_user/abc123.png")
    │   ✅ 返回 image/png
    │
    └─→ MinIO 失败（不可达/不存在）
        └─→ 本地: backend/data/charts/test_user/abc123.png
            ✅ 返回 image/png
            ❌ 404
```

#### 4.3.3 安全防护

```python
def _safe_id(s: str) -> bool:
    """只允许 [A-Za-z0-9_-]，最长 128 字符，防路径穿越"""
    return bool(re.match(r"^[\w\-]{1,128}$", s))
```

---

### 4.4 Agent 分析工具集（`excel_data_analysis_tool.py`）

**文件**：`backend/application/tools/providers/excel_data_analysis_tool.py`（708 行）

#### 4.4.1 五个 LangChain 工具

---

**工具 1：`list_experiment_datasets`**

```python
@tool("list_experiment_datasets")
async def list_experiment_datasets(
    domain: Optional[str] = None,     # 领域过滤
    tags: Optional[list[str]] = None,  # 标签过滤（任一匹配）
    agent_code: Optional[str] = None,  # Agent 编码过滤（重要）
) -> str
```

调用链：`DatasetCRUD.list_all(agent_code=...)` → MySQL → Python 层按 `agent_codes` 过滤 → JSON 返回

**过滤规则**：
- `agent_codes = []`（空数组）→ 全局可见，**任何 agent_code 都能看到**
- `agent_codes = ["csh"]` → 只有 `agent_code="csh"` 时才返回
- `agent_codes = ["csh_full_feature"]` → `agent_code="csh"` 或旧编码 `csh_full_feature` 都能返回（`DatasetCRUD._agent_code_aliases()` 兼容）

---

**工具 2：`inspect_excel_data`**

```python
@tool("inspect_excel_data")
async def inspect_excel_data(
    dataset_code: Optional[str] = None,
    dataset_codes: Optional[list[str]] = None,  # 多文件模式
    file_path: Optional[str] = None,
) -> str
```

- 单文件：返回工作表列表、列名/类型/行数/样本值 + 示例 SQL
- 多文件：返回每个数据集的表结构 + 自动生成 JOIN 示例 SQL

**输出示例（单文件）**：
```
## 数据集结构：不同污染物与羟基自由基的反应速率

**工作表数量**: 1

**表名**: `Sheet1`（共 823 行）
| 列名 | 数据类型 | 非空数 | 样本值 |
|------|---------|-------|--------|
| name     | VARCHAR | 823/823 | methylcyclopentane, benzidine |
| OH reaction rate | DOUBLE | 823/823 | 7e9, 2.5e9, 1.5e10 |
| Log k    | DOUBLE  | 823/823 | 9.845, 9.397, 10.176 |
...

---
**查询提示**:
- SQL 表名: `Sheet1`
- 示例: SELECT "name", "OH reaction rate" FROM "Sheet1" LIMIT 5
- 如需跨数据集 JOIN，改用 dataset_codes=[...] 参数
```

---

**工具 3：`query_excel_data`**

```python
@tool("query_excel_data")
async def query_excel_data(
    sql: str,
    dataset_code: Optional[str] = None,
    dataset_codes: Optional[list[str]] = None,  # JOIN 模式
    file_path: Optional[str] = None,
    output_format: str = "markdown",  # markdown / json / csv
) -> str
```

- **单数据集**：`execute_sql(file_path, sql)` → DataFrame → Markdown 表格
- **多数据集 JOIN**：`execute_multi_sql(file_paths, prefixes, sql)` → 同上

多文件 JOIN 时表名格式：`{dataset_code}__{sheet_name}`

```sql
-- 示例：关联两个数据集
SELECT
    a."污染物名称",
    b."OH reaction rate" AS oh_rate,
    AVG(a."Degradation rate降解速率") AS avg_degradation
FROM biochar_degradation__Sheet1 a
JOIN hydroxyl_radical_rates__Sheet1 b
  ON LOWER(TRIM(a."污染物名称")) = LOWER(TRIM(b."名字"))
GROUP BY a."污染物名称", b."OH reaction rate"
ORDER BY oh_rate DESC
LIMIT 20
```

---

**工具 4：`summarize_excel_data`**

```python
@tool("summarize_excel_data")
async def summarize_excel_data(
    dataset_code: Optional[str] = None,
    dataset_codes: Optional[list[str]] = None,  # 多数据集分别摘要
    file_path: Optional[str] = None,
    sheet_name: Optional[str] = None,
    columns: Optional[list[str]] = None,
) -> str
```

输出内容：
1. `df.describe(include="all")` 描述统计（数值列：count/mean/std/min/25%/50%/75%/max；分类列：count/unique/top/freq）
2. 缺失值统计（仅列出有缺失的列及缺失率）
3. 数值列 Pearson 相关系数矩阵（列数 ≥2 时输出）

多数据集模式（`dataset_codes`）：对每个数据集分别生成摘要后合并输出。

---

**工具 5：`visualize_excel_data`**

```python
@tool("visualize_excel_data")
async def visualize_excel_data(
    dataset_code: Optional[str] = None,
    dataset_codes: Optional[list[str]] = None,  # JOIN 后可视化
    file_path: Optional[str] = None,
    chart_type: str = "auto",
    x_column: Optional[str] = None,
    y_columns: Optional[list[str]] = None,
    group_by: Optional[str] = None,
    title: Optional[str] = None,
    sql: Optional[str] = None,     # 先查询后可视化
    sheet_name: Optional[str] = None,
    max_rows: int = 500,
    style: Optional[dict] = None,  # matplotlib rcParams 覆盖
) -> str
```

执行流程：
```
1. 解析数据源（dataset_code / dataset_codes / file_path）
2. 执行 SQL 或加载全表（max_rows 限制）
3. 自动补全 x/y 列（从 cat/num 列中选取）
4. generate_chart() → PNG bytes（matplotlib）
5. save_chart(png_bytes, chart_id=uuid) → /api/v1/charts/{uid}/{cid}
6. 返回 Markdown: ![title](url)
```

多文件模式下**必须提供 `sql` 参数**（JOIN SQL 明确指定绘图数据）。

---

#### 4.4.2 数据源解析辅助函数

```python
async def _resolve_file_path(dataset_code, file_path) -> (abs_path, label)
    """单数据集解析：dataset_code → MySQL → file_path；或直接用 file_path"""

async def _resolve_multi_file_paths(dataset_codes) -> (file_paths, prefixes, error_msg)
    """多数据集批量解析，返回文件路径列表和对应前缀列表"""
```

---

## 5. Skill 定义

**文件**：`backend/skills/public/excel-experiment-analysis/SKILL.md`

### YAML Front Matter

```yaml
---
name: excel-experiment-analysis
description: >
  专用于实验数据分析的技能。支持查看服务器预置数据集（生物炭降解、
  羟基自由基反应速率等）、SQL 查询、统计分析、跨数据集 JOIN、科研可视化。
tools:
  - list_experiment_datasets
  - inspect_excel_data
  - query_excel_data
  - summarize_excel_data
  - visualize_excel_data
---
```

### Skill 解析机制

由 `backend/core/skills/parser.py` 解析 YAML front matter 中的 `tools` 字段：

```python
# parser.py 解析后：
Skill(
    name="excel-experiment-analysis",
    tool_names=["list_experiment_datasets", "inspect_excel_data",
                "query_excel_data", "summarize_excel_data", "visualize_excel_data"],
    enabled=True,
    category="public",
)
```

---

## 6. Tool 注入机制

**文件**：`backend/application/tools/tool_assembly_pipeline.py`、`backend/application/skills/runtime_resolver.py`

### 注入流程

```
用户请求携带 selected_skills=["excel-experiment-analysis"]
    │
    ▼ SkillRuntimeResolver.resolve()
        │   └─ 校验 Agent-Skill 绑定、Skill ACL、domain、allowed_workflows
        ▼ ToolAssemblyPipeline.assemble()
        │
        ├─→ PolicyToolAssembler.resolve_specs() # 策略允许的基础工具
        ├─→ multi_kb_search                    # KB 检索工具
        ├─→ ScopedSkillsProvider.get_available_skills_for_user(
        │       selected_skills={"excel-experiment-analysis"}
        │   )
        │   → [Skill(name="excel-experiment-analysis", tool_names=[...])]
        │
        └─→ _validate_skill_tool_declarations(skills)
                └─→ 校验 Skill 声明工具已由策略装配或动态 provider 提供
```

### Tool provider 关键逻辑

```python
class ToolProviderLoader:
    def load(self, spec: ToolSpec):
        module_name, _, attr_name = spec.provider.partition(":")
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)

# registry.yaml
list_experiment_datasets:
  provider: backend.application.tools.providers.excel_data_analysis_tool:get_excel_analysis_tools
```

> **注意**：`get_excel_analysis_tools()` 可以返回工具列表；`PolicyToolAssembler.materialize_spec()` 只保留与当前 `ToolSpec.name` 匹配的工具，再由策略装配进入本次会话。

---

## 7. 数据集管理 REST API

**文件**：`backend/api/routers/datasets.py`

**路由前缀**：`/api/v1`，标签：`实验数据集`

### 7.1 端点一览

| 方法 | 路径 | 功能 | 关键逻辑 |
|------|------|------|---------|
| `GET` | `/datasets` | 列出数据集 | 支持 `domain`、`agent_code` 过滤；admin 可见全部，普通用户只见 `is_public=true` |
| `GET` | `/datasets/by-agent/{agent_code}` | 指定 Agent 可见的数据集 | `DatasetCRUD.list_all(agent_code=agent_code)`，兼容运行时/旧编码 |
| `GET` | `/datasets/{id}` | 数据集详情 | 含完整 `column_schema` |
| `POST` | `/datasets` | 注册已有文件 | 适用于管理员注册本地文件，自动解析 schema |
| `POST` | `/datasets/upload` | 上传并注册 | 优先 MinIO，失败降级本地；`agent_codes` 从 Form 参数传入 |
| `GET` | `/datasets/{id}/preview` | 数据预览 | 支持 MinIO 路径（先下载本地缓存再读取） |
| `POST` | `/datasets/{id}/refresh` | 刷新 schema | 清 DuckDB 缓存 + 重新解析 schema |
| `PUT` | `/datasets/{id}/agents` | 设置绑定 Agent | 覆盖 `agent_codes`，空列表=全局可见 |
| `DELETE` | `/datasets/{id}` | 删除数据集 | `?delete_file=true` 同时删 MinIO 对象或本地文件 |

### 7.2 上传端点详解

```
POST /api/v1/datasets/upload
Content-Type: multipart/form-data

file:         xlsx/csv 文件
code:         数据集编码（唯一，英文）
name:         数据集名称
description:  描述（可选）
domain:       领域（可选，如 environmental）
tags:         JSON 数组字符串（如 '["生物炭","AOPs"]'）
agent_codes:  JSON 数组字符串（如 '["csh"]'；旧值 '["csh_full_feature"]' 仍可被 csh 查询到）
is_public:    true/false
```

处理流程：
```
1. 文件类型校验（.xlsx/.xls/.csv）
2. 大小限制（100MB）
3. 上传 MinIO（5s 超时）
   ├─ 成功 → storage_type="minio", file_path=MinIO object_name
   └─ 失败 → 保存本地 datasets/，storage_type="local"
4. MySQL 创建记录
5. 解析 schema（本地直读；MinIO 用临时文件）
6. 更新 schema 到 MySQL
```

### 7.3 Agent 绑定示例

```bash
# 将数据集绑定到 CSH 智能体
curl -X PUT http://localhost:8082/api/v1/datasets/{id}/agents \
  -H "X-API-Key: ..." \
  -H "Content-Type: application/json" \
  -d '{"agent_codes": ["csh"]}'

# 设为全局可见（两个 Agent 均可用）
curl -X PUT http://localhost:8082/api/v1/datasets/{id}/agents \
  -H "X-API-Key: ..." \
  -d '{"agent_codes": []}'

# 查看 CSH 智能体可用的全部数据集
curl http://localhost:8082/api/v1/datasets/by-agent/csh \
  -H "X-API-Key: ..."
```

---

## 8. 前端集成

### 8.1 Skill 选择面板

两个前端（`frontend/` 和 `frontend_csh_ai/`）的 `ChatBox.vue` 均已加入新 Skill：

```typescript
// frontend_csh_ai/src/views/ne-full/components/ChatBox.vue
// frontend/src/views/ne-full/components/ChatBox.vue

const availableSkills = ref([
  { id: 'deep-research',             name: '深度研究技能' },
  { id: 'html-ppt',                  name: 'PPT 创建（HTML）' },
  { id: 'academic-search',           name: '学术文献搜索' },
  { id: 'excel-experiment-analysis', name: '实验数据分析' },  // ← 新增
])
```

Skill 列表当前应从 `/api/v2/agent-runtime/profiles/{agent_code}/info` 的 `available_skills` 初始化，并可通过 `/api/v2/agent-runtime/skill-manifests?agent_code=...` 获取 manifest；不要再接入旧 CSH 专属 Skill 接口。

### 8.2 请求体传递

```typescript
// 用户勾选后，发送时携带
payload.selected_skills = ["excel-experiment-analysis"]

// 后端 QueryRequest 对应字段
selected_skills: list[str] | None
```

### 8.3 图表显示

Agent 返回的图表通过 Markdown 内联图片格式嵌入：

```markdown
![热解温度与污染物降解效率的相关性](/api/v1/charts/user_123/a1b2c3d4e5f67890?exp=<unix_ts>&sig=<hmac>)
```

前端消息渲染器遇到图片 Markdown 时，`<img>` 标签的 `src` 指向 `{API_BASE}/api/v1/charts/...`，由 FastAPI 代理从 MinIO 取回字节流。

> 当前工具通过请求级访问上下文读取 `user_id`；只有兼容旧调用或缺少访问上下文时才回退到 `public` 命名空间。`store_chart()` 总是返回带过期时间和 HMAC 的签名 URL。

---

## 9. Agent-数据集绑定策略

### 9.1 设计原则

与知识库绑定（`agent_kb_bindings`）保持一致的设计范式：

| 特性 | `agent_kb_bindings` | `experiment_datasets.agent_codes` |
|------|---------------------|----------------------------------|
| 存储位置 | 独立关联表 | JSON 字段内嵌 |
| 全局可见 | 无记录 = 全局 | `[]` 空数组 = 全局 |
| 专属绑定 | 有记录 = 专属 | 非空数组 = 专属 |
| 修改方式 | `PUT /api/v1/agents/{code}/kbs` | `PUT /api/v1/datasets/{id}/agents` |
| 运行时查询 | `get_runtime_agent_kb_ids()` 解析知识库绑定 | `DatasetCRUD.list_all(agent_code=...)` 按智能体过滤数据集 |

两个预置数据集在初始化脚本中仍以旧编码绑定到 `csh_full_feature`，运行时通过别名对 `csh` 可见：

```
biochar_degradation      → agent_codes: ["csh_full_feature"]
hydroxyl_radical_rates   → agent_codes: ["csh_full_feature"]
```

### 9.2 工具层过滤

```python
# list_experiment_datasets 工具调用示例
await list_experiment_datasets(agent_code="csh")
# → 返回 agent_codes 包含 "csh"、"csh_full_feature" 或为空数组的数据集

await list_experiment_datasets(agent_code="ne")
# → 返回 agent_codes 为空数组的数据集（目前为 0 条，因两个都绑了 CSH）
```

---

## 10. 文件存储策略

### 10.1 存储路径规划

| 内容 | 存储位置 | 路径格式 |
|------|---------|---------|
| 预置数据集本地副本 | 服务器本地 | `backend/datasets/{filename}.xlsx` |
| 用户上传数据集（MinIO） | MinIO `user-files` 桶 | `datasets/uploads/{uuid}.xlsx` |
| MinIO 数据集本地缓存 | 服务器本地 | `backend/data/dataset_cache/{filename}` |
| 可视化图表（MinIO） | MinIO `user-files` 桶 | `charts/{user_id}/{chart_id}.png` |
| 可视化图表（本地降级） | 服务器本地 | `backend/data/charts/{user_id}/{chart_id}.png` |

### 10.2 MinIO 超时设置

- 数据集上传（`upload_dataset`）：默认 urllib3 超时（约 30s），接受较慢上传
- 图表上传（`_save_to_minio`）：**5 秒超时**，快速降级本地，不影响响应速度

### 10.3 与现有上传体系对比

| 类型 | 端点 | 生命周期 | 用途 |
|------|------|---------|------|
| Chat 附件 | `POST /api/v2/chat-files/upload` | Thread 级（临时） | 对话分析单次 |
| 个人文件库 | `POST /api/v1/files/upload` | 用户级（长期） | 反复使用 |
| 知识库文档 | `POST /api/v1/uploads/batch` | KB 级（长期+RAG） | 知识检索 |
| **实验数据集** | `POST /api/v1/datasets/upload` | **全局/Agent 级（长期+SQL）** | **批量 SQL 分析** |

---

## 11. 数据清洗机制

### 11.1 问题背景

Excel 文件中常见两类数据质量问题：

**问题一：非标准空白字符污染**

科研人员使用 Excel 时复制粘贴可能引入 `\xa0`（不间断空格，Unicode U+00A0），导致原本是 `0.0658` 的数值被存储为 `'0.0658\xa0'`。DuckDB 对此进行严格类型检查时报错：

```
Conversion Error: Could not convert string '0.0658\xa0' to DOUBLE
```

**问题二：混合列（标头混数据）**

数据集1 的部分列使用 Excel 合并单元格结构，第一个非空值是标头字符串（如 `kobs(min-1)`），后续才是数值。pandas 读取后整列推断为 `object` 类型，`AVG()` 等聚合函数报类型错误。

### 11.2 修复方案（`_clean_dataframe`）

```python
# Step 1: 替换非标准空白（7 种字符）
_WHITESPACE = str.maketrans({
    "\xa0": " ", "\u3000": " ", "\u200b": "",
    "\u200c": "", "\u200d": "", "\ufeff": "", "\t": " ",
})
cleaned = x.translate(_WHITESPACE).strip()

# Step 2: 空字符串 → NaN
cleaned = cleaned.replace("", pd.NA)

# Step 3: 宽松数值推断（≥50% 非空值可转数值则整列转 float64）
numeric = pd.to_numeric(cleaned, errors="coerce")
success_rate = numeric[non_null_mask].notna().mean()
if success_rate >= 0.5:
    df[col] = numeric   # 不可转的（如标头字符串）变 NaN
```

### 11.3 修复效果

| 列名 | 修复前类型 | 修复后类型 | 影响 |
|------|---------|---------|------|
| `Degradation rate降解速率` | `VARCHAR` | `DOUBLE` | `AVG()`、`MAX()` 可直接使用 |
| `Reference 文献` | 未变 | `DOUBLE` | 引用编号正确识别为数值 |
| `OH reaction rate` | `DOUBLE`（已ok） | `DOUBLE` | 无变化 |

---

## 12. 日志噪声抑制

### 12.1 问题

`KafkaLogHandler` 在 Kafka Broker 不可达时：
1. 底层 `kafka.conn` / `kafka.client` 以指数退避重连，大量输出 ERROR/WARNING
2. `_flush_batch` 每次失败后 `producer = None`，下次 batch 立即重建 `KafkaProducer`，触发新一轮连接日志
3. 高峰期每隔数十毫秒一条错误，严重污染 `app.log` 和 `app.error.log`

### 12.2 修复（两处）

**修复1：`backend/config/logging.py`**

```python
"kafka.conn":   "CRITICAL",   # 屏蔽底层连接 ERROR/WARNING 噪声
"kafka.client": "CRITICAL",   # 屏蔽 check_version WARNING 噪声
```

**修复2：`backend/config/handlers.py` — `KafkaLogHandler`**

增加指数退避重连：

```python
_RECONNECT_INIT_DELAY = 30.0   # 首次失败后等 30s
_RECONNECT_MAX_DELAY  = 300.0  # 最长等 5 分钟

# _init_producer 中检查退避
now = time.monotonic()
if now < self._next_reconnect_at:
    return  # 退避期内，不重建 producer

# _flush_batch 失败后设置退避
self._next_reconnect_at = now + self._reconnect_delay
self._reconnect_delay = min(self._reconnect_delay * 2, 300.0)
```

修复后效果：Kafka 不可用时首次失败仅打印一行提示，之后静默等待，日志干净。

---

## 13. 已知问题与修复记录

### 13.1 `agent_codes` 列缺失（已修复）

**现象**：`pymysql.err.OperationalError: (1054, "Unknown column 'experiment_datasets.agent_codes'")`

**原因**：表在旧版本代码首次启动时创建，后来新增 `agent_codes` 列，`CREATE TABLE IF NOT EXISTS` 不会修改已有表。

**修复**：在线执行 DDL + 数据回填：
```sql
ALTER TABLE experiment_datasets ADD COLUMN agent_codes JSON;
UPDATE experiment_datasets SET agent_codes = '["csh"]'
WHERE code IN ('biochar_degradation', 'hydroxyl_radical_rates');
```

**预防**：`init_datasets.py` 中已加幂等 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，服务重启后自动执行。

### 13.2 SQL AVG 类型错误（已修复）

**现象**：`Binder Error: No function matches avg(VARCHAR)`

**原因**：Excel 混合列导致数值列被 pandas 推断为 `object`/VARCHAR。

**修复**：`_clean_dataframe()` 宽松数值推断（≥50% 阈值）。

### 13.3 图表 URL 不可访问（已修复）

**现象**：`http://202.115.138.101:9004/user-files/charts/chart_xxx.png` 浏览器无法访问。

**原因**：MinIO 端口（9004）仅内网可达，bucket 无公开访问策略，presigned URL 因 MinIO 内网地址问题也无效。

**修复**：图表改由 API Server（8082 端口）代理访问，URL 改为 `/api/v1/charts/{user_id}/{chart_id}`。

### 13.4 `DuckDBPyConnection` 不支持动态属性（已修复）

**现象**：`AttributeError: '_duckdb.DuckDBPyConnection' object has no attribute '_excel_table_map'`

**原因**：DuckDB 连接对象不允许设置任意 Python 属性。

**修复**：table_map 存入独立全局字典 `_multi_table_maps`，以 cache_key 为键。

---

## 14. 文件清单

### 14.1 新建文件

| 文件路径 | 行数 | 功能 |
|---------|------|------|
| `backend/db/models/dataset.py` | 95 | `ExperimentDataset` ORM 模型 |
| `backend/db/crud/dataset_crud.py` | 146 | CRUD + agent_code 过滤 + set_agent_codes |
| `backend/db/init_datasets.py` | 260 | 建表 + MinIO 上传 + 预注册两个数据集 |
| `backend/application/tools/providers/excel_engine.py` | 629 | DuckDB 查询引擎（单/多文件、清洗、缓存） |
| `backend/application/tools/providers/excel_chart_generator.py` | 525 | matplotlib+seaborn 图表生成（10 种类型） |
| `backend/application/tools/providers/excel_data_analysis_tool.py` | 708 | 5 个 LangChain 工具 |
| `backend/api/routers/datasets.py` | 526 | 9 个数据集管理端点 |
| `backend/api/routers/chart_images.py` | 142 | 图表图片代理端点 |
| `backend/skills/public/excel-experiment-analysis/SKILL.md` | 216 | Skill 定义（含 JOIN 示例） |
| `backend/datasets/` | — | 两个预置 Excel 文件（本地副本） |

### 14.2 修改文件

| 文件路径 | 修改内容 |
|---------|---------|
| `backend/db/models/__init__.py` | 导出 `ExperimentDataset` |
| `backend/application/tools/tool_assembly_pipeline.py` | 按 ToolPolicy 注入 Excel 工具 |
| `backend/api/http_server.py` | 注册 datasets、chart_images 路由；lifespan 加数据集初始化；静态图表目录 |
| `backend/config/logging.py` | `kafka.conn`/`kafka.client` 设为 CRITICAL |
| `backend/config/handlers.py` | `KafkaLogHandler` 加指数退避重连（初始 30s，上限 5 分钟） |
| `frontend_csh_ai/.../ChatBox.vue` | `availableSkills` 加 `excel-experiment-analysis` |
| `frontend/.../ChatBox.vue` | 同上 |

---

## 15. 使用示例

### 15.1 智能体完整分析对话

**用户**：帮我分析生物炭热解温度对污染物降解效率的影响，生成散点图

**智能体自主执行链**：

```
Round 1: list_experiment_datasets(agent_code="csh")
→ 返回: biochar_degradation（365行×47列）、hydroxyl_radical_rates（823行×9列）

Round 2: inspect_excel_data(dataset_code="biochar_degradation")
→ 返回: Sheet1 结构，47列详情，样本值
         关键列: "Preparation conditions 生物炭制备条件"(VARCHAR)
                 "Degradation rate降解速率"(DOUBLE←已自动转换)

Round 3: query_excel_data(
    dataset_code="biochar_degradation",
    sql="""
        SELECT "Preparation conditions 生物炭制备条件" AS condition,
               AVG("Degradation rate降解速率") AS avg_rate,
               STDDEV("Degradation rate降解速率") AS std_rate,
               COUNT(*) AS n
        FROM Sheet1
        WHERE "Degradation rate降解速率" IS NOT NULL
        GROUP BY condition
        ORDER BY avg_rate DESC
        LIMIT 15
    """
)
→ 返回 Markdown 表格（15 行，含均值、标准差）

Round 4: visualize_excel_data(
    dataset_code="biochar_degradation",
    chart_type="bar",
    sql="同上聚合 SQL",
    title="不同热解温度下的平均降解率（±标准差）"
)
→ 生成柱状图 PNG → 上传 MinIO/本地
→ 返回: ![不同热解温度下的平均降解率](/api/v1/charts/user_123/a1b2c3d4e5f67890?exp=<unix_ts>&sig=<hmac>)

Round 5: 综合分析 → 文字结论
"根据数据分析：热解温度为 500°C 时降解率最高（均值 0.33），
 其次为 600°C（0.28）... 建议最优制备温度范围为 500-600°C"
```

### 15.2 跨数据集 JOIN 分析

**用户**：请分析哪些污染物的·OH 反应速率高但生物炭降解率低（潜在难降解物质）

```python
# Step 1: 探查多数据集结构
inspect_excel_data(dataset_codes=["biochar_degradation", "hydroxyl_radical_rates"])

# Step 2: JOIN 查询
query_excel_data(
    dataset_codes=["biochar_degradation", "hydroxyl_radical_rates"],
    sql="""
    WITH ranked AS (
        SELECT
            a."污染物名称" AS pollutant,
            AVG(a."Degradation rate降解速率") AS avg_degrad,
            AVG(b."OH reaction rate") AS avg_oh_rate,
            b."Log k"
        FROM biochar_degradation__Sheet1 a
        JOIN hydroxyl_radical_rates__Sheet1 b
          ON LOWER(TRIM(a."污染物名称")) = LOWER(TRIM(b."名字"))
        WHERE a."Degradation rate降解速率" IS NOT NULL
        GROUP BY a."污染物名称", b."OH reaction rate", b."Log k"
    )
    SELECT pollutant, avg_degrad, avg_oh_rate, "Log k"
    FROM ranked
    WHERE avg_oh_rate > 1e10 AND avg_degrad < 0.3
    ORDER BY avg_oh_rate DESC
    """
)

# Step 3: 可视化（横轴 Log k，纵轴 avg_degrad）
visualize_excel_data(
    dataset_codes=["biochar_degradation", "hydroxyl_radical_rates"],
    chart_type="scatter",
    sql="上面的 CTE SQL",
    x_column="Log k",
    y_columns=["avg_degrad"],
    title="·OH 反应速率(Log k) 与生物炭降解率相关性"
)
```

### 15.3 API 管理操作

```bash
# 上传新数据集并绑定到 CSH 智能体
curl -X POST http://localhost:8082/api/v1/datasets/upload \
  -H "X-API-Key: your_api_key" \
  -F "file=@/path/to/new_experiment.xlsx" \
  -F "code=new_experiment" \
  -F "name=新实验数据集" \
  -F 'agent_codes=["csh"]' \
  -F "domain=environmental"

# 查看某数据集前 20 行
curl http://localhost:8082/api/v1/datasets/{id}/preview?rows=20 \
  -H "X-API-Key: your_api_key"

# 将数据集开放给两个智能体
curl -X PUT http://localhost:8082/api/v1/datasets/{id}/agents \
  -H "X-API-Key: your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"agent_codes": ["csh", "ne"]}'

# 刷新数据集 schema（文件更新后）
curl -X POST http://localhost:8082/api/v1/datasets/{id}/refresh \
  -H "X-API-Key: your_api_key"
```

---

## 16. 后续规划

| 功能 | 优先级 | 说明 |
|------|--------|------|
| 图表归属用户 | P1 | 通过 `InjectedToolCallId` → `thread_id` → `user_id` 链路，将图表存至 `charts/{real_user_id}/` |
| 前端数据集管理页 | P1 | 管理员界面：上传/列表/绑定/预览，参考 KB 管理页设计 |
| ~~多文件 JOIN~~ | ✅ 已实现 | — |
| ~~\xa0 数据清洗~~ | ✅ 已实现 | — |
| ~~Kafka 日志噪声~~ | ✅ 已修复 | — |
| 分析报告导出 | P2 | 将图表+文字分析结论导出为 DOCX/PDF |
| 权限细化 | P3 | 按 `user_id` 控制数据集访问（当前仅 agent 级） |
| 定时 schema 刷新 | P3 | 数据集文件更新后定时触发 `refresh` |
| DuckDB 持久化 | P3 | 大数据集（>10 万行）改用持久化 DuckDB 文件替代内存连接 |
