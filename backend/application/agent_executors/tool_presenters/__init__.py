"""工具展示 presenter 注册表。

presenter 把工具原始返回值翻译成结构化展示语义（`ToolDisplay`）。
未注册的工具回落 `default`，行为与改造前一致；presenter 抛异常时同样回落，
渲染改造不影响主流程可用性。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from backend.application.agent_executors.tool_presenters.common import (
    json_error_display,
    output_has_error,
)
from backend.application.agent_executors.tool_presenters.types import (
    ToolDisplay,
    ToolPresenterInput,
    normalize_display_payload,
)

logger = logging.getLogger(__name__)

Presenter = Callable[[ToolPresenterInput], ToolDisplay | None]

_REGISTRY: dict[str, Presenter] = {}
_PHASES: dict[str, str] = {}


def register(tool_name: str, presenter: Presenter, *, phase: str = "") -> None:
    _REGISTRY[tool_name] = presenter
    if phase:
        _PHASES[tool_name] = phase


def register_many(
    presenters: dict[str, Presenter], *, phases: dict[str, str] | None = None
) -> None:
    for tool_name, presenter in presenters.items():
        register(tool_name, presenter, phase=(phases or {}).get(tool_name, ""))


def has_presenter(tool_name: str) -> bool:
    """是否有专属 presenter。JSON 报错兜底不算，调用方据此决定能否抑制旧事件。"""
    return tool_name in _REGISTRY


def resolve_phase(tool_name: str) -> str:
    return _PHASES.get(tool_name, "")


def build_display_payload(
    tool_name: str,
    *,
    tool_input: dict[str, Any] | None = None,
    raw_output: Any = None,
    duration_ms: int | None = None,
) -> dict[str, Any] | None:
    """产出经过上限校验的展示载荷。任何异常都降级为 None（回落旧渲染）。"""

    payload_input = ToolPresenterInput(
        tool_name=tool_name,
        tool_input=dict(tool_input or {}),
        raw_output=raw_output,
        duration_ms=duration_ms,
    )

    # 专属 presenter 优先；没有时只兜底 JSON 报错，其余仍走前端降级映射。
    presenter = _REGISTRY.get(tool_name) or json_error_display
    try:
        display = presenter(payload_input)
    except Exception:  # pragma: no cover - presenter 不得影响主流程
        logger.warning(
            "[ToolPresenter] presenter failed, falling back: tool=%s",
            tool_name,
            exc_info=True,
        )
        return None

    if display is None:
        return None
    return normalize_display_payload(display.to_payload(), tool_name=tool_name)


def _install_builtin_presenters() -> None:
    from backend.application.agent_executors.tool_presenters import (  # noqa: PLC0415
        course_eval,
    )

    course_eval.install()


_install_builtin_presenters()

__all__ = [
    "Presenter",
    "build_display_payload",
    "has_presenter",
    "output_has_error",
    "register",
    "register_many",
    "resolve_phase",
]
