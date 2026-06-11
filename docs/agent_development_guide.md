# 智能体开放框架开发指南与扩展规范

> 适用仓库：`RadAI`  
> 基线日期：2026-06-10  
> 适用范围：新增领域智能体、Prompt、Skill、Tool、Workflow、MCP 服务及对应前端接入

## 1. 文档目标

本指南规定后续智能体开发的标准流程、扩展边界、配置格式、代码约束、测试要求和上线验收标准。

平台当前采用以下运行链路：

```text
HTTP Request
  -> AgentRequest
  -> AgentService
  -> AgentProfile
  -> DomainContextResolver
  -> ProfileRuntimePolicy
  -> AgentRuntimeContext
  -> WorkflowRegistry
  -> AgentWorkflow
  -> ProfiledAgentExecutor
  -> ToolRuntimeContext
  -> AgentEvent
  -> SSEEncoder
  -> Frontend
```

新增智能体时，原则上不得复制 API、Agent 主类或 SSE 实现。领域差异应优先通过以下扩展点表达：

| 扩展内容 | 唯一入口 |
| :--- | :--- |
| 智能体身份、默认能力 | `backend/config/agents/<agent_code>.yaml` |
| 请求级领域上下文 | Profile 的 `domain_context_provider` |
| KB 与运行时权限 | `backend/application/policies/` |
| Prompt 模板 | `backend/application/agents/prompt_templates/` |
| Prompt 注册 | `backend/config/prompts/registry.yaml` |
| Skill | `backend/skills/public/` 或 `backend/skills/custom/` |
| Tool provider | `backend/application/tools/providers/` |
| Tool 元数据 | `backend/config/tools/registry.yaml` |
| Tool 准入策略 | `backend/config/tools/policies.yaml` |
| Workflow provider | `backend/application/agent_workflows/` |
| Workflow 注册 | `backend/config/workflows/registry.yaml` |
| MCP 服务绑定 | `backend/config/mcp/registry.yaml` |
| 标准运行入口 | `POST /api/v2/agent-runtime/query` |

## 2. 核心原则

### 2.1 配置是智能体身份的事实源

`backend/config/agents/*.yaml` 是 Agent Profile 的唯一配置来源。缺少对应 YAML 时，`AgentProfileLoader` 会直接报错，不存在代码默认回退。

禁止：

- 在 API 中写 `if agent_code == "xxx"`；
- 在执行器中复制一个新的领域 Agent 类；
- 在业务代码中硬编码默认知识库、Skill 或 Tool；
- 为新智能体复制 `/query`、`/info`、任务状态或 SSE 路由。

### 2.2 API 不感知领域实现

API 层只负责：

- 参数校验；
- 身份认证；
- 构造 `AgentRequest`；
- 调用 `AgentService`；
- 将 `AgentEvent` 编码为 SSE。

API 层不得：

- import 具体 Prompt 模块；
- import 具体工具 provider；
- 构造具体 Workflow；
- 拼装系统 Prompt；
- 根据行业名称选择执行器。

### 2.3 Workflow、Tool、Prompt 必须注册

所有 provider 路径统一使用：

```text
python.module.path:callable_name
```

例如：

```yaml
provider: backend.application.agents.prompt_templates.geology:build_system_prompt
```

禁止通过新增 `if/elif` 分支完成 provider 分发。

### 2.4 事件协议与传输协议分离

Workflow 和执行器只产生 `AgentEvent`。HTTP SSE 编码由：

```text
backend/application/event_stream/sse_encoder.py
```

统一处理。

禁止在 Workflow 或 Tool 中直接输出：

```text
data: {...}
```

禁止为新智能体创建私有 SSE 事件格式。

### 2.5 请求元数据不是授权结果

`AgentRequest.metadata` 只承载调用方提交的上下文线索，不能直接作为工具权限使用。

需要项目、租户、数据集或其他资源范围时，必须通过以下链路完成：

```text
request.metadata
  -> domain_context_provider
  -> 已授权 domain_context
  -> ProfileRuntimePolicy
  -> AgentRuntimeContext.permissions
  -> ToolRuntimeContext
  -> Tool Guardrail
```

禁止 Tool 仅凭 `request_metadata` 中的资源 ID 访问数据。Tool 必须使用已经解析和授权的
`domain_context`、`permissions` 与当前 `user_id`。

## 3. 命名标准

### 3.1 Agent code

`agent_code` 必须：

- 使用小写 ASCII；
- 仅包含字母、数字、下划线；
- 简短且全局唯一；
- 与 Profile 文件名一致。

示例：

```text
geology
chemical_safety
medical_qa
```

不推荐：

```text
GeologyAgent
地质智能体
geology-full-feature
```

### 3.2 Domain

`domain` 用于 ToolSpec 的领域准入，应使用稳定的业务领域标识：

```text
engineering-geology
chemical-safety
clinical-medicine
```

