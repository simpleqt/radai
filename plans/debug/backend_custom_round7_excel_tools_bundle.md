# 第七轮深度审查：Excel 工具包

**范围**：`excel_engine.py`、`excel_data_analysis_tool.py`、`excel_chart_generator.py`  
**日期**：2026-05-30 · **只读**

---

## 1. 架构概览

```text
┌────────────┐
│ Agent      │
└────────────┘

  Agent ──────────────► ToolAssembly

  ToolAssembly ──────────────► ExcelTools

  ExcelTools ──────────────► Engine

  Engine ──────────────► Charts

  Charts ──────────────► 本地路径

  本地路径 ──────────────► object_name 下载缓存

  object_name 下载缓存 ──────────────► PNG → MinIO → 代理 URL

  ToolAssembly ──(skill excel-experiment)──► ExcelTools

```

| 文件 | 行数约 | 职责 |
|------|--------|------|
| `excel_engine.py` | 524 | DuckDB 加载/缓存、SQL、schema、多库 JOIN |
| `excel_data_analysis_tool.py` | 588 | 5 个 LangChain 工具入口 |
| `excel_chart_generator.py` | 468 | matplotlib 出图 + `save_chart` |

---

## 2. P0/P1：任意路径读取（安全）

```69:73:backend/custom/tools/excel_data_analysis_tool.py
    if file_path:
        p = Path(file_path)
        if p.exists():
            return str(p.resolve()), p.name
```

- **无用户目录校验**：模型传入 `/etc/passwd`、其他用户上传路径、环境文件等，只要进程可读且 DuckDB/pandas 能解析（或仅 `inspect` 读元数据），即可被工具访问。
- `dataset_code` 路径来自 DB，相对安全；**风险集中在 `file_path` 参数**。
- 5 个工具均经 `_resolve_file_path` / `query` / `visualize` 间接使用。

**建议**：

1. 废弃 Agent 直传 `file_path`，仅允许 `dataset_code(s)` + 会话上传登记路径。  
2. 若保留：白名单目录（如 `users/{user_id}/uploads/**`），`resolve()` 后 `is_relative_to(allowed_root)`。  
3. 审计日志：记录 `user_id`、`dataset_code`、`resolved_path`。

---

## 3. P1：DuckDB SQL 无语句级沙箱

```418:428:backend/custom/tools/excel_engine.py
def execute_sql(file_path: str, sql: str, max_rows: int = _MAX_QUERY_ROWS) -> pd.DataFrame:
    conn = get_connection(file_path)
    try:
        result = conn.execute(sql).fetchdf()
```

- 模型可执行 **任意 DuckDB SQL**（含 `COPY`、`CREATE`、多语句若驱动允许）。
- 连接为 **进程内内存库**，不直接写用户磁盘，但可：
  - 消耗 CPU/内存（大 JOIN、笛卡尔积）→ **DoS**；
  - 在**同一缓存连接**内创建持久视图，影响后续同文件查询。
- `visualize_excel_data` 多库模式将用户 `sql` 与 `f" LIMIT {max_rows}"` **字符串拼接**（L493），若 `sql` 已含 `;` 注入第二条语句，存在风险（取决于 DuckDB 是否接受多语句）。

**建议**：

- 仅允许 `SELECT`/`WITH`（解析或正则前置校验）；拒绝 `;`。  
- 查询超时（`conn.execute` 包 `asyncio.wait_for` 或 DuckDB `SET statement_timeout`）。  
- 多库 LIMIT 用参数化或 `df.head(max_rows)` 而非拼接 SQL。

---

## 4. P2：全局 DuckDB 缓存无淘汰

```31:33:backend/custom/tools/excel_engine.py
_cache: dict[str, duckdb.DuckDBPyConnection] = {}
_multi_table_maps: dict[str, dict[str, list[str]]] = {}
_cache_lock = threading.Lock()
```

- 键为文件 hash（mtime+size），**无 LRU/TTL**；长进程 + 多用户/多数据集 → 内存持续增长。
- `invalidate_cache` 存在但 **工具层未在数据集更新后调用**。
- 每个连接持有完整 Excel 内存表，大文件 × N 用户 ≈ OOM 风险。

**建议**：`cachetools.LRU(maxsize=32)` + 进程内存上限告警；或按请求创建连接、请求结束 `close()`。

---

## 5. P2：MinIO 缓存文件名碰撞

```166:170:backend/custom/tools/excel_engine.py
        local_fname = Path(file_path).name
        local_path = cache_dir / local_fname
        if local_path.exists():
            return str(local_path)
```

- 不同 `object_name` 若 **basename 相同**（如 `data.xlsx`），会错误复用旧缓存，**跨用户/跨数据集脏读**。
- 应用 `hash(object_name)` 或完整路径编码作为本地文件名。

---

## 6. P2：错误与异常外泄

- 全模块大量 `return _error(f"...: {e}")` / `str(e)` 进入 **ToolMessage → 模型**，可能含路径、SQL、DB 细节。
- 对用户侧应统一「查询失败，请检查 SQL/数据集」；细节仅 `logger.exception`。

---

## 7. P3：工程与依赖

| 项 | 说明 |
|----|------|
| `visualize` + 无 `sql` | `SELECT * LIMIT {max_rows}` 默认全表列，大表仍可能拖慢绘图 |
| `style` 参数 | 传入 `matplotlib rcParams` 覆盖，需限制 key 白名单，防奇怪配置 |
| 中文图表 | `_get_chinese_font()` 启动时扫描字体，容器无中文字体时标签方框 |
| 测试缺口 | 无单测覆盖 Path-A、JOIN、缓存碰撞、路径校验 |

---

## 8. 与 tool_assembly 的关系

- Skill `excel-experiment-analysis` 通过 `get_excel_analysis_tools()` 注入 **5 个工具**（L59–69 `tool_assembly.py`）。
- 与 `nuclear-electronics` skill 的 `multi_kb_search` 门禁 **独立**；可同时启用 Excel + KB，成本叠加。
- **未**按 `agent_code`（NE/CSH）区分数据集列表；`list_experiment_datasets(domain=...)` 依赖 DB 字段，需确认 CSH 数据集是否注册。

---

## 9. 修复优先级建议

| 优先级 | 项 |
|--------|-----|
| P1 | `file_path` 路径白名单或移除 |
| P1 | SQL 只读 + 超时 + 禁止多语句拼接 |
| P2 | MinIO 缓存键唯一化 |
| P2 | DuckDB 连接 LRU |
| P3 | 异常脱敏、单测 |

---

*关联：[backend_custom_round5_core_security_tests.md](./backend_custom_round5_core_security_tests.md) · Issue P2-1 / P2-EXCEL-* in CSV*
