# 地下水项目分析能力说明

> 文档版本：2026-06-24  
> 适用范围：当前 `groundwater` Agent、地下水项目目录服务、DuckDB 项目表工具、KML 解析链路与六塘标准化脚本。

## 1. 当前定位

地下水项目分析已经不再是单纯的“水文地质知识库问答”。当前实现是一个**请求级项目分析链路**：用户先选择一个被授权的项目，系统再围绕这个项目包读取 Skill、manifest、背景资料、质量说明和标准化 CSV 表，并结合地下水标准知识库给出结论。

主链路如下：

```text
用户在前端进入 /groundwater
  ↓
请求 metadata.groundwater_project_id
  ↓
DomainContextResolver -> resolve_groundwater_domain_context()
  ↓
GroundwaterProjectService.resolve() 校验项目存在性与用户授权
  ↓
groundwater Agent 进入 full 工作流
  ↓
read_local_skill_file("groundwater-project-analysis")
  ↓
groundwater_project_skill_read(resource="skill")
  ↓
groundwater_project_bootstrap()
  ↓
DuckDB 只读查询 / 质量检查 / KML 汇总 / 邻近井预测
  ↓
标准知识库检索，必要时 web_search 核验最新公开信息
```

这里最关键的变化是：**项目事实来自授权项目包，标准依据来自知识库，公开网页只做补充核验**。三者不能混用，更不能互相替代。

## 2. 核心设计变化

### 2.1 一个项目一个项目包

每个地下水项目都被组织成一个标准化目录。当前代码约定的典型结构如下：

```text
<project_root>/
├── SKILL.md
├── manifest.json
├── data/
├── docs/
│   ├── background.md
│   ├── data_quality.md
│   └── table_schema.md
├── images/
└── attachments/
```

`backend/application/groundwater/project_service.py` 中的 `GroundwaterProjectService` 负责：

- 创建项目目录和初始模板文件。
- 导入标准 ZIP 项目包。
- 读取 `skill / manifest / background / data_quality / table_schema` 资源。
- 维护 `data_version` 和 `manifest_snapshot`。
- 在文件系统目录与 MySQL 项目目录之间同步。

当前默认路径有两类：

- 普通项目目录：`data/groundwater_projects/`
- 内置六塘示例目录：`六塘项目数据信息/standardized/`

### 2.2 MySQL 项目目录与授权

当前地下水项目不再只依赖文件夹存在性，还引入了项目目录和授权表：

| 表 | 作用 |
| :--- | :--- |
| `groundwater_projects` | 记录项目编号、名称、区域、存储路径、状态、是否内置、数据版本、质量状态和 manifest 快照 |
| `groundwater_project_users` | 记录项目与用户的授权关系 |

当前行为要点：

- 普通用户只能访问被授权的项目。
- 管理员可以创建、编辑、归档、上传项目包，并维护项目授权。
- 内置项目会以 `read_only=true` 解析，不能更新、上传覆盖或删除。
- `/admin/projects` 当前只返回**非内置**项目；内置六塘示例属于只读样例，不在管理列表中作为普通可维护项目出现。

### 2.3 结构化数据统一走 DuckDB 工具

地下水项目的结构化事实不再建议通过 JSON 分页、普通文档检索或“猜表名”方式读取。当前规则是：

- `manifest.datasets` 中声明的 CSV 会被映射为 DuckDB 命名表。
- 先用 `groundwater_project_bootstrap` 获取真实表名、字段和 `table_routes`。
- 字段仍不确定时再调用 `inspect_project_tables`。
- 最终只通过 `query_project_tables` / `summarize_project_table` 读取结构化事实。

当前 SQL 限制由 [backend/application/tools/providers/project_table_tools.py](/home/inspur/myenv/langchian_project/backend/application/tools/providers/project_table_tools.py) 实施：

- 只允许 `SELECT` 或 `WITH`。
- 禁止 `read_csv`、`read_json`、`copy`、`create`、`insert`、`update`、`delete`、`drop`、`attach`、`install`、`pragma` 等访问文件、加载扩展或写入操作。
- `max_rows` 会被限制在 `1..1000`。
- 查询只作用于当前请求绑定的项目表，不能跨项目读取。

### 2.4 从“先 inspect”改为“先 bootstrap”

当前项目分析的核心不是“拿到项目后先扫全表 schema”，而是先拿一个**紧凑项目合同**：

- `groundwater_project_skill_read(resource="skill")`：读取当前项目自己的 `SKILL.md`。
- `groundwater_project_bootstrap()`：返回真实表、列名、质量摘要、坐标参考和高频选表路径。
- `inspect_project_tables()`：仅在 bootstrap 无法确认字段时补充使用。

