---
name: groundwater-project-analysis
description: 地下水项目资料、钻井数据、水文地质背景、质量检查与新井预测分析技能
domain: groundwater-project-analysis
tools:
  - groundwater_project_skill_read
  - groundwater_project_bootstrap
  - inspect_project_tables
  - query_project_tables
  - summarize_project_table
  - groundwater_prediction
  - groundwater_quality_check
  - groundwater_kml_summary
  - multi_kb_search
  - web_search
workflows:
  allowed:
    - full
    - multi_agent_parallel
    - multi_agent_alternating
  preferred: full
kb_policy:
  required: true
  binding_mode: agent_or_user_selected
guardrails:
  - 项目数据只能从当前请求绑定的项目上下文读取
  - 坐标系未确认时禁止跨坐标组距离计算和空间插值
  - 新井预测必须输出证据点位、数据版本、假设和不确定性
---

# 地下水项目分析

分析顺序：

当前文件由 `read_local_skill_file("groundwater-project-analysis")` 加载。未完成该步骤前，禁止调用下列项目、知识库或联网工具。

1. 使用 `groundwater_project_skill_read(resource="skill")` 读取项目 Skill，再调用一次 `groundwater_project_bootstrap` 获取真实表、字段、质量摘要和选表路径。
2. 仅在 bootstrap 不能满足字段确认时使用 `inspect_project_tables`；随后用 `query_project_tables` 通过 DuckDB SQL 查询井点、地层、井管、水样和水质检测数据。未知表名会被工具在执行前拒绝，不得继续猜测同义表。
3. 仅在工程背景、水文地质条件或质量限制与当前问题相关时，使用 `groundwater_project_skill_read` 按需读取 `background` 或 `data_quality`。
4. 使用 `groundwater_prediction` 完成同坐标组内的邻近井初步预测。
5. 使用 `groundwater_quality_check` 判断项目数据是否满足分析条件。
6. 项目包含 KML 时，使用 `groundwater_kml_summary` 汇总地物、分组、坐标范围、空名称和重复名称；明细关联再查询 `kml_features` 与 `kml_vertices`。
7. 使用 `multi_kb_search` 从地下水标准知识库获取规范依据。
8. 仅当知识库证据不足、需要核验最新标准状态或查询公开指南/政策时，使用 `web_search` 补充公开网页证据。

项目事实、算法推断和标准要求必须分别标识。不得将邻近井统计结果描述为已经验证的水文地质模型结论。
KML 经纬度与项目平面坐标在转换参数确认前不得混合计算距离、插值或用于工程放样。


## 联网搜索规则

1. 项目点位、检测值、井结构和项目质量问题必须来自 DuckDB 项目表，禁止用网页结果替代项目事实。
2. 标准限值优先检索绑定知识库；知识库缺失、版本状态不明或用户明确要求最新公开信息时才调用 `web_search`。
3. 联网结果必须注明网页标题、发布机构、发布日期或访问日期和 URL；无法确认权威来源时必须降低结论置信度。
4. 优先采用国家标准发布机构、生态环境部门、自然资源部门、卫生健康部门和地方政府等官方来源。

## 水质判断规则

1. 查询指定 GW 点位相对 IV 类水的超标情况时，优先调用 `groundwater_quality_check(point_code=..., standard_class="IV")`；该工具会基于 DuckDB 联合 `water_samples` 和 `water_quality_measurements`，并返回分层完整性、检测值、标准限值、差值和倍数。
2. 如果 `groundwater_quality_check` 不支持当前标准类别或返回字段不足，再使用 DuckDB 分别查询 `water_samples` 和 `water_quality_measurements`。
3. 必须按 `sample_layer` 分别核对 `upper`、`middle`、`lower`，不得只依据第一层作结论。
4. 超标判断应逐层列出检测值、标准限值、差值和检测值/限值倍数，并注明标准来源。

## 统一表格查询规则

1. 所有 manifest `datasets` 中的结构化数据只能通过 `groundwater_project_bootstrap`、`inspect_project_tables`、`query_project_tables` 和 `summarize_project_table` 处理。
2. 禁止使用 `groundwater_project_skill_read` 分页读取 JSON 表格，也不得根据文件顺序推断数据完整性。
3. 点位查询应在 SQL 中使用明确的 `WHERE point_code = ...`；跨表分析使用 JOIN，完整性判断使用 COUNT/GROUP BY。
4. 当上下文中已有 `GROUNDWATER_QUALITY_CHECK_RESULT` 或工具返回 `redundant_groundwater_quality_query` 时，表示关键项目数据已经取得；应直接写结论，不得继续重复查询同一点位完整水质明细。
5. `raw_ref` 是系统内部保存的原始结果引用，不是业务工具；不得围绕 raw_ref 继续追问或重复检索。
