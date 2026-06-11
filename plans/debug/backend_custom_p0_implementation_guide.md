# P0 修复实施指南（伪代码级）

**前置**：[backend_custom_p0_fix_checklist.md](./backend_custom_p0_fix_checklist.md)  
**原则**：最小 diff、可拆 3 个 PR；本文不写完整补丁，只给文件级步骤与接口约定。  
**日期**：2026-05-30

---

## 总览

| PR | 目标 | 新建文件 | 主要改动文件 |
|----|------|----------|--------------|
| PR-A | 思考标签过滤统一 | `agents/streaming/thinking_filter.py` | `guest.py`, `runner.py`, `agent.py`, `csh_agent.py` |
| PR-B | 轻量 Prompt 按域 | 可选 `agents/support/lightweight_answer_template.py` | `lightweight_qa_agent.py`, `csh_full_feature_agent.py`, `explore.py` |
| PR-C | CSH 深研编排器 | `agents/csh_deep_research_orchestrator_agent.py`, `agents/agent_factory.py` | `deep_research_orchestrator_agent.py`(可选抽基类), `csh_router.py`, `runner.py`, `csh_agent.py` |

---

## PR-A：P0-3 思考标签过滤

### A.1 新建 `agents/streaming/thinking_filter.py`

从以下来源**合并为唯一实现**（逻辑以 `api/v2/guest.py` L28-85 为准）：

```python
# 导出
_THINK_OPEN_RE = re.compile(...)
_THINK_CLOSE_RE = re.compile(...)
_THINK_TAG_TOKENS = (...)
_MAX_THINK_TAG_LEN = ...

def extract_possible_tag_prefix(text: str) -> str: ...

def split_thinking_segments(
    text: str,
    in_thinking: bool,
    carry: str,
) -> tuple[str, str, bool, str]:
    """Returns: visible, thinking, in_thinking, carry"""

@dataclass
class ThinkingStreamSplitter:
    """有状态封装，供 SSE 循环使用。"""
    in_thinking: bool = False
    carry: str = ""

    def feed(self, chunk: str) -> tuple[str, str]:
        visible, thinking, self.in_thinking, self.carry = split_thinking_segments(
            chunk, self.in_thinking, self.carry
        )
        return visible, thinking
```

### A.2 修改 `api/v2/agent.py` 流式循环（约 L929-936）

**当前（错误）**：

```python
raw_text = str(event.get("content") or "")
if raw_text:
    accumulated_answer += raw_text
    yield chunk SSE(raw_text)
```

**目标（与 guest 一致）**：

```python
from backend.custom.agents.streaming.thinking_filter import ThinkingStreamSplitter

splitter = ThinkingStreamSplitter()  # 在 stream_generator 开头创建

# 在 event["type"] == "chunk" 分支：
raw_text = str(event.get("content") or "")
if raw_text:
    visible, thinking = splitter.feed(raw_text)
    if thinking:
        yield SSE type=thinking, content=thinking
    if visible:
        accumulated_answer += visible
        yield SSE type=chunk, content=visible
```

**清理**：删除本文件内重复的 `_split_thinking_segments`、`_extract_possible_tag_prefix`、未使用的 `in_thought`/`think_carry`/`stripped_chunk_count`（或仅保留统计用）。

### A.3 同步 `api/v2/csh_agent.py`

与 A.2 相同改动（复制逻辑或抽 `api/v2/_stream_thinking.py` 若愿减少重复——属 P1，P0 可先各改一处）。

### A.4 修改 `api/v2/guest.py`、`tasks/runner.py`

- 删除本地重复函数，改为 `from ...thinking_filter import ...`
- 行为应保持不变（回归 guest 用例）

### A.5 可选（更彻底，略超 P0 范围）

在 `NEAgentStreamHandler._on_model_stream` 出口统一 `splitter.feed`，则 API 层可不再过滤——需验证 handler 是否覆盖所有 chunk 来源。P0 建议 **先改 API**，风险更低。

### A.6 测试

```text
手工：模型返回 前言秘密推理正文
期望 SSE：thinking 事件含「秘密推理」；chunk 仅「前言」「正文」

自动化（建议）：对 split_thinking_segments 单测
  - 跨 chunk 拆开标签：", 思, 考, , 答"
  - 空输入、仅 thinking、无标签

```

---

## PR-B：P0-2 轻量 Prompt 领域化

### 问题精确定位