`groundwater_project_bootstrap` 当前返回的核心字段包括：

- `tables`
- `table_routes`
- `quality_status`
- `coordinate_reference`
- `on_demand_resources`
- `workflow`

这也是当前 Prompt 和中间件要求的标准前置步骤。

## 3. Agent、Prompt 与中间件约束

### 3.1 Agent Profile

配置文件：`backend/config/agents/groundwater.yaml`

| 配置项 | 当前值 |
| :--- | :--- |
| `code` | `groundwater` |
| `domain` | `groundwater-project-analysis` |
| `default_workflow` | `full` |
| `allowed_workflows` | `full`、`multi_agent_parallel`、`multi_agent_alternating` |
| `default_skills` | `groundwater-project-analysis` |
| `tool_policy` | `groundwater_default` |
| `kb_policy` | `bound_or_selected` |
| `domain_context_provider` | `backend.application.groundwater.runtime_context:resolve_groundwater_domain_context` |

这里需要特别注意：**默认工作流仍是 `full`，但并不是“只允许 full”**。当前 profile 还允许并行/交替子智能体工作流，Prompt 里也已经加入了 `task` 委派规则。

### 3.2 请求上下文契约

调用方必须在请求 metadata 中传入项目编号：

```json
{
  "agent_code": "groundwater",
  "question": "分析 GW18 的 IV 类水超标情况",
  "metadata": {
    "groundwater_project_id": "liutang-groundwater"
  }
}
```

`resolve_groundwater_domain_context` 会读取 `metadata.groundwater_project_id`，并通过 `GroundwaterProjectService.resolve()` 校验项目存在性和用户权限。通过后，运行时上下文至少会带上：

```json
{
  "project_id": "liutang-groundwater",
  "project_name": "...",
  "data_version": 3,
  "read_only": true
}
```

### 3.3 当前强制步骤

当前 [backend/application/agents/prompt_templates/groundwater.py](/home/inspur/myenv/langchian_project/backend/application/agents/prompt_templates/groundwater.py) 与相关中间件共同约束以下顺序：

1. 先 `read_local_skill_file("groundwater-project-analysis")`。
2. 再 `groundwater_project_skill_read(resource="skill")`。
3. 再 `groundwater_project_bootstrap()`。
4. 只有字段仍不明确时才补 `inspect_project_tables(...)`。
5. 结构化事实统一通过 `query_project_tables(...)`。
6. 点位 IV 类水质判断优先走 `groundwater_quality_check(point_code=..., standard_class="IV")`。

当前有两层显式保护：

- `SkillLoadGuardMiddleware`：未加载通用 Skill 前，阻止项目查询和知识库工具。
- `GroundwaterProjectWorkflowMiddleware`：未完成“项目 Skill + bootstrap”前，阻止项目 SQL 查询。

这意味着当前链路已经从“建议顺序”升级为**运行时强约束**。

## 4. API 接口

所有接口挂载在 `/api/v2/groundwater` 下，对应路由文件 [backend/api/routers/groundwater_projects.py](/home/inspur/myenv/langchian_project/backend/api/routers/groundwater_projects.py)。

### 4.1 用户侧接口

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `GET` | `/projects` | 获取当前用户可访问的非内置项目列表；详情接口仍可解析已授权的只读内置项目 |
| `GET` | `/projects/{project_id}` | 获取项目 manifest 与只读状态 |
| `GET` | `/projects/{project_id}/resources/{resource}` | 读取项目资源 |

支持的 `resource`：

| resource | 文件 |
| :--- | :--- |
| `skill` | `SKILL.md` |
| `manifest` | `manifest.json` |
| `background` | `docs/background.md` |
| `data_quality` | `docs/data_quality.md` |
| `table_schema` | `docs/table_schema.md` |

注意：

- `read_resource` 只面向这些项目资源。
- 结构化数据集即使在 `manifest.datasets` 中声明，也不能通过这个接口分页替代 DuckDB 查询。

### 4.2 管理员接口

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `GET` | `/admin/projects` | 获取非内置项目管理列表，包含授权用户数 |
| `POST` | `/admin/projects` | 创建空项目目录和初始 `manifest.json` / `SKILL.md` |
| `PATCH` | `/admin/projects/{project_id}` | 更新项目名称、区域、坐标系 |
| `DELETE` | `/admin/projects/{project_id}` | 归档非内置项目 |
| `POST` | `/admin/projects/{project_id}/upload` | 上传标准 ZIP 项目包并导入 |
| `GET` | `/admin/users` | 获取可授权用户列表 |
| `GET` | `/admin/projects/{project_id}/bindings` | 查看项目授权 |
| `PUT` | `/admin/projects/{project_id}/bindings` | 覆盖项目授权用户 |