修改已上线的 `domain` 会影响工具匹配，因此应视为接口变更。

### 3.3 Profile、Policy 与 Skill 名称

推荐：

```text
Profile:       geology
Prompt:        geology
Tool policy:   geology_default
Domain skill:  engineering-geology
UI profile:    geology
```

Tool 名称必须与 LangChain `BaseTool.name` 一致，否则策略审计可能出现“配置存在但运行时工具缺失”。

## 4. 最小新增流程

新增一个只复用现有 Workflow 和 Tool 的领域智能体，至少需要完成：

1. 创建 Agent Profile；
2. 创建并注册 Prompt；
3. 创建领域 Skill；
4. 创建 Tool Policy；
5. 扩展所需 ToolSpec 的 `domains`；
6. 如访问请求级业务资源，创建 Domain Context Provider 和运行时权限策略；
7. 增加契约测试；
8. 验证标准 API；
9. 完成前端 agent code 与 metadata 接入。

以下以 `geology` 为示例。

## 5. 创建 Agent Profile

创建：

```text
backend/config/agents/geology.yaml
```

推荐模板：

```yaml
code: geology
name: 工程地质智能助手
domain: engineering-geology
description: 面向工程地质勘察、边坡稳定与地质灾害分析的专业智能体

default_model: null

default_kb_ids:
  - replace-with-real-kb-id

default_skills:
  - engineering-geology
  - general-table-analysis

default_workflow: lightweight

allowed_workflows:
  - lightweight
  - full
  - deep_research
  - plan_solve
  - multi_agent_parallel
  - multi_agent_alternating

prompt_profile: geology
tool_policy: geology_default
kb_policy: bound_or_selected
guardrail_policy: default
ui_profile: geology
# 仅在需要请求级项目/租户/资源授权时配置：
# domain_context_provider: backend.application.geology.runtime_context:resolve_geology_context
```

### 5.1 字段标准

| 字段 | 必填 | 当前作用 |
| :--- | :---: | :--- |
| `code` | 是 | Profile 主键和 API `agent_code` |
| `name` | 是 | 前端展示名称 |
| `domain` | 是 | ToolSpec 领域匹配 |
| `description` | 建议 | Profile 列表和说明 |
| `default_model` | 否 | 未指定请求模型时使用 |
| `default_kb_ids` | 否 | 请求未选择 KB 时的默认 KB |
| `default_skills` | 否 | 请求未提交非空 Skill 列表时，由 `AgentService` 作为运行时默认值 |
| `default_workflow` | 是 | 请求未指定 Workflow 时使用 |
| `allowed_workflows` | 是 | Profile 可执行 Workflow 白名单 |
| `prompt_profile` | 是 | Prompt Registry key |
| `tool_policy` | 是 | Tool Policy key |
| `kb_policy` | 是 | `KnowledgeBaseAccessPolicy` 的运行时授权模式 |
| `guardrail_policy` | 是 | `ProfileRuntimePolicy` 和 Tool Guardrail 使用的策略标识 |
| `ui_profile` | 否 | UI 及存量执行器诊断行为标识 |
| `domain_context_provider` | 否 | 请求级领域上下文解析和资源授权 provider |

### 5.2 Default Skill 的注意事项

当前服务层的有效 Skill 计算规则是：

```text
request.selected_skills 非空
  ? request.selected_skills
  : profile.default_skills
```

因此：

- 请求未提交 Skill 或提交空列表时，会使用 `default_skills`；
- 当前协议不支持用空列表显式关闭 Profile 默认 Skill；
- 用户显式选择 Skill 时，不会与默认 Skill 合并，而是使用用户提交列表；
- `WorkflowRegistry` 的 Skill preferred/full 自动选择依据仍是请求中显式提交的 Skill；
- `lightweight` Workflow 会主动清空 Skill，以保持轻量执行。

前端仍应调用 `/profiles/{agent_code}/info` 初始化选中状态，确保 UI 展示与实际运行时一致。

### 5.3 知识库绑定

运行时先由 `KnowledgeBaseAccessPolicy` 校验请求和 Profile，再生成允许使用的 KB：

```text
request.kb_ids 非空
  ? 校验请求 KB
  : 校验 profile.default_kb_ids
```

当前支持的 `kb_policy`：

| 值 | 行为 |
| :--- | :--- |
| `bound_or_selected` / `selected_owned` / `user_selected` | 允许 Profile 绑定 KB、平台默认 KB或当前用户拥有的 KB |
| `bound_only` / `profile_bound` | 仅允许 `default_kb_ids` 中的 KB |
| `disabled` / `none` | 禁止选择和使用 KB |

所有候选 KB 必须存在且状态为 `active`。非 Profile 绑定 KB 还必须是平台默认 KB 或属于当前用户。

