"""跨智能体通用的展示回落。

只处理一件事：工具用 JSON 报错时，过程面板显示这句错误，而不是 JSON 片段。
其余工具继续走前端的降级映射路径，行为与改造前一致。
"""

from __future__ import annotations

from backend.application.agent_executors.tool_presenters.types import (
    DisplayChip,
    ToolDisplay,
    ToolPresenterInput,
)


def output_has_error(raw_output: object) -> bool:
    """工具用 JSON 报错时返回 True，供调用方把步骤级别降为 warning。"""
    parsed = ToolPresenterInput(tool_name="", raw_output=raw_output).parsed_output()
    return bool(parsed and str(parsed.get("error") or "").strip())


def json_error_display(payload: ToolPresenterInput) -> ToolDisplay | None:
    parsed = payload.parsed_output()
    if not parsed:
        return None
    message = str(parsed.get("error") or "").strip()
    if not message:
        return None

    chips: list[DisplayChip] = []
    allowed = parsed.get("allowed_actions")
    if isinstance(allowed, list) and allowed:
        chips.append(
            DisplayChip(text=f"可选操作: {'、'.join(str(a) for a in allowed[:3])}")
        )

    return ToolDisplay(
        label=f"{payload.tool_name} 执行未通过",
        status_text=message,
        chips=chips,
        view="text",
        data={"text": message},
    )