ZIP 导入约束：

- 只接受 `.zip`。
- ZIP 中必须且只能定位到一个 `manifest.json` 根。
- 导入前会检查 ZIP 路径穿越。
- 导入后 `manifest.data_version` 会自动加 1，并更新 MySQL 中的 `manifest_snapshot`。
- 内置只读项目不能被覆盖导入。

## 5. 工具能力与查询合同

当前 `groundwater_default` 工具策略包含：

| 工具 | 用途 |
| :--- | :--- |
| `groundwater_project_skill_read` | 读取当前项目的 Skill、manifest、背景、数据质量和表结构说明 |
| `groundwater_project_bootstrap` | 返回项目 SQL 合同：真实表、列名、质量摘要、坐标参考和选表路径 |
| `inspect_project_tables` | 查看当前项目 DuckDB 表名、字段类型和总行数 |
| `query_project_tables` | 对当前项目 CSV 数据执行只读 DuckDB SQL |
| `summarize_project_table` | 获取单表行数，用于完整性检查 |
| `groundwater_prediction` | 基于同一 `coordinate_profile` 内邻近点做新井深度和初见水位范围初步预测 |
| `groundwater_quality_check` | 返回项目质量问题，或对单点位执行 IV 类限值快速判断 |
| `groundwater_kml_summary` | 汇总 KML 地物、分组、坐标范围、空名称和重复名称 |
| `multi_kb_search` | 检索绑定的地下水标准知识库 |
| `web_search` | 知识库证据不足时核验最新标准、指南或政策 |
| `chat_file_search` | 检索当前会话上传的长文档 |

### 5.1 旧表名兼容映射

当前 `query_project_tables` 已支持一小层**受控别名兼容**，只在项目确实存在目标表时生效。典型映射包括：

| 旧名称 | 标准表 |
| :--- | :--- |
| `well_construction` / `wells` | `constructed_wells` |
| `well_locations` | `points` |
| `monitoring_wells` / `monitoring_points` | `points`（自动替换为监测点筛选子查询） |
| `boreholes` | `geotechnical_boreholes` |
| `stratigraphy` | `geology_layers` |
| `contamination_assessment` | `water_quality_measurements` |

其中 `stratigraphy` 还会把历史字段 `layer_order` 自动改写为当前标准字段 `layer_no`。

这层兼容是为了减少旧 Prompt/旧经验造成的误差，不是为了支持任意“猜表名”。如果仍命中未知表，工具会返回 `unknown_project_table`，并要求回到 bootstrap 或 inspect 的真实结果。

### 5.2 查询止损与预算语义

当前项目查询工具会区分“真正的数据查询”和“schema/校验信息”：

- `groundwater_project_bootstrap`：不是数据明细查询。
- `inspect_project_tables`：用于 schema 辅助。
- `query_project_tables` 命中 `duckdb_tables` / `information_schema` 等目录查询时，会直接返回项目表清单摘要，而不是放开系统目录访问。
- 只有成功的数据查询才标记 `counts_toward_query_budget=true`。

这套语义是为了避免模型在同义表名、系统目录和大范围 `SELECT *` 上浪费回合。

## 6. 当前专项能力

### 6.1 水质快速判断

`groundwater_quality_check` 当前有两种模式：

1. 不传 `point_code`
   返回项目级 `quality_status`、`coordinate_reference` 和 `quality_issues`。
2. 传入 `point_code` 且 `standard_class="IV"`
   返回单点位的 IV 类水快速判断结果。

当前 IV 类判断的特点：

- 基于 `water_quality_measurements` 与 `water_samples` 联合查询。
- 强制核对 `upper / middle / lower` 三层完整性。
- 内置常用 `GB/T 14848-2017` IV 类限值映射。
- 输出检测值、比较单位、标准限值、超标差值和倍数。
- `include_all_comparisons=true` 时可返回完整比较明细。

限制：

- 当前快速判断只内置 `IV` 类。
- 其它类别仍需 `multi_kb_search` + `query_project_tables` 联合核验。

### 6.2 KML 解析与空间摘要

当前代码已经有 KML 支持，不再是“待设计能力”：

- 解析器：[backend/services/kml_parser.py](/home/inspur/myenv/langchian_project/backend/services/kml_parser.py)
- 测试：[backend/tests/test_kml_parser.py](/home/inspur/myenv/langchian_project/backend/tests/test_kml_parser.py)
- 项目工具：`groundwater_kml_summary`

当前 KML 链路会：

