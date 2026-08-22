# 自定义学习闭环功能方案

> **最后同步**：2026-07-09  
> **当前状态**：第一期闭环已实现，后端路由为 `backend/api/routers/custom_learning.py`，服务编排为 `backend/services/custom_learning_agents.py`，前端调用为 `frontend/src/api/custom-learning.ts`。

## 1. 目标

围绕“探索式学习 / 自定义学习 / 学习风格”三个学习入口，形成一个可持续迭代的闭环学习系统：

```text
学习风格测评 → 自定义学习计划生成 → 节点学习执行 → 探索式学习补充理解
      ↑                                               ↓
      └──────────── 掌握度诊断 / 反馈 / 计划调整 ←──────┘
```

其中“自定义学习”作为闭环中心，负责把用户目标、时间、基础水平、学习风格、探索式学习结果和阶段测验结果组织成一条可执行、可追踪、可调整的学习路径。

### 1.1 闭环定义

本功能的闭环不是简单的“生成一个学习计划”，而是完整覆盖以下五个动作：

| 环节 | 用户动作 | 系统动作 | 产生数据 |
| --- | --- | --- | --- |
| 定目标 | 输入学习目标、基础、周期、知识库 | 解析目标并生成计划草稿 | `learning_plans`、`learning_plan_nodes` |
| 学内容 | 进入节点学习详解、资源和图谱 | 固化详解、资源和习题 | `explanation`、`quiz_questions`、`explore_cache` |
| 做检测 | 完成练习或任务 | 评分、诊断薄弱点 | `learning_node_attempts` |
| 算掌握 | 查看节点和计划进度 | 计算掌握度、开放后续节点 | `mastery`、`status` |
| 调路径 | 接受补救或调整建议 | 插入复习/补救节点，更新计划 | `learning_plan_adjustments` |

MVP 至少要打通“定目标 → 学内容 → 做检测 → 算掌握 → 开放后续节点”，第三期再补全自动调路径。

## 2. 三个方案的定位

| 模块 | 当前定位 | 闭环中的职责 | 关键产物 |
| --- | --- | --- | --- |
| 学习风格 | 了解用户偏好的学习输入、加工和理解方式 | 提供个性化表达策略、任务形态和节奏建议 | `LearningStyleProfile` |
| 自定义学习 | 选择或生成学习计划 | 闭环主控台，生成路径、执行节点、记录进度、触发诊断和调整 | `LearningPlan`、`LearningNodeProgress` |
| 探索式学习 | 围绕主题生成知识图谱、详解、资源和习题 | 为学习节点补充知识结构、扩展分支、生成练习与资源证据 | `ExploreMap`、`Detail`、`Quiz`、`Resources` |

核心原则：学习风格只影响“怎么学”和“怎么表达”，不影响知识正确性；探索式学习负责“把知识展开”，自定义学习负责“把学习跑完”。

## 3. 当前基础

### 3.1 已具备能力

- 前端已有三个路由：
  - `/explore`、`/m/explore`
  - `/custom-learning`、`/m/custom-learning`
  - `/learning-style`、`/m/learning-style`
- `frontend/src/views/custom-learning/index.vue` 已有预制计划列表、计划详情和学习路径入口。
- `frontend/src/views/custom-learning/path.vue` 已有学习路径图和节点展示。
- `backend/api/routers/explore.py` 已有主题、脑图、节点展开、详解、资源、习题和评分接口。
- `backend/api/routers/learning_style.py` 已有学习风格题库、答题记录、结果画像和“应用于智能助手”开关。
- `backend/db/models/__init__.py` 已有 `LearningStyleAttempt` 和 `LearningStyleProfile`。
- `backend/db/models/custom_learning.py` 已有 `LearningPlan`、`LearningPlanNode`、`LearningNodeAttempt`、`LearningPlanAdjustment`。
- `frontend/src/api/custom-learning.ts` 已接入计划列表、工作台、生成、路径、节点、测验、探索、富集、调整、复盘等接口。

### 3.2 主要缺口（历史记录，2026-06 前）