AgentInfo 会尝试读取数据库中的 Agent-KB 绑定；读取失败或不存在绑定时回退到 Profile。
但当前运行时“Profile 绑定”判定仍以 `profile.default_kb_ids` 为准，数据库动态绑定不是独立的运行时授权来源。

对于新智能体：

- Profile 中必须配置存在且状态为 `active` 的真实 KB ID；
- 如需要运营侧动态调整，应在数据库中使用新 `agent_code` 建立绑定；
- 数据库绑定超出 `default_kb_ids` 时，还必须满足用户所有权或平台默认库规则；
- `backend/db/init_agent_bindings.py` 仍是硬编码初始化脚本，不要把新增智能体依赖于该脚本；
- 新增通用初始化能力时应改造成配置驱动，不得继续扩充硬编码列表。

### 5.4 请求级领域上下文

当 Tool 需要访问项目、租户或用户绑定资源时，在 Profile 中声明：

```yaml
domain_context_provider: backend.application.geology.runtime_context:resolve_geology_context
```

Provider 签名：

```python
from typing import Any


def resolve_geology_context(
    *,
    user_id: str,
    request_metadata: dict[str, Any],
) -> dict[str, Any]:
    project_id = str(request_metadata.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("缺少 metadata.project_id")

    project = project_service.resolve_for_user(user_id, project_id)
    return {
        "project_id": project.id,
        "project_name": project.name,
        "read_only": True,
    }
```

要求：

- Provider 必须根据 `user_id` 校验资源归属或绑定关系；
- 返回值必须是字典，且只包含后续策略和 Tool 所需的最小上下文；
- 不得直接信任 metadata 中的名称、路径、权限或只读标记；
- 不得把服务器绝对路径、凭证或数据库连接对象放入上下文；
- Provider 在每个请求解析一次，Tool 复用解析后的 `domain_context`；
- 无需请求级资源范围的智能体应省略该字段，Resolver 会返回空字典。

当前 `ProfileRuntimePolicy` 已实现 `ne_domain_scope`、`csh_domain_scope`、
`groundwater_project_scope` 和 `default`。新增具有特殊权限语义的
`guardrail_policy` 时，必须同步扩展策略求值、Guardrail 和测试，不能只在 YAML 中新增名称。

## 6. 创建 Prompt Provider

创建：

```text
backend/application/agents/prompt_templates/geology.py
```

Provider 必须兼容以下签名：

```python
def build_system_prompt(
    subagent_enabled: bool = False,
    use_search: bool = True,
    available_skills: set[str] | None = None,
    task_type: str | None = None,
    *,
    lightweight_mode: bool = False,
    lightweight_has_kb: bool = False,
) -> str:
    ...
```

最小实现：

```python
from backend.core.prompts.system_template import apply_prompt_template


def build_system_prompt(
    subagent_enabled: bool = False,
    use_search: bool = True,
    available_skills: set[str] | None = None,
    task_type: str | None = None,
    *,
    lightweight_mode: bool = False,
    lightweight_has_kb: bool = False,
) -> str:
    if lightweight_mode:
        kb_note = (
            "已绑定工程地质知识库，优先使用检索上下文。"
            if lightweight_has_kb
            else "当前无知识库上下文，明确说明不确定性。"
        )
        return f"""
你是工程地质智能助手。
直接回答问题，不编造来源。
{kb_note}
"""

    base = apply_prompt_template(
        agent_name="工程地质智能助手",
        subagent_enabled=subagent_enabled,
        max_concurrent_subagents=3,
        available_skills=available_skills,
    )
    search_policy = (
        "允许按工具策略使用检索工具。"
        if use_search
        else "禁止声称执行了未启用的联网检索。"
    )
    return (
        base
        + "\n<domain_policy>\n"
        + "- 优先基于工程地质知识库证据回答。\n"
        + "- 数值、单位、标准编号必须保持原始精度。\n"
        + f"- {search_policy}\n"
        + f"- task_type: {task_type or 'standard'}\n"
        + "</domain_policy>\n"
    )
```

### 6.1 注册 Prompt

修改：

```text
backend/config/prompts/registry.yaml
```

添加：

```yaml
prompt_profiles:
  geology:
    provider: backend.application.agents.prompt_templates.geology:build_system_prompt
    aliases:
      - geology
      - engineering-geology
```

### 6.2 Prompt 标准

Prompt 必须：

- 同时支持 lightweight 和 full 模式；
- 尊重 `use_search`；
- 不声明运行时不存在的工具；
- 不硬编码过期年份；
- 不在 Prompt 中包含 API Key、内部地址或数据库凭证；
- 对引用、图片、数值和不确定性给出明确规则；
- 将领域规则与通用 Tool/Skill 机制分开。

Prompt 不得：

- 直接拼接用户未经处理的敏感元数据；
- 要求模型调用未通过 ToolPolicy 注册的工具；
- 将“选择了 Skill”等同于“Skill 已加载”；
- 使用 Prompt 代替后端权限校验；
- 在多个 Agent 类中复制同一模板。

