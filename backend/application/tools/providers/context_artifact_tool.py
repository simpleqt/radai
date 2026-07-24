"""Tool for bounded reads of context artifact references."""

from __future__ import annotations

import json

from langchain_core.tools import StructuredTool, tool

from backend.core.agents.context import (
    context_artifact_scope_matches,
    create_context_artifact_store,
)
from backend.domain.tools.runtime_context import ToolRuntimeContext

_CONTEXT_ARTIFACT_PREFIX = "artifact://context/"
_CONTEXT_RECORD_ID_PREFIXES = ("ev_", "raw_")


def _artifact_budget_error() -> str | None:
    """Consume one artifact read allowance and return an error when exhausted."""

    from backend.core.tools.request_limits import consume_tool_budget

    allowed, _remaining = consume_tool_budget("read_context_artifact")
    if allowed:
        return None
    return json.dumps(
        {
            "error": "tool_budget_exhausted",
            "tool": "read_context_artifact",
            "instruction": (
                "The context artifact read allowance for this turn is exhausted. "
                "Do not retry this tool; continue with context already available."
            ),
        },
        ensure_ascii=False,
    )


def _invalid_artifact_ref(raw_ref: str) -> str | None:
    normalized = str(raw_ref or "").strip()
    if normalized.startswith(_CONTEXT_ARTIFACT_PREFIX):
        relative = normalized[len(_CONTEXT_ARTIFACT_PREFIX) :].lstrip("/")
        if relative and not relative.startswith(_CONTEXT_RECORD_ID_PREFIXES):
            return None
        reason = (
            "context_record_id_is_not_an_artifact_ref"
            if relative.startswith(_CONTEXT_RECORD_ID_PREFIXES)
            else "artifact_ref_path_missing"
        )
    else:
        reason = "raw_ref_must_start_with_artifact_context_scheme"
    return json.dumps(
        {
            "error": "invalid_artifact_ref",
            "reason": reason,
            "instruction": (
                "read_context_artifact only accepts an explicit "
                "artifact://context/... reference returned by context capture. "
                "A context record id such as ev_* or raw_* is not a raw_ref. "
                "Never pass an http(s) URL, knowledge-base citation, guessed path, "
                "or a source identifier; use those sources directly."
            ),
        },
        ensure_ascii=False,
    )


@tool("read_context_artifact")
def read_context_artifact(raw_ref: str, max_chars: int = 12000) -> str:
    """未绑定身份的兼容入口；生产运行时必须使用 scoped 工具工厂。

    Args:
        raw_ref: ContextItem.raw_ref 或 handoff.raw_artifact_refs 明确返回的
            artifact://context/... 引用。不得传入 http(s) URL、知识库引用或猜测路径。
        max_chars: 最大返回字符数，默认 12000，最大 50000。
    """
    budget_error = _artifact_budget_error()
    if budget_error is not None:
        return budget_error
    invalid = _invalid_artifact_ref(raw_ref)
    if invalid is not None:
        return invalid
    return json.dumps(
        {
            "error": "artifact_access_denied",
            "reason": "runtime_identity_missing",
            "raw_ref": raw_ref,
        },
        ensure_ascii=False,
    )


def _read_context_artifact_for_scope(
    raw_ref: str,
    max_chars: int,
    *,
    runtime_context: ToolRuntimeContext | None,
) -> str:
    """Read an artifact only after checking the bound user/thread scope."""

    invalid = _invalid_artifact_ref(raw_ref)
    if invalid is not None:
        return invalid
    bounded = max(1, min(int(max_chars or 12000), 50000))
    if runtime_context is None:
        return json.dumps(
            {"error": "artifact_access_denied", "reason": "runtime_identity_missing"},
            ensure_ascii=False,
        )
    user_id = str(runtime_context.user_id or "").strip()
    thread_id = str(runtime_context.thread_id or "").strip()
    if not user_id or not thread_id:
        return json.dumps(
            {"error": "artifact_access_denied", "reason": "scope_missing"},
            ensure_ascii=False,
        )

    store = create_context_artifact_store()
    metadata = store.metadata(raw_ref)
    if not context_artifact_scope_matches(
        metadata,
        user_id=user_id,
        thread_id=thread_id,
    ):
        # Do not distinguish an unknown reference from a reference owned by a
        # different user; both are deliberately opaque to the model.
        return json.dumps(
            {"error": "artifact_access_denied", "raw_ref": raw_ref},
            ensure_ascii=False,
        )
    content = store.read(raw_ref, max_chars=bounded)
    if content is None:
        return json.dumps(
            {"error": "artifact_not_found", "raw_ref": raw_ref},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "raw_ref": raw_ref,
            "max_chars": bounded,
            "truncated": bool(metadata and metadata.size_chars > len(content)),
            "metadata": metadata.__dict__ if metadata else None,
            "content": content,
        },
        ensure_ascii=False,
    )


def get_scoped_read_context_artifact_tool(
    runtime_context: ToolRuntimeContext | None = None,
) -> StructuredTool:
    """Materialize the production artifact reader with request identity bound."""

    def _scoped_read_context_artifact(
        raw_ref: str,
        max_chars: int = 12000,
    ) -> str:
        budget_error = _artifact_budget_error()
        if budget_error is not None:
            return budget_error
        return _read_context_artifact_for_scope(
            raw_ref,
            max_chars,
            runtime_context=runtime_context,
        )

    return StructuredTool.from_function(
        func=_scoped_read_context_artifact,
        name="read_context_artifact",
        description=(
            "只读取当前认证用户和线程授权范围内、由系统明确返回的 "
            "artifact://context/... 上下文引用；禁止传入 http(s) 网页 URL、"
            "知识库引用或猜测路径。未知或越权 artifact 引用统一返回拒绝。"
        ),
    )