| 组件 | 现状 | 应有 |
|------|------|------|
| `csh_prompts.build_system_prompt(lightweight_mode=True)` | ✅ 已实现 CSH 轻量角色 | 未被 LightweightQAAgent 调用 |
| `LightweightQAAgent` L839 | 固定 `LIGHTWEIGHT_SYSTEM_PROMPT`（NE） | 按 domain 选择 |
| `LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT` | NE 示例（核探测器、[1] 核辐射…） | CSH 需独立模板或泛化示例 |

### B.1 扩展 `LightweightQAAgent.__init__`

```python
class LightweightQAAgent:
    def __init__(
        self,
        *,
        ...
        prompt_domain: Literal["ne", "csh"] = "ne",  # 新参数
    ):
        self._prompt_domain = prompt_domain
```

### B.2 新增 `_build_lightweight_system_parts(self, ...) -> list[str]`

```python
def _build_lightweight_system_parts(self, *, lightweight_has_kb: bool) -> list[str]:
    if self._prompt_domain == "csh":
        from backend.custom.agents.support import csh_prompts
        header = csh_prompts.build_system_prompt(
            subagent_enabled=False,
            use_search=self._use_search,
            available_skills=None,
            task_type=None,
            lightweight_mode=True,
            lightweight_has_kb=lightweight_has_kb,
        )
        template = CSH_LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT  # 见 B.3
    else:
        header = LIGHTWEIGHT_SYSTEM_PROMPT  # 或 prompts.build_system_prompt(lightweight_mode=True)
        template = LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT
    return [header, template]
```

在 `aquery_stream` 原 L839 处改为：

```python
system_parts = self._build_lightweight_system_parts(
    lightweight_has_kb=bool(self._kb_ids),
)
```

### B.3 新增 CSH 回答模板（二选一）

**方案 1（推荐）**：在 `csh_prompts.py` 增加 `CSH_LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT`，结构与 NE 相同，但：

- 参考文档示例改为水文地质文献名
- 删除「核探测器」「核电子学库」等 NE 专用措辞
- 图片示例改为场地调查/渗透曲线等

**方案 2**：抽 `agents/support/lightweight_answer_template.py`，`get_answer_template(domain)` 返回对应字符串。

### B.4 修改 `csh_full_feature_agent.py`（约 L555）

```python
lightweight_agent = LightweightQAAgent(
    ...
    prompt_domain="csh",
)
```

### B.5 修改 `api/v2/explore.py`（约 L234-242）

```python
domain = str(payload.domain or "ne").strip().lower()
prompt_domain = "csh" if domain == "csh" else "ne"
# 在委托 Lightweight 的路径：若 explore 未来直接调 LightweightQAAgent，传 prompt_domain
# 当前经 AgentClass(lightweight_mode=True) → 在 CSHFullFeatureAgent 已传 domain 即可
```

explore 已用 `CSHFullFeatureAgent` when domain=csh，**只要 B.4 完成即可**，explore 无需再改（除非 explore 直连 Lightweight）。

### B.6 测试

```http
POST /api/v2/explore/detail
{"node_title":"渗透系数","path":[],"domain":"csh","kb_ids":["<kb_id_csh_...>"]}
```

断言：首段回答不出现「核电子学智能体」；可出现地下水/渗透相关表述。

```http
POST /api/v2/csh_agent/query
{"question":"什么是渗透系数？","selected_skills":[]}
```

轻量路由（无 skill）应走 Lightweight + CSH prompt。

---

## PR-C：P0-1 CSH 深度研究编排器

### 问题精确定位

`DeepResearchOrchestratorAgent` 继承链：

```text
DeepResearchOrchestratorAgent → NEFullFeatureAgent._build_system_prompt()
  → prompts.build_system_prompt()  # 核电子学

```

`CSHAgentRouter` L85 与 `runner.py` L212 均 `DeepResearchOrchestratorAgent(..., kb_ids=csh_ids)` — **仅 KB 对，Prompt 错**。

### C.1 新建 `agents/csh_deep_research_orchestrator_agent.py`