### 6.3 Skill enforcement 限制

当前 `PromptBuilder._build_skill_enforcement()` 对部分内置 Skill 仍有显式规则。新增 Skill 的一般说明可通过 `SKILL.md` 注入，但若需要“调用某工具前必须先加载 Skill”这类强制行为，应：

1. 优先实现通用、声明式 enforcement；
2. 增加契约测试；
3. 不继续无限扩充领域 `if skill_name == ...` 分支。

## 7. 创建领域 Skill

公共 Skill 放置于：

```text
backend/skills/public/engineering-geology/SKILL.md
```

项目定制覆盖放置于：

```text
backend/skills/custom/engineering-geology/SKILL.md
```

同名情况下，`custom` 内容覆盖 `public`，缺失的能力声明会从 public 合并。

推荐 front matter：

```yaml
---
name: engineering-geology
description: 工程地质勘察、边坡稳定、岩土参数和地质灾害分析技能
domain: engineering-geology
tools:
  - multi_kb_search
  - web_search
workflows:
  allowed:
    - full
    - deep_research
  preferred: full
kb_policy:
  required: true
  binding_mode: agent_or_user_selected
guardrails:
  - citation_required
---
```

### 7.1 Skill 字段标准

| 字段 | 说明 |
| :--- | :--- |
| `name` | 全局唯一 Skill 名称 |
| `description` | 触发场景和能力摘要 |
| `domain` | 所属领域 |
| `tools` | Skill 声明会使用的 Tool 名称 |
| `workflows.allowed` | 允许的 Workflow |
| `workflows.preferred` | 用户选择 Skill 且未指定 Workflow 时的优先 Workflow |
| `kb_policy.required` | 是否要求知识库 |
| `kb_policy.binding_mode` | KB 绑定模式元数据 |
| `guardrails` | Skill 声明的 Guardrail 元数据 |

### 7.2 Skill 内容标准

`SKILL.md` 应包含：

- 适用场景；
- 不适用场景；
- 工具选择顺序；
- 关键参数和单位规范；
- 证据与引用要求；
- 输出结构；
- 失败和信息不足时的处理；
- 禁止行为。

Skill 不应重复完整系统 Prompt。它应描述可复用的任务方法和工具使用规范。

### 7.3 Skill 与 ToolPolicy 的双向一致性

Skill 声明的每个 Tool 必须同时满足：

1. 存在于 `tools/registry.yaml`；
2. 能被 Profile 的 ToolPolicy 选中；
3. ToolSpec 支持该 Skill 的 domain；
4. ToolSpec 支持目标 Workflow；
5. `required_skills` 与实际选择条件一致。

否则 Skill 虽能加载，工具仍不会进入执行器。

## 8. 注册和开发 Tool

### 8.1 创建 provider

文件位置：

```text
backend/application/tools/providers/geology_calculator.py
```

Provider 可以是：

- 一个 `BaseTool`；
- 一个返回 `BaseTool` 的零参数 callable；
- 一个返回 `list[BaseTool]` 的零参数 callable；
- 一个接收关键字参数 `runtime_context` 的请求级 callable。

示例：

```python
from langchain_core.tools import tool


@tool
def calculate_slope_factor_of_safety(
    cohesion_kpa: float,
    friction_angle_deg: float,
    unit_weight_kn_m3: float,
) -> str:
    """根据明确输入参数执行边坡稳定计算。"""
    ...


def get_geology_tools():
    return [calculate_slope_factor_of_safety]
```

访问请求级资源的 provider 必须使用 `ToolRuntimeContext`：

```python
from backend.domain.tools.runtime_context import ToolRuntimeContext


def get_project_geology_tools(
    *,
    runtime_context: ToolRuntimeContext | None = None,
):
    if runtime_context is None:
        raise ValueError("缺少工具运行时上下文")

    project_id = str(runtime_context.domain_context.get("project_id") or "")
    authorized = bool(
        runtime_context.permissions.get("domain_context", {}).get("authorized")
    )
    if not project_id or not authorized:
        raise PermissionError("项目上下文未授权")
    ...
```

`ToolRuntimeContext` 当前包含：

- `request_id`、`user_id`、`agent_code`、`domain`、`thread_id`；
- `permissions`；
- `time_context`；
- 原始 `request_metadata`；
- 已解析授权的 `domain_context`。

Tool 只能把 `request_metadata` 当作业务输入线索，资源访问范围必须以
`domain_context` 和 `permissions` 为准。

Provider 不得：

- 在 import 阶段建立网络连接；
- 在 import 阶段访问数据库或创建后台线程；
- 绕过用户、会话或 KB 权限；
- 根据原始 metadata 自行扩大项目、租户或文件访问范围；
- 返回与注册名无关且未审计的隐藏工具；
- 将异常静默转换成虚假成功结果。

