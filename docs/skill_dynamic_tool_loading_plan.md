# Skill 动态工具加载规范

> 最后同步：2026-06-08
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
        ▼
ProfiledGraphBuilder
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

## 6. 声明工具注入与校验

`ToolAssemblyPipeline` 会：

1. 从已选 Skill 收集 `tool_names`；
2. 尝试通过 `ToolRegistry` 加载声明工具；
3. 对最终工具集进行声明校验；
4. 缺失时记录 warning，但不直接让请求失败。

条件性缺失是合法情况，例如：

- `use_search=False` 时缺少 `web_search`；
- 没有知识库 ID 时缺少 `multi_kb_search`；
- 没有会话文件索引时缺少 `chat_file_search`；
- ToolPolicy 明确禁止某工具。

## 7. 子智能体规则

`task_tool.py` 是当前本地子智能体执行后端。新增协同能力时：

- coordinator 负责拆解、分派、轮次和合并；
- `task_tool.py` 负责执行单个子任务；
- 子智能体不得再次开启无限递归子智能体；
- 工具集仍由 ToolPolicy 和 SkillManifest 计算；
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
python \
  -m pytest backend/tests/test_agent_platform_standardization.py -q
```
