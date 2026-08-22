"""轻量问答 system/answer 模板与 prompt provider 解析(拆自 lightweight_qa_agent.py)。"""

from __future__ import annotations

import logging
import importlib
import sys

from backend.application.agents.prompt_registry import (
    PromptProvider,
    PromptTemplateRegistry,
)
from types import ModuleType
from typing import Any


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System / Answer template shared by prompt providers that do not define an override.
# ---------------------------------------------------------------------------
PromptDomain = str


LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT = """\
<lightweight_answer_template>
你必须严格按照以下学术标准和结构模板生成回答，不可有任何偏差。

## 一、 回答结构要求（强制）

### 1. 开头结论（强制）
- 回答开头必须先给出“结论段”，但不要写“结论：”字样。
- 结论段控制在 1-3 句话，直接回答用户问题核心，不要先写背景铺垫。
- 若结论依赖前提条件，需在该段同时说明适用边界。

### 2. 整体结构
直接采用"一、二、三、四"等数字序号作为主标题，形成连贯的层次结构：
- 主标题（一、二、三、四...）：主题模块
- 二级标题（1. 2. 3...）：具体要点
- 三级内容：详细说明

### 3. 结尾总结
在回答的最后，必须包含一个独立的总结段落，以"总结："或"综上所述"开头，概括回答的核心要点。

### 4. 参考来源格式
在最后标注文献来源：
- 使用数字编号 [1], [2], [3]...
- 只显示具体文章/文献名称（不含路径和后缀），必须与上下文「可引用文献」或片段标题「来自：XXX」中的名称一致
- **严禁**使用知识库/容器名称作为引用（如"领域知识库"、"资料库"）
- 正文中的 [n] 与末尾「参考文档」列表必须编号一一对应，不得遗漏或混用
- 必须使用 Markdown 列表逐条输出，每条单独一行，格式为：`- [n] 文献名`
- `参考文档：` 与第一条引用之间保留一个空行
- 禁止把多条引用写在同一段落中（例如 `[1] ... [2] ... [3] ...`）

示例：
参考文档：

- [1] 领域基础教程
- [2] 技术应用指南

---

## 二、 图片引用与排版规则（强制要求）
- **仅限使用已登记图片**：严禁伪造、虚构或引用未登记图片。你只能使用当前上下文白名单，或历史对话中已经出现且仍保留的 `[IMAGE:xxx]` 标识符；服务端会校验历史标识并刷新访问签名。没有这两类标识符时绝对禁止产出图片。
- **插入格式**：只能使用标准 Markdown 语法 `![图片描述]([IMAGE:xxx])`，严禁输出图片 URL、`/files/` 路径、MinIO 地址或代理地址。
- **整合原则**：图片必须与上下文相关，但必须使用块级展示，不得与正文在同一行混排。
- **换行硬约束**：图片前后各保留 1 个空行，且图片语法单独占一行。
- **禁止行内图片**：禁止输出“……如图所示![图]([IMAGE:xxx])……”或“（![图]([IMAGE:xxx])）”这类写法。
- **禁止图片标识表格**：不得把 `[IMAGE:xxx]`、图片 URL 或路径作为“图片标识”列输出；多张图片按独立图文段落依次展示。
- **图下注释**：每张图片下方必须紧跟一行图名/图注，格式为 `*图n：图名*`（例如 `*图1：系统原理图*`）。
- **图注编号一致性**：图注编号必须与文中引用一致（如“如图2所示”对应 `*图2：...*`），禁止编号跳跃或重复。
- **上下文关联**：每张图片前必须有至少 1-2 句文字说明，解释图片内容及其与当前论述的关系。
- **一致性**：确保图片的 `alt` 描述与上下文内容高度匹配。

推荐图片段落模板：

说明文字（1-2句，解释该图与当前论述的关系）。

![图1：系统原理图]([IMAGE:xxx])

*图1：系统原理图*

---

## 三、 公式渲染规则
- **行内公式**：必须使用单美元符号包裹，例如 $E=mc^2$
- **行间公式**：必须使用双美元符号包裹，例如 $$E=mc^2$$
- **禁止裸文本**：禁止把上/下标写成裸文本（如 ^252Cf、x_0、10^6 n·s^-1），必须放入公式环境。
- **同位素**：同位素必须写为 LaTeX 兼容形式，例如 $^{252}\\mathrm{Cf}$、$^{60}\\mathrm{Co}$。
- **科学计数法**：优先写为 $a \\times 10^b$，单位建议使用 $\\mathrm{...}$ 包裹。
- **禁止 HTML 标签**：禁止输出 HTML 上下标标签，包括 `<sup>...</sup>`、`<sub>...</sub>` 及其变体（如 `< sup >`、`&lt;sup&gt;`）。
- **规范化**：若上下文中出现 OCR/抽取噪声形式（如 `cm < sup > 3 < / sup >`），需要在回答中规范化为 KaTeX 写法（如 $\\mathrm{cm^3}$），数值本身保持不变。

---

## 四、 回答聚焦与数值精确性
- **直接回答**：始终围绕用户原始问题展开，避免无关的背景介绍或发散。
- **数值精确性**：使用知识库中的原始数值，必须原样引用，严禁简化、修改或四舍五入。
- **对比分析表格要求**：当回答涉及两个或多个概念的比较时，使用清晰的表格进行对比。
</lightweight_answer_template>
"""