### 8.2 注册 ToolSpec

修改：

```text
backend/config/tools/registry.yaml
```

```yaml
tools:
  calculate_slope_factor_of_safety:
    provider: backend.application.tools.providers.geology_calculator:get_geology_tools
    description: 使用用户提供的工程参数执行边坡稳定计算
    domains:
      - engineering-geology
    workflows:
      - full
      - deep_research
    required_skills:
      - engineering-geology
    risk_level: safe
    enabled_by_default: false
```

### 8.3 ToolSpec 字段标准

| 字段 | 标准 |
| :--- | :--- |
| `provider` | 必须可 import |
| `description` | 描述输入、输出和副作用 |
| `domains` | 空表示不限领域；推荐显式配置 |
| `workflows` | 使用 capability 匹配，不要只考虑字面名称 |
| `required_skills` | 非空时，选择其中任一 Skill 即可通过当前实现 |
| `risk_level` | 仅允许 `safe/network/write/sandbox` |
| `enabled_by_default` | 当前不替代 ToolPolicy 准入 |

注意：当前 `required_skills` 使用集合相交判断。如果一个工具要求多个 Skill 同时满足，现有模型无法表达，必须先扩展契约和测试，不能误用列表表达 AND 语义。

同一个 provider 可以返回多个 Tool，但 Registry 中每个 Tool 名称仍应有独立 ToolSpec。
materialize 后的 `BaseTool.name` 必须与当前 ToolSpec 和 Policy 引用保持一致。

### 8.4 配置 Tool Policy

修改：

```text
backend/config/tools/policies.yaml
```

```yaml
policies:
  geology_default:
    include:
      - multi_kb_search
      - chat_file_search
      - web_search
    include_when_skill_selected:
      engineering-geology:
        - calculate_slope_factor_of_safety
      academic-search:
        - academic_search
      deep-research:
        - save_deep_research_report
    exclude: []
```

ToolPolicy 是业务工具的第一层准入来源。`enabled_by_default` 不能绕过 Policy。
工具装配完成后，`RuntimePolicyGuardrailProvider` 还会按实际 allowlist、KB 权限和领域权限执行第二层校验。

### 8.5 扩展已有 Tool 的领域

如新智能体复用 `multi_kb_search`，需要把新 domain 加入该 ToolSpec：

```yaml
domains:
  - nuclear-electronics
  - csh-hydrogeology
  - engineering-geology
```

仅在 Policy 中 include 工具但不扩展 `domains`，工具仍会被过滤。

## 9. Workflow 开发标准

### 9.1 复用现有 Workflow

优先使用：

- `lightweight`：低开销问答，不加载 Skill 工具；
- `full`：完整模型、Skill、Tool、KB 能力；
- `deep_research`：计划、多分支证据收集、Writer 汇总和报告工件生成；
- `plan_solve`：计划、多分支求解和 Reviewer 汇总；
- `multi_agent_parallel`：并行领域分析、证据研究、风险审查和综合；
- `multi_agent_alternating`：分析、批评和定稿的交替协调。

不要仅因 Prompt 不同就创建新 Workflow。

当前协同 Workflow 全部在本次 SSE 请求中执行。旧版后台研究任务已经移除，
`/task/{task_id}/status|stream|cancel` 端点返回 HTTP 410，不得继续依赖旧任务协议。

### 9.2 新 Workflow 协议

Workflow 必须实现：

```python
class AgentWorkflow(Protocol):
    name: str

    async def run(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> AsyncIterator[AgentEvent]:
        ...
```

Provider factory 必须接受：

```python
def create_custom_workflow(
    *,
    name: str,
    checkpointer=None,
    runnable_config=None,
):
    ...
```

### 9.3 注册 Workflow

修改：

```text
backend/config/workflows/registry.yaml
```

```yaml
workflows:
  evidence_review:
    provider: backend.application.agent_workflows.factories:create_evidence_review_workflow
    aliases:
      - evidence-review
      - evidence_review
    capabilities:
      - evidence_review
      - full
    full_capability: true
    subagent_enabled: false
```

### 9.4 Capability 语义

ToolSpec 的 Workflow 匹配通过 `resolve_workflow_capabilities()` 完成。

如果新 Workflow 复用 full 工具能力，应把 `full` 加入 `capabilities`，并设置：

```yaml
full_capability: true
```

`subagent_enabled: true` 会让 `AgentService` 自动开启子智能体能力。

### 9.5 Workflow 事件要求

标准事件包括：

- `workflow.started`
- `workflow.step`
- `plan.created`
- `plan.updated`
- `coordination.unit.completed`
- `coordination.unit.failed`
- `coordination.completed`
- `message.delta`
- `reasoning.delta`
- `follow_up.created`
- `error`
- `done`

每条事件必须包含：

- `request_id`
- `thread_id`
- 单调递增的 `sequence`
- 字典类型 `payload`

