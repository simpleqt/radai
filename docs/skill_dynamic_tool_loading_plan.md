# Skill 动态工具加载规范

> 最后同步：2026-07-09
> 状态：已实现
> 本文描述当前平台实现。旧 `NEFullFeatureAgent._get_tools()`、`custom/agents/support/tool_assembly.py` 和全局 `SkillToolRegistry` 方案已废弃。

## 1. 目标

Skill 不只是提示词片段，还可以声明：

- 依赖工具；
- 允许和推荐的 workflow；
- 知识库要求；
- guardrail 要求。

工具能否进入当前智能体，由 `ToolRegistry + ToolPolicy + SkillManifest + RuntimeContext` 共同决定，Skill 声明本身不能绕过工具策略。

## 2. 当前链路

```text
SKILL.md frontmatter
        │
        ├── core/skills/parser.py
        │     └── Skill.tool_names
        │
        └── application/skills/manifest_loader.py
              └── SkillManifest
                    │
                    ▼
AgentRequest.selected_skills
        │
        ├── AgentService / SkillRuntimeResolver
        │     └── 校验托管 Skill 发布状态、智能体绑定、用户 use 权限
        │
        ▼
ProfiledGraphBuilder / RuntimeToolInvoker
        │
        ▼
ToolAssemblyPipeline
        ├── ToolPolicy 计算允许工具
        ├── ToolRegistry 校验 provider/domain/workflow/skill
        ├── 注入请求级工具
        ├── 注入 Skill 控制面工具
        └── 校验 Skill 声明与最终工具集
```

主要代码：

| 职责 | 当前入口 |
| :--- | :--- |
| Skill 文件解析 | `backend/core/skills/parser.py` |
| Skill 运行时类型 | `backend/core/skills/types.py` |
| 平台 Manifest | `backend/domain/skills/manifest.py` |
| Manifest 加载 | `backend/application/skills/manifest_loader.py` |
| 工具元数据 | `backend/config/tools/registry.yaml` |
| 工具策略 | `backend/config/tools/policies.yaml` |
| 工具实例化 | `backend/application/tools/provider_loader.py` |
| 工具组装 | `backend/application/tools/tool_assembly_pipeline.py` |
| 运行时托管 Skill 解析 | `backend/application/skills/runtime_resolver.py` |
| 子智能体执行后端 | `backend/core/tools/task_tool.py` |

## 3. SKILL.md 声明

推荐格式：

```yaml
---
name: example-skill
description: Example skill description
tools:
  - multi_kb_search
  - web_search
workflows:
  allowed:
    - full
    - plan_solve
    - multi_agent_parallel
  preferred: plan_solve
kb_policy:
  required: true
  binding_mode: agent_or_user_selected
guardrails:
  - domain_scope
---
```

`backend/core/skills/parser.py` 只负责把本地 `SKILL.md` 的 `name`、`description`、`owner_id`、`license` 和 `tools` 解析为运行时 Skill；`workflows`、`kb_policy`、`guardrails` 等平台扩展由 `backend/application/skills/manifest_loader.py` 读取为 `SkillManifest`，并参与 workflow 路由与工具准入。

`tools` 支持字符串列表，也兼容只读取 `name` 的对象写法：

```yaml
tools:
  - name: web_search
```

## 4. 工具准入规则

工具进入最终工具集必须同时满足：

1. ToolPolicy 允许该工具；
2. ToolSpec 支持当前 domain；
3. ToolSpec 支持当前 workflow 或其能力族；
4. ToolSpec 的 required skills 与当前 selected skills 匹配；
5. provider 可以成功加载；
6. 请求级条件允许，例如 `use_search=True`、存在 `kb_ids` 或存在会话文件索引。

协同 workflow `plan_solve`、`multi_agent_parallel`、`multi_agent_alternating` 继承 `full` 的工具能力，但仍受同一 ToolPolicy 约束。

## 5. Skill 控制面工具

选中 Skill 后，平台按请求注入：

- `read_local_skill_file`
- `read_skill_reference`
- `list_available_skills`

这些工具只允许读取当前请求作用域内的 Skill。模型必须先加载需要的方法论文件，再使用受 Skill 约束的业务工具。

当前请求作用域来自两个来源：文件型 public/custom Skill 由 `ScopedSkillsProvider` 按用户与选择列表过滤；托管 Skill 由 `SkillRuntimeResolver` 先按数据库绑定和授权解析为 `authorized_skill_names`，再交给 provider 暴露。`read_skill_reference` 会使用 `resolve().relative_to(skill_dir)` 防止路径穿越，并支持 `references/index.md` 目录兜底。

## 6. 声明工具注入与校验

`ToolAssemblyPipeline` 会：

1. 从已选 Skill 收集 `tool_names`；
2. 尝试通过 `ToolRegistry` 加载声明工具；
3. 对最终工具集进行声明校验；
4. 对缺失工具进行条件判断；不可接受的缺失会让请求失败。

条件性缺失是合法情况，例如：

- `use_search=False` 时缺少 `web_search`；
- 没有知识库 ID 时缺少 `multi_kb_search`；
- ToolSpec 不支持当前 domain/workflow/skill。

以下情况会直接失败：

- Skill 声明 `chat_file_search`，但当前 `thread_id` 没有可用会话文件索引；
- ToolPolicy 期望的业务工具在最终工具集中缺失，且不属于条件性缺失；
- 最终工具集中出现 ToolPolicy 未授权的业务工具；
- Skill 声明的工具既不属于条件性缺失，也不属于当前 domain/workflow 的合法过滤结果。

核电子学与 CSH 领域还启用了知识库技能加载约束：在非 lightweight 流程中，`multi_kb_search` 会被拦截，直到模型先调用对应 Skill 的 `read_local_skill_file()`；groundwater 领域额外通过 `GroundwaterProjectWorkflowMiddleware` 要求读取项目 Skill 并调用 `groundwater_project_bootstrap()` 后，才允许自由 SQL 类项目工具。

## 7. 子智能体规则

`task_tool.py` 是当前本地子智能体执行后端。新增协同能力时：

- coordinator 负责拆解、分派、轮次和合并；
- `task_tool.py` 负责执行单个子任务；
- 子智能体不得再次开启无限递归子智能体；
- 工具集仍由 ToolPolicy 和 SkillManifest 计算；
- 子智能体必须携带 domain、workflow、tool_policy 和 `ToolRuntimeContext`，否则拒绝装配；
- 主智能体与子智能体均不得使用 `backend/custom/agents/*` 作为扩展入口。

## 8. 新增 Skill 流程

1. 在 `backend/skills/public/<name>/SKILL.md` 或 `backend/skills/custom/<name>/SKILL.md` 创建声明。
2. 在 `backend/config/tools/registry.yaml` 注册所需工具 provider。
3. 在对应 ToolPolicy 中允许工具。
4. 确认 Skill 的 allowed/preferred workflow 已注册。
5. 增加 Manifest、ToolPolicy 和工具组装契约测试。

## 9. 验证要求

至少覆盖：

- YAML frontmatter list/dict 解析；
- 未知工具声明启动失败；
- domain/workflow/skill 工具准入；
- `use_search=False` 条件禁用；
- 协同 workflow 复用 full 工具能力；
- 子智能体工具集不越权；
- custom Skill 覆盖 public Skill 时的合并规则。

当前标准化测试入口：

```bash
/home/inspur/myenv/langchian_project/venv314/bin/python \
  -m pytest backend/tests/test_agent_platform_standardization.py -q
```