- 安全解析本地 KML XML，不跟随样式或远程链接。
- 提取 `Placemark`、几何类型、顶点、分组、名称、坐标范围。
- 生成 `kml_features` 与 `kml_vertices` 两张结构化表。
- 为聊天文件解析器输出语义化 KML 文本，用于会话级检索。

但仍有严格边界：

- KML 经纬度不能和项目平面坐标直接混算距离。
- 在转换参数确认前，不能用 KML 做工程放样、插值或井位推荐。

### 6.3 新井初步预测

`groundwater_prediction` 当前是**邻近点统计工具**，不是数值模拟模型。它会：

- 只在同一 `coordinate_profile` 内找邻近点。
- 用 `points` 筛选候选点位。
- 结合 `constructed_wells` 与 `geotechnical_boreholes` 抽取井深和初见水位证据。
- 输出建议井深范围、初见水位范围、证据点列表、置信度和不确定性。

因此它适合“初步估计”和“解释式参考”，不适合作为正式设计或抽水试验替代。

## 7. 六塘示例项目标准化

标准化脚本：`scripts/standardize_liutang_groundwater.py`

当前关键常量：

```python
PROJECT_CODE = "liutang-groundwater"
DATA_VERSION = 3
```

脚本会生成：

- `SKILL.md`
- `manifest.json`
- `README.md`
- `六塘项目标准化数据.xlsx`
- `六塘项目标准化Skill包.zip`
- `data/*.csv`
- `docs/background.md`
- `docs/preliminary_design.md`
- `docs/project_readme.md`
- `docs/table_schema.md`
- `docs/data_quality.md`
- `docs/DATA_PROCESSING_STANDARD.md`
- `attachments/文件11_北部区域具体的地理位置图.kml`

当前主要标准化表包括：

| 表 | 说明 |
| :--- | :--- |
| `points` | 统一点位主表 |
| `constructed_wells` | 已建治理井 |
| `planned_wells` | 计划井位 |
| `planned_constructed_comparison` | 同编号计划井与已建井坐标对比 |
| `well_components` | 长管、短管、筛管和封闭结构 |
| `geotechnical_boreholes` | JC 地勘孔 |
| `geology_layers` | 自动解析地层记录 |
| `water_samples` | 上、中、下层水样 |
| `water_quality_measurements` | 水质检测长表 |
| `water_level_measurements` | 水位测量 |
| `kml_features` | KML 地物摘要 |
| `kml_vertices` | KML 顶点明细 |
| `quality_issues` | 标准化质量问题 |

当前回答六塘项目时，至少要显式考虑以下限制：

- `liutang_3659_271` 与 `liutang_271_597` 两组坐标的转换参数尚未确认，禁止跨组距离、插值和井位推荐。
- `ND` 表示未检出，不等于 0。
- `/` 表示未检测或无样品，不等同普通空值。
- `quality_flag=suspected_error` 只能作为待复核证据。
- Z55、P5、P6 等已建井坐标缺失时，不能用计划井坐标自动回填。
- 水位埋深源表单位标注存在疑点；标准化结果按米保留，引用时需要说明依据。
- KML 含空名称和重名地物，井号关联前应结合 `quality_issues` 复核。

## 8. 前端入口

### 8.1 用户入口

当前前端已提供单独地下水路由：

- 路由：`/groundwater/:threadId?`
- 组件：[frontend_csh_ai/src/views/groundwater/index.vue](/home/inspur/myenv/langchian_project/frontend_csh_ai/src/views/groundwater/index.vue)

该页面复用了通用聊天视图，但会显式传入：

- `agent-code="groundwater"`
- `project-mode`

因此地下水项目分析已经是**独立 agent 入口**，不是普通 `csh` 对话里再临时切换技能。

### 8.2 管理员入口

- API：`frontend_csh_ai/src/api/groundwater.ts`
- 页面：`frontend_csh_ai/src/views/admin/groundwater-projects.vue`
- 路由：`/admin/groundwater-projects`

当前管理页支持：

- 查看非内置项目列表、区域、数据版本和授权人数。
- 新建项目。
- 上传 ZIP 项目包。
- 编辑项目名称、区域和坐标系。
- 给普通用户授权项目。
- 删除非内置项目。

## 9. 建议验证

地下水相关文档同步后，建议至少验证：

```bash
venv314/bin/python -m pytest -q backend/tests/test_groundwater_project_runtime.py
venv314/bin/python -m pytest -q backend/tests/test_kml_parser.py
```

如需重建六塘项目包，再执行：

```bash
venv314/bin/python scripts/standardize_liutang_groundwater.py
unzip -t '六塘项目数据信息/standardized/六塘项目标准化Skill包.zip'
```