错误路径必须最终产生 `error` 和 `done`，不得让 SSE 无结束标记。

## 10. MCP 开放标准

MCP 配置位于：

```text
backend/config/mcp/registry.yaml
```

示例：

```yaml
services:
  default:
    name: 工程地质 MCP 服务
    instructions: 通过平台 Agent Runtime 查询工程地质智能体
    agent_code: geology
    workflow: full
    user_id: mcp
    use_search: true
    thinking_enabled: false
    tool_name: query_geology_knowledge
    tool_description: 查询工程地质知识库并返回回答
```

要求：

- MCP 只通过 `application.mcp` 调用平台运行时；
- API 层不得再次硬编码 `agent_code`；
- `agent_code` 必须存在对应 Profile；
- `workflow` 必须在 Profile 白名单中；
- MCP 用户身份必须有明确权限边界；
- 对外开放前必须评估 network/write 工具风险。

当前默认只实例化 `services.default`。如果需要同一进程暴露多个 MCP 服务或多个 endpoint，需要先扩展服务选择和路由装配机制。

当前 MCP 配置不能声明任意领域 metadata。使用 `domain_context_provider` 且要求项目 ID
等请求上下文的 Profile，不能直接绑定到默认 MCP 服务；必须先扩展 MCP 输入协议，
并在服务端根据 MCP 身份解析授权资源，不能让调用方直接提交可越权的资源 ID。

## 11. 前端接入标准

标准客户端支持通过 `agentCode` 提交不同 Profile。当前已有：

```text
frontend/                                -> ne
frontend_csh_ai/ 默认聊天               -> csh
frontend_csh_ai/groundwater              -> groundwater
```

新增领域入口或通用智能体选择器时，必须：

1. 从路由、部署配置或 Profile API 获取 `agent_code`；
2. 调用 `/api/v2/agent-runtime/profiles/{agent_code}/info`；
3. 使用返回的模型、KB 和默认 Skill 初始化 UI；
4. 查询时显式发送 `agent_code`；
5. 如 Profile 使用 `domain_context_provider`，发送其约定的 metadata key；
6. 直接消费标准 `AgentEvent` envelope；
7. 不新增领域专属后端 query API。

标准请求：

```json
{
  "question": "分析某边坡失稳的主要控制因素",
  "agent_code": "geology",
  "thread_id": "optional-thread-id",
  "workflow": "full",
  "kb_ids": ["real-kb-id"],
  "selected_skills": ["engineering-geology"],
  "model": null,
  "use_search": true,
  "subagent_enabled": false,
  "thinking": true,
  "metadata": {}
}
```

`metadata` key 是领域入口与 `domain_context_provider` 的显式契约。前端不得发送服务器路径、
权限布尔值或可绕过后端授权的派生字段。

例如地下水项目入口提交：

```json
{
  "agent_code": "groundwater",
  "metadata": {
    "groundwater_project_id": "authorized-project-id"
  }
}
```

标准 SSE：

```text
data: {"type":"message.delta","payload":{"content":"..."},"request_id":"...","thread_id":"...","sequence":1,"created_at":"..."}

data: [DONE]
```

前端不得依赖旧的 `chunk/status/event` 私有协议。

## 12. 配置使用标准

配置已按领域拆分：

```python
settings.models
settings.rag
settings.storage
settings.auth
settings.server
```

新代码必须优先使用分组访问：

```python
settings.models.llm_provider
settings.storage.redis_host
settings.auth.cas_enabled
settings.server.server_port
```

旧的扁平访问仍兼容，但仅用于存量代码迁移：

```python
settings.redis_host
```

禁止：

- 在业务模块直接读取 `.env`；
- 新增重复配置字段；
- 把领域 Agent 配置加入全局 Settings；
- 在 Settings 中保存可由 Profile、Policy 或 Registry 表达的内容。

## 13. Guardrail 与权限标准

当前运行时已经执行以下权限链：

1. `DomainContextResolver` 根据 Profile provider 解析请求级领域上下文；
2. `KnowledgeBaseAccessPolicy` 校验 KB 存在性、状态、Profile 绑定和用户所有权；
3. `ProfileRuntimePolicy` 生成 `permissions` 与运行时 Guardrail 指令；
4. `RuntimePolicyGuardrailProvider` 按实际 Tool allowlist 和运行时权限拦截调用；
5. 请求级 Tool 再根据 `ToolRuntimeContext` 限定数据访问范围。

任何高风险能力必须在代码层落实：

- 用户身份和资源所有权校验；
- KB 访问范围；
- 文件路径和对象存储 key 校验；
- ToolPolicy 准入；
- network/write/sandbox 风险控制；
- 子智能体并发、递归和超时限制；
- 日志脱敏；
- 错误信息脱敏。

Prompt 中的“禁止访问”不能代替后端权限控制。

## 14. 当前已知平台限制