```python
from backend.custom.agents.csh_full_feature_agent import CSHFullFeatureAgent

_CSH_ORCHESTRATOR_APPEND = """
<deep_research_orchestrator_hard_rules>
... 与 NE 版同结构，措辞改为场地污染/水文地质 ...
- 主智能体禁止直接 multi_kb_search / web_search，委派 subagent_type=deep-research
- 同一轮 2 个 task 并行 ...
</deep_research_orchestrator_hard_rules>
"""

class CSHDeepResearchOrchestratorAgent(CSHFullFeatureAgent):
    def __init__(self, **kwargs: Any):
        kwargs["task_type"] = self.DEEP_RESEARCH_TASK_TYPE
        kwargs["mode"] = "deep-research"
        kwargs["subagent_enabled"] = True
        kwargs["max_concurrent_subagents"] = 2
        super().__init__(**kwargs)

    def _build_system_prompt(self) -> str:
        return super()._build_system_prompt() + _CSH_ORCHESTRATOR_APPEND

    def _get_tools(self, model_name: str | None = None) -> list:
        # 复制 DeepResearchOrchestratorAgent._get_tools 逻辑
        tools = super()._get_tools(model_name=model_name)
        blocked = {"multi_kb_search", "web_search", "duckduckgo_search"}
        return [t for t in tools if t.name not in blocked]
```

`_get_tools` 可抽到 `agents/support/orchestrator_tools.py` 供 NE/CSH 共用（P1）。

### C.2 新建 `agents/agent_factory.py`（小工厂）

```python
AgentDomain = Literal["ne", "csh"]

def create_deep_research_orchestrator(
    domain: AgentDomain,
    **kwargs: Any,
) -> NEFullFeatureAgent:  # 或 Protocol / 基类
    if domain == "csh":
        from .csh_deep_research_orchestrator_agent import CSHDeepResearchOrchestratorAgent
        return CSHDeepResearchOrchestratorAgent(**kwargs)
    from .deep_research_orchestrator_agent import DeepResearchOrchestratorAgent
    return DeepResearchOrchestratorAgent(**kwargs)
```

### C.3 修改 `agents/csh_router.py` L77-95

```python
from backend.custom.agents.agent_factory import create_deep_research_orchestrator

executor = create_deep_research_orchestrator(
    "csh",
    model_name=self._model_name,
    use_search=self._use_search,
    kb_ids=self._kb_ids,
    ...
)
```

### C.4 修改 `tasks/runner.py`

函数签名增加：

```python
async def run_deep_research_background(
    ...,
    agent_domain: Literal["ne", "csh"] = "ne",
) -> None:
```

L212 附近：

```python
agent = create_deep_research_orchestrator(
    agent_domain,
    model_name=model_name,
    thinking_enabled=False,
    subagent_enabled=True,
    use_search=use_search,
    kb_ids=kb_ids,
    ...
)
```

### C.5 修改 `api/v2/csh_agent.py` 后台任务启动（约 L655）

```python
asyncio.create_task(
    run_deep_research_background(
        ...,
        agent_domain="csh",
    )
)
```

`agent.py` 保持默认 `agent_domain="ne"` 或不传。

### C.6 实时深研路径

用户未走后台、直接 `CSHAgentRouter` deep_research 时，C.3 已覆盖。

API 层 `runtime_deep_research_mode` 走 `CSHAgentRouter` 时同样生效。

### C.7 测试

1. CSH 用户确认深研后台任务 → 子智能体检索 CSH kb_id  
2. 日志/抽检 system prompt 片段含「场地污染水文地质」、不含「核电子学智能体」  
3. NE 回归：RadAI 深研仍用 `DeepResearchOrchestratorAgent`

---

## 合并顺序与冲突

```text
PR-A ──┐
PR-B ──┼── 可并行开发；合并顺序建议 A → B → C（C 依赖 B 可选）
PR-C ──┘

```

若 B 与 C 同时改 `csh_full_feature_agent.py`，注意合并冲突。

---

## 文档同步（每个 PR 各勾一项）

| PR | 更新文档 |
|----|----------|
| A | `sse_events_module.md` §思考过滤 |
| B | `csh_ai_module.md` 轻量通道；`explore_module.md` |
| C | `deep_research_module.md` §8；`agent_engine_module.md` 路由图 |

---

## 回滚策略

- PR-A：仅新增模块 + 改 import，回滚删除 `thinking_filter` 引用即可  
- PR-B：`prompt_domain` 默认 `"ne"`，回滚参数不影响 NE  
- PR-C：工厂默认 `"ne"`，删除 CSH 类即可回滚 CSH 侧  

---

*实施时仍遵守 P0 清单：不做 API 大重构、不做 workflows/registry。*