> **2026-07 更新**：以下缺口已在第一期实现中补齐，当前状态见 [§17 当前实现状态](#17-当前实现状态)。

- ~~自定义学习计划仍是前端静态数据，缺少后端持久化。~~
- ~~没有“创建计划”表单和计划生成服务。~~
- ~~没有节点级学习记录、掌握度、测验结果和复习计划。~~
- ~~探索式学习生成的知识图谱、资源、习题尚未回写到学习计划。~~
- ~~学习风格结果尚未进入自定义学习的计划生成和节点内容编排。~~

## 4. 闭环用户流程

### 4.1 首次进入

1. 用户进入“自定义学习”。
2. 系统检查是否存在学习风格画像。
3. 若没有画像，提示可先完成学习风格测评；用户也可以跳过。
4. 用户点击“新建计划”，填写目标、基础、时间、课程范围和偏好。
5. 后端生成学习计划草稿，用户确认后保存。
6. 进入学习路径页，从第一个开放节点开始学习。

### 4.2 单节点学习闭环

每个学习节点按如下闭环执行：

```text
进入节点
  → 查看目标、前置知识、推荐资源
  → 调用探索式学习生成/读取知识图谱和详解
  → 完成练习或小任务
  → 提交答案或学习产物
  → 评分与诊断
  → 更新掌握度
  → 达标则开放后续节点，不达标则生成补救任务
```

### 4.3 计划级调整闭环

系统在以下时机触发计划调整：

- 用户连续多个节点低于掌握阈值。
- 用户实际耗时明显高于计划耗时。
- 用户主动修改学习目标或可用时间。
- 探索式学习发现当前节点缺少前置概念。
- 周期复盘时发现遗忘风险较高。

调整结果包括：插入补充节点、降低或提高难度、改变资源类型、增加练习、重新排序节点、更新完成日期。

### 4.4 状态机与事件

自定义学习应以事件驱动更新状态，避免前端直接推断业务结果。

```text
Plan: draft → active → completed
                ↓
             archived

Node: locked → open → learning → done
                           ↓
                         review
```

| 事件 | 触发方式 | 后端处理 |
| --- | --- | --- |
| `plan.generated` | 用户提交新建计划表单 | 生成计划草稿和节点草稿，状态为 `draft` |
| `plan.activated` | 用户确认计划 | 第一个节点设为 `open`，计划设为 `active` |
| `node.started` | 用户打开节点并点击开始 | 节点设为 `learning`，记录学习开始行为 |
| `node.explanation_viewed` | 用户查看详解 | 若无固化详解则生成并保存 |
| `node.quiz_submitted` | 用户提交练习 | 保存 attempt，计算得分和诊断 |
| `node.mastery_updated` | 练习或任务完成后 | 刷新节点掌握度和计划掌握度 |
| `node.completed` | 掌握度达标或用户手动完成 | 节点设为 `done`，开放下一个可学习节点 |
| `plan.adjustment_suggested` | 低分、延期、复习周期到达 | 生成调整建议，等待用户接受 |
| `plan.adjustment_accepted` | 用户接受调整 | 写入新增节点或重排节点，刷新计划 |

## 5. 自定义学习 MVP 范围

第一期优先完成“能创建、能学习、能记录、能调整”的最小闭环。

### 5.1 必做功能

- 新建学习计划表单：
  - 学习目标
  - 课程/领域
  - 当前基础
  - 每日可用时间
  - 计划周期
  - 期望深度
  - 关联知识库
- 计划生成：
  - 生成学习目标、节点路径、资源建议、练习任务和验收标准。
  - 若用户开启学习风格应用，则调整表达方式和任务类型。
- 计划保存与列表：
  - 展示用户自己的计划。
  - 支持继续学习、重命名、归档。
- 节点学习页：
  - 展示目标、解释、资源、练习和检查点。
  - 支持标记完成、提交测验结果。
- 掌握度记录：
  - 节点掌握度 `0-100`。
  - 计划总进度。
  - 最近学习时间。
- 简单调整：
  - 测验低于阈值时生成“补充解释 + 追加练习”。
  - 完成节点后开放下一个节点。

### 5.2 暂不做

- 不做复杂的学习能力预测模型。
- 不做教师端班级管理。
- 不做跨用户推荐排序。
- 不把学习风格结果作为硬性分流依据。
- 不自动修改知识库内容。

## 6. 数据模型设计

### 6.1 learning_plans

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| plan_id | string | 学习计划 ID |
| user_id | string | 用户 ID |
| title | string | 计划标题 |
| domain | string | 领域，如 `ne` |
| goal | text | 用户学习目标 |
| baseline | string | 基础水平 |
| depth | string | 学习深度 |
| duration_days | int | 计划天数 |
| daily_minutes | int | 每日学习分钟数 |
| kb_ids | json | 关联知识库 |
| style_snapshot | json | 创建计划时采用的学习风格快照 |
| status | string | `draft` / `active` / `completed` / `archived` |
| progress | int | 总进度百分比 |
| mastery | int | 总掌握度 |
| accent | string | 前端计划卡片色彩标识 |
| surface | string | 前端计划卡片视觉层级 |
| course_type | string | 课程或模板类型 |
| metadata | json | 扩展元数据；ORM 字段名为 `metadata_` |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

建议补充字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| template_id | string | 来源模板 ID，可为空 |
| summary | text | 计划摘要 |
| current_node_id | string | 当前推荐学习节点 |
| started_at | datetime | 激活时间 |
| completed_at | datetime | 完成时间 |
| archived_at | datetime | 归档时间 |

以上“建议补充字段”在当前 `LearningPlan` ORM 中已落地。

### 6.2 learning_plan_nodes

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| node_id | string | 节点 ID |
| plan_id | string | 计划 ID |
| parent_node_id | string | 父节点，可为空 |
| order_index | int | 路径顺序 |
| title | string | 节点标题 |
| subtitle | string | 节点副标题，可为空 |
| objective | text | 学习目标 |
| explanation | text | 固化存储的详解文本，防止重新访问时内容漂移 |
| quiz_questions | json | 固化存储的习题库，防止重新访问时题目漂移 |
| prerequisites | json | 前置知识 |
| resources | json | 推荐资源 |
| tasks | json | 学习任务 |
| checks | json | 检查点 |
| resource | text | 兼容旧模板的单条资源 |
| task | text | 兼容旧模板的单条任务 |
| check | text | 兼容旧模板的单条检查 |
| day | string | 兼容旧模板的日期/阶段标签 |
| estimated_minutes | int | 预计耗时 |
| status | string | `locked` / `open` / `learning` / `done` / `review` |
| mastery | int | 节点掌握度 |
| explore_cache | json | 关联探索式学习结果摘要 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

建议补充字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| node_type | string | `concept` / `practice` / `project` / `review` |
| difficulty | string | `basic` / `medium` / `advanced` |
| unlock_rule | json | 前置节点和掌握度阈值 |
| study_signals | json | 是否看过详解、资源、图谱等学习行为 |
| due_at | datetime | 计划完成时间 |
| completed_at | datetime | 实际完成时间 |

以上“建议补充字段”在当前 `LearningPlanNode` ORM 中已落地。新逻辑优先使用结构化 `resources/tasks/checks/prerequisites`，`resource/task/check/day` 主要用于兼容前端模板和历史数据。

### 6.3 learning_node_attempts

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| attempt_id | string | 节点学习记录 ID |
| node_id | string | 节点 ID |
| user_id | string | 用户 ID |
| action_type | string | `study` / `quiz` / `task` / `review` |
| payload | json | 答案、任务产物或学习行为 |
| score | int | 得分 |
| diagnosis | json | 错因、薄弱点、建议 |
| created_at | datetime | 创建时间 |

建议补充字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| duration_seconds | int | 本次学习或作答耗时 |
| mastery_before | int | 行为发生前节点掌握度 |
| mastery_after | int | 行为发生后节点掌握度 |
| source | string | `manual` / `explore_quiz` / `agent_grade` |

### 6.4 learning_plan_adjustments

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| adjustment_id | string | 调整 ID |
| plan_id | string | 计划 ID |
| trigger_type | string | `low_score` / `slow_progress` / `user_change` / `review_due` |
| before_snapshot | json | 调整前计划摘要 |
| after_snapshot | json | 调整后计划摘要 |
| reason | text | 调整原因 |
| accepted | bool | 用户是否接受 |
| created_at | datetime | 创建时间 |

### 6.5 索引与约束

- `learning_plans.user_id + status + updated_at`：用于计划列表。
- `learning_plan_nodes.plan_id + order_index`：用于路径图排序。
- `learning_plan_nodes.plan_id + status`：用于查找当前开放节点。
- `learning_node_attempts.node_id + user_id + created_at`：用于学习记录和最近一次练习。
- 所有 plan/node 查询必须校验 `user_id`，不能只按 `plan_id` 或 `node_id` 查询。
- 删除计划时级联删除节点、attempt 和 adjustment，归档计划不删除历史记录。

## 7. API 设计

已实现 `backend/api/routers/custom_learning.py`，挂载前缀 `/api/v1/custom-learning`（见 `http_server.py`）。

> [!IMPORTANT]
> **鉴权与隔离要求**：所有自定义学习接口必须通过 `user: User = Depends(get_current_user)` 强制要求用户登录。不支持游客/访客无凭证访问，避免引发数据库级外键约束异常及恶意消耗 LLM/RAG 计算资源。

| 方法 | 路径 | 用途 | 说明 |
| --- | --- | --- | --- |
| GET | `/knowledge-bases` | 获取自定义学习可选择的知识库 | 支持 `agent_code` 过滤，验证当前用户可见性 |
| GET | `/workspace` | 获取学习工作台聚合数据 | 返回计划列表、当前计划、模块效果、下一步建议等 |
| GET | `/plans` | 获取我的学习计划列表 | 仅返回简短元数据，以保证列表页流畅 |
| POST | `/plans` | 创建学习计划草稿 | 确认保存最终路径 |
| POST | `/plans/generate` | 根据目标生成计划 | 当前为**同步 JSON 规则版**（`CustomLearningAgentTeam`）；后续可替换为 LLM/SSE 且保持响应 schema 不变 |
| GET | `/plans/{plan_id}` | 获取计划详情 | 包括进度、标题、掌握度及属性 |
| GET | `/plans/{plan_id}/dashboard` | 获取计划仪表盘 | 汇总节点状态、学习风格影响、复盘和下一步动作 |
| PATCH | `/plans/{plan_id}` | 修改标题、目标、周期等 | |
| POST | `/plans/{plan_id}/activate` | 确认并启用计划 | 激活后，将第一个节点置为 `open` |
| POST | `/plans/{plan_id}/next-action` | 执行下一步建议 | 根据当前节点自动开始、补内容或推进 |
| POST | `/plans/{plan_id}/archive` | 归档计划 | |
| GET | `/plans/{plan_id}/nodes` | 获取节点路径 | **轻量化骨架数据** (不含详解与习题文本)，仅供路径图绘制 |
| GET | `/nodes/{node_id}` | 获取节点详情 | **延迟加载**节点的详解(explanation)和标准习题(quiz_questions) |
| POST | `/nodes/{node_id}/start` | 开始学习节点 | |
| POST | `/nodes/{node_id}/complete` | 标记节点完成 | |
| POST | `/nodes/{node_id}/quiz` | 生成或读取节点练习 | 读取 node 中的 `quiz_questions` 固化题库，如未固化则生成并保存 |
| POST | `/nodes/{node_id}/quiz/grade` | 提交练习并评分 | |
| POST | `/nodes/{node_id}/explore` | 为节点生成探索式学习内容 | 整合 explore 模块服务 |
| POST | `/nodes/{node_id}/enrich` | 富集或修复节点内容 | 补详解、资源、测验和学习提示并固化 |
| POST | `/plans/{plan_id}/adjust` | 触发计划调整 | |
| GET | `/plans/{plan_id}/review` | 获取复盘和复习建议 | |

### 7.1 关键接口请求响应

#### 7.1.1 生成计划

`POST /api/v1/custom-learning/plans/generate`

**当前实现**：同步返回 JSON。路由会读取学习风格 profile（仅当 `use_learning_style=true` 且 `apply_to_ai=true`）、校验可选知识库，调用 `CustomLearningAgentTeam.generate_plan_blueprint()` 生成结构化蓝图，并直接创建保存计划与节点。

**请求示例**：

```json
{
  "goal": "系统掌握半导体探测器与低噪声读出",
  "domain": "ne",
  "baseline": "has_basic",
  "depth": "analysis",
  "duration_days": 10,
  "daily_minutes": 90,
  "kb_ids": ["kb-001"],
  "use_learning_style": true,
  "template_id": "semiconductor-detectors"
}
```

**响应**：`{"plan": {...}}` 含完整节点列表与 `agent_trace`。`agent_trace` 当前来自规则化多 Agent 编排摘要，便于前端展示生成依据；它不是 SSE 流式事件日志。

**后续可选（尚未实现）SSE 事件建议**：

```text
planning.started
planning.kb_context_loaded
planning.outline_created
planning.nodes_created
planning.validated
planning.completed
planning.failed
```

完成事件 payload：

```json
{
  "plan_id": "plan-001",
  "status": "draft",
  "title": "半导体探测器专项学习",
  "node_count": 8,
  "estimated_minutes": 900
}
```

#### 7.1.2 计划详情

`GET /api/v1/custom-learning/plans/{plan_id}`

```json
{
  "plan": {
    "plan_id": "plan-001",
    "title": "半导体探测器专项学习",
    "status": "active",
    "progress": 25,
    "mastery": 68,
    "current_node_id": "node-003"
  },
  "style_applied": true,
  "stats": {
    "total_nodes": 8,
    "done_nodes": 2,
    "open_nodes": 1,
    "review_nodes": 0
  }
}
```

当前还可通过 `GET /plans/{plan_id}/dashboard` 获取更完整的工作台摘要，包括节点分布、弱点、学习风格影响、复盘建议和下一步动作。

#### 7.1.3 节点详情

`GET /api/v1/custom-learning/nodes/{node_id}`

返回完整节点内容。若 `explanation` 或 `quiz_questions` 为空，可由接口内部按需生成并固化，也可以返回 `content_status` 让前端显式触发生成。

当前接口支持查询参数 `include_content`；`include_content=false` 时轻量读取节点骨架，`true` 时确保详解、资源、学习任务和测验内容可用。

```json
{
  "node": {
    "node_id": "node-003",
    "title": "电荷灵敏前置放大器",
    "status": "learning",
    "mastery": 55,
    "objective": "解释反馈电容如何完成电荷到电压转换",
    "explanation": "...",
    "quiz_questions": [],
    "resources": [],
    "checks": []
  },
  "content_status": {
    "explanation_ready": true,
    "quiz_ready": false
  }
}
```

#### 7.1.4 内容富集

`POST /api/v1/custom-learning/nodes/{node_id}/enrich`

```json
{
  "focus": "explanation"
}
```

`focus` 可为空或指定局部内容。接口会确保节点内容可学习，并把新生成或修复后的解释、资源、任务、检查点或测验固化到节点字段。

#### 7.1.5 练习评分

`POST /api/v1/custom-learning/nodes/{node_id}/quiz/grade`

```json
{
  "answers": {
    "q1": "A",
    "q2": "反馈电容决定电荷到电压的转换增益"
  },
  "duration_seconds": 420
}
```

响应：

```json
{
  "attempt_id": "attempt-001",
  "score": 82,
  "mastery_before": 55,
  "mastery_after": 76,
  "node_status": "done",
  "unlocked_node_ids": ["node-004"],
  "diagnosis": {
    "weak_points": ["复位方式区分不清"],
    "suggestions": ["复习反馈电阻复位与开关复位的差异"]
  }
}
```

## 8. 计划生成逻辑

### 8.1 输入

```json
{
  "goal": "系统掌握半导体探测器与低噪声读出",
  "domain": "ne",
  "baseline": "有基础",
  "depth": "分析层",
  "duration_days": 10,
  "daily_minutes": 90,
  "kb_ids": ["..."],
  "use_learning_style": true
}
```

### 8.2 生成步骤

1. 解析目标，识别主题、能力层级和交付物。
2. 从已选择且当前用户可访问的知识库召回相关主题、章节、术语和资源；未选择知识库时使用规则化主题库与推荐资源兜底。
3. 生成知识结构：核心概念、前置概念、进阶概念和应用任务。
4. 按时间预算拆成学习节点。
5. 为每个节点生成：
   - 学习目标
   - 前置知识
   - 推荐资源
   - 探索式学习主题
   - 练习任务
   - 掌握检查点
6. 读取学习风格画像，只调整呈现方式：
   - 视觉型：节点中优先增加图表、流程图、对比表。
   - 言语型：增加定义、解释、类比和逐段说明。
   - 活跃型：增加操作任务、讨论题和即时练习。
   - 沉思型：增加思考题、总结点和自检问题。
   - 序列型：按小步递进拆分节点。
   - 综合型：先给整体框架，再展开细节。
7. 输出计划草稿，由用户确认。

### 8.2.1 生成质量控制

计划草稿保存前必须通过结构化校验：

- 节点数量与时间预算匹配：`总预计分钟数 <= duration_days * daily_minutes * 1.2`。
- 每个节点必须包含 `objective`、`checks`、`estimated_minutes`。
- 第一个节点不能为 `locked`。
- 除第一个节点外，每个 `locked` 节点必须有 `unlock_rule`。
- 每个检查点必须是可验证表述，例如“能解释/能计算/能画出/能比较”，避免“理解得更好”这类空泛文案。
- 知识库资源引用必须包含 `kb_id`、`doc_id` 或明确标记 `source_type=suggested`。
- 如果使用学习风格画像，必须保存 `style_snapshot`，避免用户后续重新测评导致历史计划解释漂移。

### 8.3 输出结构

```json
{
  "title": "半导体探测器专项学习",
  "summary": "10 天完成从 PN 结、耗尽区到前置放大与噪声预算的学习。",
  "nodes": [
    {
      "title": "PN 结与耗尽区",
      "objective": "解释反偏 PN 结形成灵敏体积的机制",
      "estimated_minutes": 90,
      "prerequisites": ["半导体能带", "载流子漂移"],
      "explore_topic": "PN 结耗尽区与辐射探测",
      "resources": [],
      "tasks": ["画出耗尽区变化示意", "完成 3 道概念判断题"],
      "checks": ["能说明偏置电压对耗尽层厚度的影响"]
    }
  ]
}
```

### 8.4 学习风格映射策略

| 偏好 | 计划生成影响 | 节点内容影响 | 不允许做的事 |
| --- | --- | --- | --- |
| 活跃型 | 增加练习、小任务、即时反馈 | 每个节点至少一个操作题或讨论题 | 不减少反思和总结 |
| 沉思型 | 增加自检、总结和思考停顿 | 节点结尾提供反思问题 | 不取消必要练习 |
| 感知型 | 增加案例、实验数据、参数计算 | 资源优先关联事实材料 | 不省略原理解释 |
| 直觉型 | 增加原理联系、迁移问题 | 强化概念关系和开放题 | 不跳过基础事实 |
| 视觉型 | 增加流程图、表格、结构图 | 详解优先提供 Mermaid 或对比表 | 不只给图不解释 |
| 言语型 | 增加定义、文字解释、类比 | 详解按段落逐步展开 | 不完全去掉图表 |
| 序列型 | 拆成更小节点，线性递进 | 每节按步骤推进 | 不隐藏全局目标 |
| 综合型 | 先总览再细节 | 节点开头给知识地图 | 不跳过步骤验证 |

## 9. 探索式学习与缓存固化机制

自定义学习不重新实现探索式学习，而是复用现有 `/api/v2/explore` 能力，同时增加固化缓存以防大模型生成发生内容漂移。

### 9.1 详解内容固化

- **按需固化**：在节点首次被置为 `open`/`learning` 状态或用户点击“查看详解”时，调用后端 Agent 引擎生成详解。
- **缓存写入**：生成的详解 Markdown 文本保存至 `learning_plan_nodes.explanation` 字段中。
- **后续读取**：用户二次或多次访问该节点时，后端直接返回数据库中存储的 `explanation` 内容，无需再次调用 LLM，从而保障内容一致性。

### 9.2 节点练习固化

- **习题固化**：在计划激活或节点开启时，调用 `/explore/quiz` 一次性生成该节点的习题包（包含正确答案与解析）。
- **缓存写入**：习题的 JSON 结构体固化在 `learning_plan_nodes.quiz_questions` 字段。
- **作答与诊断**：
  - 用户答题时，加载固化好的 `quiz_questions`。
  - 用户提交答案时，调用 `/explore/quiz/grade` 评分，并产生 `diagnosis` 与 `score`。
  - 评分结果与用户答题详情写入 `learning_node_attempts`。
  - 每次作答都会在 attempts 中留下带时间戳的记录，而原节点的 `quiz_questions` 保持不变，支持多次重复作答与错题集汇编。

### 9.3 回写与摘要存储

为了防止数据臃肿，数据库中不重复存储大段的 Explore 交互历史，而是在 `learning_plan_nodes.explore_cache` 存储轻量化的检索摘要：

```json
{
  "topic": "电荷灵敏前置放大器",
  "map_id": "local-cache-key",
  "resource_refs": ["doc-1", "doc-2"],
  "weak_points": ["反馈电容作用", "噪声来源区分"],
  "last_explored_at": "2026-06-25T10:00:00"
}
```

## 10. 掌握度与开放规则

### 10.1 节点掌握度

建议 MVP 使用可解释规则：

```text
节点掌握度 = 学习完成度 30% + 练习得分 50% + 任务完成 20%
```

- 学习完成度：是否查看节点详解、资源、关键概念。
- 练习得分：该节点下最近一次完成（或历史最高一次）的 quiz 测验得分，避免前期未掌握时的低分持续拉低用户当下的掌握度。
- 任务完成：用户是否提交小任务或自检结果。

### 10.2 状态流转

```text
locked → open → learning → done
                    ↓
                  review
```

- `locked`：前置节点未达标。
- `open`：可以开始学习。
- `learning`：已有学习行为。
- `done`：掌握度达到阈值，默认 `>= 70`。
- `review`：曾经完成，但复习周期已到或后续测验暴露遗忘。

### 10.3 计划进度

```text
计划进度 = done 节点数 / 总节点数
计划掌握度 = 所有节点 mastery 加权平均
```

节点可按预计耗时加权，避免一个 10 分钟节点和一个 2 小时项目节点权重相同。

### 10.4 补救与调整规则

MVP 使用规则引擎即可，不需要复杂模型：

| 条件 | 动作 |
| --- | --- |
| 最近一次 quiz `< 60` | 节点保持 `learning`，生成 1 组补充练习 |
| 连续两次 quiz `< 70` | 生成 `review` 类型补救节点，插入当前节点之后 |
| 节点耗时超过预计 `150%` | 标记 `slow_progress`，计划页提示可调整节奏 |
| 完成节点后 mastery `>= 70` | 当前节点 `done`，开放下一个节点 |
| 完成节点后 mastery `>= 90` | 可跳过同主题低阶复习节点，但必须用户确认 |
| 已完成节点 7 天未复习且后续相关题低分 | 状态转为 `review` |

补救节点不应无限生成。建议同一原始节点最多生成 2 个自动补救节点，之后给用户明确提示“建议人工调整学习目标或降低节奏”。

## 11. 前端实现

### 11.1 `/custom-learning`

当前已从静态计划列表改为真实计划工作台：

- 顶部展示当前进行中的计划、总进度、今日任务。
- 计划卡片来自 `GET /custom-learning/plans`。
- “新建计划”打开表单抽屉或弹窗。
- 若存在学习风格画像，展示“已应用学习风格”；若没有，给出测评入口。
- 保留预制计划作为“模板”，点击后调用生成接口创建用户计划。

页面核心区块：

| 区块 | 内容 | 数据来源 |
| --- | --- | --- |
| 今日继续 | 当前推荐节点、预计耗时、上次学习时间 | `current_node_id` |
| 进行中计划 | 标题、进度、掌握度、状态 | `GET /plans` |
| 模板计划 | 现有预制计划模板 | 前端常量 + `POST /plans` 或 `POST /plans/generate` |
| 学习画像 | 是否完成测评、是否应用 | `GET /learning-style/profile` |
| 新建计划 | 表单入口 | 本地表单 + `POST /plans/generate` |

### 11.2 `/custom-learning/path/:planId`

当前已从静态路径图改为计划详情：

- 根据 `planId` 获取真实节点。
- 节点状态来自后端。
- 点击节点打开右侧/底部学习面板：
  - 目标
  - 资源
  - 探索图谱
  - 详解
  - 练习
  - 检查点
- 完成练习后即时刷新掌握度和后续节点状态。

节点面板使用学习、资源、练习、复盘等信息分区：

| Tab | 内容 | 行为 |
| --- | --- | --- |
| 学习 | 目标、详解、关键概念 | 首次进入时固化详解 |
| 资源 | 知识库来源、推荐阅读、下载入口 | 复用 Explore resources |
| 练习 | 固化习题、提交答案、评分反馈 | 提交后写 attempt |
| 复盘 | 掌握度、薄弱点、补救建议 | 来自 attempts 和 adjustment |

### 11.3 与学习风格页面联动

- 学习风格结果页增加“用于生成自定义学习计划”入口。
- 自定义学习新建计划时可读取 `LearningStyleProfile.apply_to_ai`。
- 用户未开启时，只展示“可选择应用”，不默认使用画像。

### 11.4 前端状态与空态

- 未登录：提示登录后创建和保存计划，不调用自定义学习接口。
- 无计划：展示模板计划和“从目标生成计划”入口。
- 有草稿：卡片显示“继续完善”，允许激活或删除。
- 生成中：当前为同步请求；若后续接入 SSE，可在不阻塞页面的前提下展示规划进度。
- 生成失败：保留用户输入，允许重试；错误信息区分模型失败、知识库为空、鉴权失败。
- 节点内容生成中：节点面板内局部 loading，不刷新整页。
- 移动端：节点面板使用底部 Sheet，路径图保持横向滚动。

## 12. 后端服务与分层划分

当前实现按“ORM 模型 + 路由内业务编排 + 轻量多 Agent 服务”的方式落地，尚未拆出独立 CRUD/domain/application 包：

```text
backend/db/models/custom_learning.py      # 定义数据表模型 (LearningPlan, LearningPlanNode 等)
backend/api/routers/custom_learning.py    # 请求模型、权限校验、计划/节点/测验/调整编排
backend/services/custom_learning_agents.py # 规则版目标解析、路径规划、内容组织、质量校验
frontend/src/api/custom-learning.ts       # 前端接口契约
```

职责划分：

- **数据模型层 (`models/custom_learning.py`)**：负责 SQLAlchemy 对象定义、关系映射、级联删除、索引和默认值。
- **路由编排层 (`api/routers/custom_learning.py`)**：定义 Pydantic 请求体，校验 `get_current_user`，执行 owner 隔离，组装计划生成、节点内容、测验评分、掌握度刷新和计划调整。
- **生成服务层 (`services/custom_learning_agents.py`)**：当前是规则版多 Agent 团队，包含目标分析、课程结构规划、节点内容设计、资源推荐和蓝图质量校验；后续可替换为 LLM/SSE，但响应 schema 应保持稳定。
- **前端契约层 (`api/custom-learning.ts`)**：负责把页面动作映射到 `/api/v1/custom-learning/*`，并显式区分列表、工作台、节点详情、内容富集和测验评分。

### 12.1 与现有模块关系

- 复用 `backend/application/explore/service.py` 的资源、习题和详解能力，避免新建一套重复 RAG。
- 复用 `LearningStyleProfile`，不新增学习风格表。
- 计划生成服务可调用现有 Agent Runtime 或轻量 LLM helper，但输出必须落到 Pydantic schema。
- 自定义学习接口使用 `/api/v1`，Explore 仍保持 `/api/v2/explore`；路由层负责适配，不要求前端直接组合多个后端能力。

### 12.2 错误处理

| 场景 | HTTP 状态 | 返回信息 |
| --- | --- | --- |
| 未登录 | 401 | `请先登录后使用自定义学习` |
| 访问他人计划 | 404 | `学习计划不存在` |
| 计划草稿结构非法 | 422 | `计划结构校验失败` |
| 知识库为空或不可访问 | 400 / 404 | `请选择可访问的知识库` |
| 节点未开放却开始学习 | 409 | `前置节点尚未完成` |
| LLM 生成失败 | 502 | `计划生成失败，请稍后重试` |
| SSE 中途失败 | event: `planning.failed` | 返回可展示错误和 retry token |

## 13. Agent 提示词约束

计划生成和调整时应遵循：

- 不编造不存在的知识库资源，资源必须带来源引用或标记为“建议补充”。
- 计划节点必须有可验证检查点。
- 每个节点只聚焦一个主要学习目标。
- 学习风格只改变学习任务和表达方式，不降低知识要求。
- 对用户基础薄弱时，优先插入前置节点，不直接删除核心内容。
- 输出 JSON 必须符合后端 schema，文案字段再由前端渲染。

## 14. 验收标准

### 14.1 功能验收

- 用户可以创建一个自定义学习计划，并在刷新后继续查看。
- 计划详情页展示真实节点、状态、掌握度和资源。
- 用户完成节点练习后，节点掌握度更新。
- 节点达标后，下一个节点自动开放。
- 练习不达标时，系统生成补充解释或追加练习。
- 开启学习风格应用后，新计划的节点任务能体现相应偏好。
- 探索式学习资源和练习结果能回写到学习节点。

### 14.2 技术验收

- 后端接口校验用户权限，不能访问他人计划。
- 数据库迁移包含四张新增表。
- 自定义学习 API 有单元测试覆盖计划创建、节点完成、测验回写和权限隔离。
- 前端 TypeScript 检查通过。
- 移动端 `/m/custom-learning` 和 `/m/custom-learning/path/:planId` 可用。
- 已有探索式学习和学习风格测评功能不回归。

### 14.3 测试用例建议

后端单元测试：

- 用户 A 不能读取用户 B 的 plan/node/attempt。
- 从模板创建计划后，第一个节点为 `open`，其他节点按规则 `locked`。
- 未激活的 `draft` 计划不能开始节点学习。
- quiz 提交后写入 attempt，并更新节点 mastery。
- mastery 达标后自动开放下一个节点。
- 连续低分触发 adjustment 建议，但不自动改计划，除非用户接受。
- 学习风格未开启时，生成请求不注入 style snapshot。

前端测试：

- 无计划空态显示模板和新建入口。
- SSE 生成计划期间显示阶段进度。
- 计划详情页能从轻量节点列表进入节点详情。
- 移动端底部 Sheet 不遮挡提交按钮。
- 练习提交后节点状态和掌握度刷新。

## 15. 分期实施

### 第一期：计划持久化和最小闭环

目标：把当前静态自定义学习页变成可保存、可继续、可完成节点的真实功能。

- 后端：
  - 新增四张表和迁移脚本。
  - 实现计划列表、详情、节点列表、节点详情接口。
  - 实现从模板创建计划。
  - 实现计划激活、节点开始、节点完成。
  - 实现基础 mastery 和 progress 计算。
- 前端：
  - 新建 `frontend/src/api/custom-learning.ts`。
  - `/custom-learning` 接入真实计划列表。
  - `/custom-learning/path/:planId` 接入真实节点列表。
  - 保留当前静态计划作为模板入口。
  - 完成未登录、空计划、草稿、进行中四类状态。
- 验收：
  - 用户能从模板创建计划，刷新后仍存在。
  - 完成第一个节点后第二个节点开放。
  - 不能访问其他用户计划。

### 第二期：生成式计划和探索式学习回写

目标：让节点内容从固定模板升级为可根据知识库和目标生成。

- 后端：
  - 实现 `plans/generate` SSE。
  - 接入 Explore resources、detail、quiz。
  - 固化 `explanation` 和 `quiz_questions`。
  - 实现 quiz 提交、评分和 diagnosis 写入。
  - 完善 mastery 计算权重。
- 前端：
  - 新建计划表单接入生成接口。
  - 节点面板增加学习、资源、练习、复盘 Tab。
  - 练习提交后展示得分、薄弱点和下一步建议。
- 验收：
  - 能基于用户目标生成草稿计划。
  - 节点详解二次访问内容一致。
  - 练习结果能影响 mastery 和节点状态。

### 第三期：学习风格和自适应调整

目标：形成真正个性化和可调整的闭环。

- 后端：
  - 计划生成读取 `LearningStyleProfile`。
  - 保存 `style_snapshot`。
  - 实现低分、慢进度和复习周期触发调整建议。
  - 实现 adjustment 接受后插入补救节点。
  - 生成周报和复盘建议。
- 前端：
  - 学习风格结果页增加“生成自定义计划”入口。
  - 计划页展示调整建议卡片。
  - 支持接受或忽略调整。
- 验收：
  - 开启学习风格后，计划任务形态有可见差异。
  - 连续低分能生成补救节点。
  - 用户拒绝调整时原计划不变。

## 16. 推荐优先级

最建议先做第一期，因为它能把“自定义学习”从静态页面变成真实产品功能；第二期再把探索式学习接进节点；第三期最后做个性化和自适应，否则容易一开始就把功能做散。

优先任务清单：

1. 建表：`learning_plans`、`learning_plan_nodes`、`learning_node_attempts`、`learning_plan_adjustments`。
2. 做计划列表和详情接口。
3. 把现有前端静态计划迁移为模板数据。
4. 做“从模板创建我的计划”。
5. 做节点完成、掌握度和下个节点开放。
6. 接入探索式学习的资源和习题。
7. 接入学习风格画像生成个性化任务。

## 17. 当前实现状态

截至当前实现，已完成第一期闭环骨架，并补入第二期/第三期的规则版生成、练习检测、学习风格映射和反馈调节入口：

- 已新增自定义学习数据库表：`learning_plans`、`learning_plan_nodes`、`learning_node_attempts`、`learning_plan_adjustments`。
- 已注册 `/api/v1/custom-learning` 后端接口。
- 已支持从前端模板保存个人计划。
- 已支持按目标生成规则版学习计划：`POST /plans/generate`。
- 已支持计划列表、计划详情、计划更新、计划启用、归档、节点列表、节点详情。
- 已支持开始节点、完成节点、更新掌握度、自动开放下一个节点。
- 已支持节点详情规则版详解固化：`GET /nodes/{node_id}` 首次访问开放节点时写入 `explanation` 并返回 `content_status`。
- 已支持节点练习生成与规则评分：`POST /nodes/{node_id}/quiz`、`POST /nodes/{node_id}/quiz/grade`。
- 已支持低分反馈调节：节点进入 `review` 状态，写入 `learning_plan_adjustments`，并在节点数未满时自动插入复习节点。
- 已支持计划级调整和复盘接口：`POST /plans/{plan_id}/adjust`、`GET /plans/{plan_id}/review`。
- 已支持学习风格画像映射到生成计划任务形态：活跃/沉思、感知/直觉、视觉/言语、序列/综合会影响节点任务和详解学习建议。
- 前端 `/custom-learning` 已接入“我的学习计划”和“按目标生成计划”。
- 前端 `/custom-learning/path/:planId` 已接入真实计划、节点完成、节点详解、探索资源、节点练习、计划复盘和复习调节提示。
- MySQL 已初始化并确认新增四张表存在。

### 17.1 完整性检验结果

| 方案项 | 当前状态 | 说明 |
| --- | --- | --- |
| 建表与模型 | 已完成 | 四张自定义学习表已加入 ORM 和初始化脚本 |
| 用户隔离 | 已完成 | plan/node 查询都按 `user_id` 校验 |
| 模板创建计划 | 已完成 | 前端模板可保存为个人计划 |
| 目标生成计划 | 已完成规则版 | 接口稳定，后续可替换为 LLM/SSE |
| 学习风格接入 | 已完成规则版 | 保存 `style_snapshot`，并影响任务与详解建议 |
| 节点开始/完成 | 已完成 | 支持状态流转、掌握度和下个节点开放 |
| 节点详解固化 | 已完成规则版 | 首次访问节点详情生成并写入 `explanation` |
| 探索资源回写 | 已完成基础版 | 节点探索写入 `resources` 与 `explore_cache` |
| 节点练习与评分 | 已完成 | 优先复用 Explore 习题，失败回退规则题 |
| 低分调节 | 已完成 | 低分进入 `review`，写 adjustment，并插入复习节点 |
| 计划复盘 | 已完成基础版 | 汇总复习节点、薄弱点、最近 attempts 和 adjustments |
| 前端路径页 | 已完成基础版 | 展示真实路径、详解、资源、练习、复盘和调节提示 |
| 自动化验证 | 已完成基础版 | 已有专项后端测试和前端 TypeScript 构建验证 |

仍待后续增强，均为模型/调度/体验增强，不阻塞当前闭环主流程：

- 将规则版计划生成替换为 LLM/SSE 生成。
- 将节点详解替换为 Explore/RAG 驱动的证据型长解释，并保留当前 `explanation` 缓存字段。
- 增加复习周期调度、长期遗忘曲线和计划调整建议确认机制。