截至 2026-06-10，新增领域时必须关注以下限制。

### 14.1 执行器诊断行为仍存在领域映射

`ProfiledAgentExecutor._domain_key` 已显式支持 NE、CSH 和 groundwater，但其他新领域仍默认归入 NE，用于：

- graph label；
- graph name；
- `prompt_domain`；
- KB 检索调用次数默认值；
- 部分输出诊断行为。

因此新增第四类领域时，应先将这些运行参数配置化，或明确扩展映射并增加测试，不能静默依赖 NE 默认行为。

### 14.2 Guardrail Policy 尚未配置化注册

`guardrail_policy` 已执行化，但当前策略分发仍位于：

```text
backend/application/policies/profile_runtime_policy.py
backend/core/agents/middlewares/runtime_policy.py
```

新增策略名称必须同步实现：

- 上下文授权；
- permissions 结构；
- Prompt Guardrail 指令；
- Tool 调用拦截；
- 允许与拒绝路径测试。

只在 Profile 中填写新字符串会导致运行时明确报错。

### 14.3 数据库 KB 绑定与运行时绑定来源未完全统一

`AgentInfoService` 可以从数据库读取 Agent-KB 绑定，但 `KnowledgeBaseAccessPolicy`
当前仍以 `profile.default_kb_ids` 判定“Profile 已绑定”。数据库新增绑定若不是平台默认库或当前用户拥有，
可能在查询时被拒绝。

在统一绑定来源前，正式默认 KB 应同时维护 Profile 与数据库展示绑定，并增加真实请求测试。

### 14.4 旧后台 Deep Research API 已移除

协同深研现在直接输出标准 SSE 和报告工件。旧任务状态、任务事件流和取消端点返回 410。
前端不得再以 `task_id` 作为深研完成条件，应以当前 SSE 的 `done` 和工件事件为准。

### 14.5 Domain Context Provider 是同步契约

Provider 当前是同步 callable，由 `AgentService` 的阻塞隔离执行。不要在 Provider 中启动后台任务、
返回异步生成器或保留请求外连接；复杂 I/O 应封装为有超时的同步服务调用，或先升级 Resolver 契约。

## 15. 测试标准

所有新增智能体必须增加自动化测试，最低覆盖以下内容。

### 15.1 Profile 测试

```python
def test_geology_profile_loads():
    profile = AgentProfileLoader().load("geology")
    assert profile.code == "geology"
    assert profile.domain == "engineering-geology"
    assert profile.prompt_profile == "geology"
    assert profile.tool_policy == "geology_default"
    assert profile.guardrail_policy == "default"
```

如配置 `domain_context_provider`，还必须验证 provider 路径可解析。

### 15.2 Prompt 测试

必须验证：

- provider 可 import；
- lightweight/full 均返回非空 Prompt；
- Prompt 包含领域关键约束；
- 未启用搜索时不声称已联网；
- Profile 优先使用 `prompt_profile`。

### 15.3 Tool 测试

必须验证：

- 所有 provider 可 import；
- Skill 声明工具全部存在；
- ToolPolicy 引用工具全部存在；
- domain/workflow/Skill 条件正确；
- provider materialize 后得到预期 `BaseTool.name`；
- 请求级 provider 能收到 `ToolRuntimeContext`；
- Tool 拒绝缺失或未授权的 `domain_context`；
- Tool 不直接使用原始 metadata 扩大资源范围；
- network/write 工具有权限和失败路径测试。

### 15.4 Workflow 测试

必须验证：

- alias 归一化；
- Profile 白名单拦截；
- provider 可构造；
- 标准事件顺序；
- 异常产生 `error` 和 `done`；
- 协同 Workflow 的取消、超时和子任务失败。

### 15.5 API 测试

至少验证：

- Profile 列表包含新 Agent；
- Profile info 返回正确默认项；
- query 返回 `text/event-stream`；
- SSE 使用标准 envelope；
- 最后存在 `[DONE]`；
- Profile 查询接口对未知 Profile 返回 404；
- Service/Registry 对 Profile 未允许的 Workflow 明确拒绝；
- KB 不存在、未激活、非本人且未绑定时被拒绝；
- 项目型 Profile 缺少 metadata、项目不存在或用户未绑定时被拒绝；
- 授权后的 `domain_context` 和 `permissions` 能传入 Tool；
- 流式 query 的初始化和运行时异常不会产生无结束标记的半截响应。

### 15.6 推荐验证命令

统一使用当前项目环境：

```bash
./venv314/bin/python -m compileall -q backend
./venv314/bin/python -m pytest -q \
  backend/tests/test_agent_platform_standardization.py \
  backend/tests/test_agent_runtime_policies.py \
  backend/tests/test_groundwater_project_runtime.py
```

检查 Profile：

```bash
./venv314/bin/python -c \
  "from backend.application.agents.profile_loader import AgentProfileLoader; print(AgentProfileLoader().load('geology'))"
```