def _build_lightweight_system_header_and_template(
    *,
    prompt_domain: PromptDomain | None = None,
    prompt_profile: str | None = None,
    domain: str | None = None,
    use_search: bool,
    lightweight_has_kb: bool,
    available_skills: set[str] | None = None,
    prompt_registry: PromptTemplateRegistry | None = None,
    prompt_provider: PromptProvider | None = None,
) -> tuple[str, str]:
    """Resolve lightweight prompt resources from the registered prompt provider."""

    provider, provider_module, _ = _resolve_lightweight_prompt_provider(
        prompt_domain=prompt_domain,
        prompt_profile=prompt_profile,
        domain=domain,
        prompt_registry=prompt_registry,
        prompt_provider=prompt_provider,
    )
    header = provider(
        subagent_enabled=False,
        use_search=use_search,
        # ``None`` means "all public Skills" to the shared base template.
        # Lightweight execution must advertise only the request-authorized
        # selection, including the legitimate empty selection.
        available_skills=set(available_skills or ()),
        task_type=None,
        lightweight_mode=True,
        lightweight_has_kb=bool(lightweight_has_kb),
    )
    answer_template = _read_prompt_resource(
        provider_module,
        "LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT",
    )
    return header, answer_template or LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT


def _get_lightweight_search_prefix(
    prompt_domain: PromptDomain | None = None,
    *,
    prompt_profile: str | None = None,
    domain: str | None = None,
    prompt_registry: PromptTemplateRegistry | None = None,
    prompt_provider: PromptProvider | None = None,
) -> str:
    """Return a provider-owned search prefix or a domain-neutral fallback."""

    _, provider_module, resolved_domain = _resolve_lightweight_prompt_provider(
        prompt_domain=prompt_domain,
        prompt_profile=prompt_profile,
        domain=domain,
        prompt_registry=prompt_registry,
        prompt_provider=prompt_provider,
    )
    configured = _read_prompt_resource(
        provider_module,
        "LIGHTWEIGHT_SEARCH_QUERY_PREFIX",
    )
    if configured:
        return configured
    return (
        "<lightweight_search_domain>\n"
        f"领域：{resolved_domain}。保留用户问题中的领域专名、数值、单位与约束，"
        "生成可独立理解且适合证据检索的查询；不得注入其他领域术语。\n"
        "</lightweight_search_domain>\n"
    )


def _get_lightweight_file_search_prefix(
    prompt_domain: PromptDomain | None = None,
    *,
    prompt_profile: str | None = None,
    domain: str | None = None,
    prompt_registry: PromptTemplateRegistry | None = None,
    prompt_provider: PromptProvider | None = None,
) -> str:
    _, provider_module, resolved_domain = _resolve_lightweight_prompt_provider(
        prompt_domain=prompt_domain,
        prompt_profile=prompt_profile,
        domain=domain,
        prompt_registry=prompt_registry,
        prompt_provider=prompt_provider,
    )
    configured = _read_prompt_resource(
        provider_module,
        "LIGHTWEIGHT_FILE_SEARCH_PREFIX",
    )
    if configured:
        return configured
    return (
        "<lightweight_file_search_domain>\n"
        f"领域：{resolved_domain}。从上传文档提取检索词时优先采用文档自身的"
        "术语、实体、型号和标题，不得猜测未出现的领域概念。\n"
        "</lightweight_file_search_domain>\n"
    )


def _resolve_lightweight_prompt_provider(
    *,
    prompt_domain: PromptDomain | None,
    prompt_profile: str | None,
    domain: str | None,
    prompt_registry: PromptTemplateRegistry | None,
    prompt_provider: PromptProvider | None = None,
) -> tuple[Any, ModuleType, str]:
    profile_name = str(prompt_profile or prompt_domain or "").strip()
    if not profile_name or profile_name.lower() == "default":
        raise ValueError(
            "Lightweight runtime requires an explicit registered prompt_profile"
        )
    resolved_domain = str(domain or prompt_domain or profile_name).strip()
    if not resolved_domain:
        raise ValueError("Lightweight runtime requires an explicit domain")
    if prompt_provider is not None:
        provider = prompt_provider
    else:
        registry = prompt_registry or PromptTemplateRegistry()
        provider = registry.resolve_provider(profile_name)
    module_name = str(getattr(provider, "__module__", "") or "").strip()
    if not module_name:
        raise ValueError(
            f"Prompt provider for '{profile_name}' does not expose a module"
        )
    provider_module = sys.modules.get(module_name)
    if provider_module is None:
        try:
            provider_module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            provider_module = ModuleType(module_name)
            provider_globals = getattr(provider, "__globals__", None)
            if isinstance(provider_globals, dict):
                provider_module.__dict__.update(provider_globals)
    return provider, provider_module, resolved_domain


def _read_prompt_resource(module: ModuleType, *names: str) -> str:
    for name in names:
        value = getattr(module, name, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""