检查 Prompt：

```bash
./venv314/bin/python -c \
  "from backend.application.agents.prompt_registry import PromptTemplateRegistry; print(PromptTemplateRegistry().resolve_provider('geology'))"
```

检查 Tool Policy：

```bash
./venv314/bin/python -c \
  "from backend.application.tools.registry import ToolRegistry; print([x.name for x in ToolRegistry().resolve_policy_tools(policy_name='geology_default', domain='engineering-geology', workflow='full', selected_skills={'engineering-geology'})])"
```

服务启动后检查 Profile：

```bash
curl -sS \
  -H "Authorization: Bearer <API_KEY>" \
  http://<guest_frontend_host>:<guest_frontend_port>/api/v2/agent-runtime/profiles/geology
```

查询烟测：

```bash
curl -N \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "说明岩体结构面对边坡稳定性的影响",
    "agent_code": "geology",
    "workflow": "full",
    "selected_skills": ["engineering-geology"],
    "use_search": true
  }' \
  http://<guest_frontend_host>:<guest_frontend_port>/api/v2/agent-runtime/query
```

## 16. 代码审查标准

审查新增智能体时，必须逐项确认：

### 架构

- [ ] 未新增领域专属 Query API；
- [ ] API 未 import 具体 Agent、Prompt、Tool 或 Workflow；
- [ ] 未复制 `ProfiledAgentExecutor`；
- [ ] 未新增 `if agent_code == ...` 分发；
- [ ] Profile、Prompt、Tool、Workflow 均通过 Registry 接入。

### Profile

- [ ] 文件名与 `code` 一致；
- [ ] `domain` 稳定且已加入必要 ToolSpec；
- [ ] `allowed_workflows` 仅包含真实支持的能力；
- [ ] KB ID 真实存在；
- [ ] Prompt 和 ToolPolicy key 可解析；
- [ ] `kb_policy`、`guardrail_policy` 是已实现策略；
- [ ] `domain_context_provider` 可解析并校验用户资源权限。

### Prompt 与 Skill

- [ ] Prompt provider 签名兼容；
- [ ] lightweight/full 行为明确；
- [ ] Skill front matter 完整；
- [ ] Skill 工具可通过 Profile Policy 到达；
- [ ] 不存在只写 Prompt、不做后端权限控制的风险。

### Tool

- [ ] provider import 无副作用；
- [ ] Tool 名称一致；
- [ ] domain/workflow/required_skills 正确；
- [ ] risk level 正确；
- [ ] 请求级 Tool 只使用授权后的 `domain_context` 和 `permissions`；
- [ ] network/write 工具有权限、超时和错误处理。

### Workflow 与事件

- [ ] Workflow 通过 YAML 注册；
- [ ] 事件使用 `AgentEvent`；
- [ ] sequence 单调递增；
- [ ] 异常路径可结束；
- [ ] 没有私有 SSE 格式。

### 测试与上线

- [ ] 标准化测试通过；
- [ ] Profile/Prompt/Tool/Skill 有契约测试；
- [ ] API SSE smoke 通过；
- [ ] 日志中无密钥、Token 和敏感文档内容；
- [ ] 前端没有硬编码错误的 `agent_code`；
- [ ] 新领域已处理执行器默认归入 NE 的诊断映射限制；
- [ ] 项目型入口正确提交 metadata 且拒绝越权资源。

## 17. 完成定义

一个智能体只有同时满足以下条件，才视为开发完成：

1. Profile 可通过 Loader 加载；
2. Prompt provider 可解析且两种模式可运行；
3. 默认 KB 可访问；
4. 默认 Skill 可加载；
5. Skill 所需 Tool 全部可由 Policy 到达；
6. Profile 允许的 Workflow 均真实可执行；
7. 标准 API 可返回完整 SSE；
8. 错误路径产生标准错误事件并结束；
9. 前端正确提交 `agent_code`、KB、Skill 和所需 metadata；
10. 自动化测试和真实服务 smoke 均通过；
11. 没有新增领域硬编码；
12. 请求级资源经过 Domain Context 和 Runtime Policy 授权；
13. 已知平台限制已解决或明确关闭对应能力。

## 18. 推荐开发顺序

```text
确认业务边界
  -> 准备知识库与权限
  -> 创建 Profile
  -> 创建 Prompt
  -> 创建领域 Skill
  -> 注册/复用 Tool
  -> 创建 ToolPolicy
  -> 实现 Domain Context 与 Runtime Policy（如需要）
  -> 验证 Workflow
  -> 补测试
  -> 接入前端
  -> API/SSE smoke
  -> 安全与上线评审
```

遵循该顺序可以较早发现 Profile、Skill、ToolPolicy 和 Workflow 之间的不一致，避免到前端联调阶段才发现工具不可达或领域能力仍被硬编码。
