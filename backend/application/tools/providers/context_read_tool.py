"""Navigator tool for per-turn context-object handles."""

from __future__ import annotations

import json
import threading
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool, tool

from backend.core.agents.context.artifact_store import context_owner_allowed
from backend.core.agents.context.capability_ledger import (
    CapabilityLedger,
    Grant,
)
from backend.core.agents.context.object_store import ContextObjectStore
from backend.domain.tools.runtime_context import ToolRuntimeContext

# 共享池耗尽时定向读取的保底配额(有界超支,reserve 计费饱和于池上限)。
_EXHAUSTED_READ_FLOOR_TOKENS = 512

_CAPABILITY_LEDGER: ContextVar[CapabilityLedger | None] = ContextVar(
    "context_capability_ledger",
    default=None,
)

# Scope key -> stack of weakly referenced ledgers.  Keys are context-scope ids
# (request_id + execution thread id) so parallel coordination units and task
# sub-agents never share an entry; the stack plus remove-by-identity keeps
# even a same-key overlap safe: an early uninstall removes only its own
# ledger instead of "restoring" a snapshot that may already be stale.
_LEDGER_REGISTRY: dict[str, list[weakref.ref[CapabilityLedger]]] = {}
_LEDGER_REGISTRY_LOCK = threading.RLock()


@dataclass(frozen=True)
class _LedgerInstallToken:
    """Opaque token carrying the ContextVar reset and registry entry identity."""

    context_token: Any
    request_id: str
    ledger_ref: Any


def _registry_push(request_id: str, ledger: CapabilityLedger) -> Any:
    ledger_ref = weakref.ref(ledger)
    with _LEDGER_REGISTRY_LOCK:
        _LEDGER_REGISTRY.setdefault(request_id, []).append(ledger_ref)
    return ledger_ref


def _registry_remove(request_id: str, ledger_ref: Any) -> None:
    with _LEDGER_REGISTRY_LOCK:
        stack = _LEDGER_REGISTRY.get(request_id)
        if not stack:
            return
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] is ledger_ref:
                del stack[index]
                break
        stack[:] = [ref for ref in stack if ref() is not None]
        if not stack:
            _LEDGER_REGISTRY.pop(request_id, None)


def set_request_capability_ledger(
    request_id: str,
    ledger: CapabilityLedger,
) -> Any:
    token = _CAPABILITY_LEDGER.set(ledger)
    request_id = str(request_id or "").strip()
    if request_id:
        _registry_push(request_id, ledger)
        ledger._unbound = False
    else:
        ledger._unbound = True
    return token


def install_request_capability_ledger(
    request_id: str,
    ledger: CapabilityLedger,
) -> _LedgerInstallToken:
    """Install a scope ledger onto the registry stack for its scope key.

    Nested executors on the same scope key push/pop like a stack; uninstall
    removes exactly the installed ledger by identity, so overlapping
    lifetimes (parallel units that accidentally share a key) can no longer
    evict a sibling that is still running or resurrect a released ledger.
    """

    request_id = str(request_id or "").strip()
    ledger_ref: Any = None
    if request_id:
        ledger_ref = _registry_push(request_id, ledger)
        ledger._unbound = False
    else:
        ledger._unbound = True
    context_token = _CAPABILITY_LEDGER.set(ledger)
    return _LedgerInstallToken(
        context_token=context_token,
        request_id=request_id,
        ledger_ref=ledger_ref,
    )


def uninstall_request_capability_ledger(token: _LedgerInstallToken) -> None:
    """Reset the ContextVar and drop exactly this token's registry entry."""

    try:
        _CAPABILITY_LEDGER.reset(token.context_token)
    except ValueError:
        pass
    if token.request_id and token.ledger_ref is not None:
        _registry_remove(token.request_id, token.ledger_ref)


@contextmanager
def capability_ledger_scope(request_id: str, ledger: CapabilityLedger):
    """Run a block with ``ledger`` installed and restore the outer scope."""

    token = install_request_capability_ledger(request_id, ledger)
    try:
        yield
    finally:
        uninstall_request_capability_ledger(token)


def reset_request_capability_ledger(token: Any, request_id: str | None = None) -> None:
    try:
        _CAPABILITY_LEDGER.reset(token)
    except ValueError:
        pass
    request_id = str(request_id or "").strip()
    if request_id:
        with _LEDGER_REGISTRY_LOCK:
            _LEDGER_REGISTRY.pop(request_id, None)


def get_capability_ledger(request_id: str | None = None) -> CapabilityLedger | None:
    request_id = str(request_id or "").strip()
    if request_id:
        with _LEDGER_REGISTRY_LOCK:
            stack = _LEDGER_REGISTRY.get(request_id)
            if stack:
                for ref in reversed(stack):
                    ledger = ref()
                    if ledger is not None:
                        return ledger
                _LEDGER_REGISTRY.pop(request_id, None)
        context_ledger = _CAPABILITY_LEDGER.get()
        if context_ledger is not None and getattr(
            context_ledger,
            "_unbound",
            False,
        ):
            return context_ledger
        return None
    return _CAPABILITY_LEDGER.get()


def _available_handles(ledger: CapabilityLedger) -> list[str]:
    return sorted(
        dict.fromkeys(
            receipt.get("handle") or ""
            for receipt in ledger.receipts
            if receipt.get("handle")
        )
    )


def _ledger_excerpt(
    ledger: CapabilityLedger,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    return [
        {
            "n": int(row.get("n") or 0),
            "sid": str(row.get("sid") or ""),
            "title": str(row.get("title") or ""),
            "score": float(row.get("score") or 0.0),
            "fidelity": str(row.get("fidelity") or "omitted"),
        }
        for row in ledger.ledger_rows()[:limit]
    ]


def _unknown_handle(handle: str, ledger: CapabilityLedger | None = None) -> str:
    return json.dumps(
        {
            "error": "unknown_handle",
            "handle": handle,
            "instruction": (
                "该句柄在当前轮次不可用。句柄按轮铸造，跨轮不能复用；"
                "请使用【证据台账】中的 sid 调用 context_read(sids=[...])，"
                "或使用 available_handles；禁止重新发起知识库检索。"
            ),
            "available_handles": _available_handles(ledger) if ledger else [],
            "ledger_excerpt": _ledger_excerpt(ledger) if ledger else [],
        },
        ensure_ascii=False,
    )


def _denied_sid(handle: str, sid: str) -> str:
    return json.dumps(
        {
            "error": "sid_not_granted",
            "handle": handle,
            "sid": sid,
            "instruction": (
                "只允许读取【证据台账】中本轮明确授权的 sid；"
                "请按台账中的 sid 取回，禁止重新发起知识库检索。"
            ),
        },
        ensure_ascii=False,
    )


def _sid_not_granted(
    ledger: CapabilityLedger,
    sids: list[str],
    requested_sids: list[str],
) -> str:
    return json.dumps(
        {
            "error": "sid_not_granted",
            "sids": sids,
            "requested_sids": requested_sids,
            "instruction": (
                "这些 sid 本轮未授权，可能来自归档或跨轮历史。"
                "优先改用【证据台账】列出的 sid，禁止仅为换取同一 sid 重新检索。"
                "仅当该 sid 不在本轮台账中、且其正文对回答确属必需时，"
                "才可就该主题重新发起一次知识库检索（旧对象可能已过期被清理）。"
            ),
            "available_sids": [
                str(row.get("sid") or "") for row in ledger.ledger_rows()[:40]
            ],
            "available_handles": _available_handles(ledger),
            "ledger_excerpt": _ledger_excerpt(ledger),
        },
        ensure_ascii=False,
    )


def _missing_sids() -> str:
    return json.dumps(
        {
            "error": "missing_sids",
            "instruction": (
                "context_read 必须至少提供一个 sid："
                'context_read(sids=["sid"])。请从【证据台账】选择 sid，'
                "禁止重新发起知识库检索。"
            ),
        },
        ensure_ascii=False,
    )


def _context_read_for_scope(
    sids: list[str] | None,
    handle: str | None,
    max_tokens: int,
    *,
    runtime_context: ToolRuntimeContext | None,
) -> str:
    # The scope id distinguishes parallel units / task sub-agents of one
    # request; the plain request_id remains the legacy fallback.
    request_id = (
        str(
            getattr(runtime_context, "context_scope_id", "")
            or runtime_context.request_id
            or ""
        )
        if runtime_context
        else ""
    )
    ledger = get_capability_ledger(request_id)
    if ledger is None:
        return json.dumps(
            {
                "error": "capability_ledger_missing",
                "instruction": "当前请求没有上下文句柄映射。",
            },
            ensure_ascii=False,
        )
    from backend.core.tools.request_limits import consume_tool_budget

    allowed, _remaining = consume_tool_budget("context_read")
    if not allowed:
        return json.dumps(
            {
                "error": "tool_budget_exhausted",
                "tool": "context_read",
                "instruction": "本轮 context_read 调用预算已用尽，请基于已有上下文作答。",
            },
            ensure_ascii=False,
        )
    requested_sids = [str(sid) for sid in sids or [] if str(sid or "").strip()]
    handle = str(handle or "").strip() or None
    if not requested_sids:
        if not handle:
            return _missing_sids()
        grant = ledger.resolve(handle)
        if grant is None:
            return _unknown_handle(handle, ledger)
        return _read_grant_segments(
            ledger,
            grant,
            handle=handle,
            sids=None,
            max_tokens=max_tokens,
            runtime_context=runtime_context,
        )
    if handle is not None:
        grant = ledger.resolve(handle)
        if grant is None:
            return _unknown_handle(handle, ledger)
        denied = [sid for sid in requested_sids if not grant.allows(sid)]
        if denied:
            return _denied_sid(handle, denied[0])
    groups: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for sid in requested_sids:
        grant_for_sid = ledger.resolve_sid(sid)
        if grant_for_sid is None:
            unresolved.append(sid)
            continue
        groups.setdefault(grant_for_sid.oid, []).append(sid)
    if unresolved:
        return _sid_not_granted(ledger, unresolved, requested_sids)
    if not groups:
        return _sid_not_granted(ledger, requested_sids, requested_sids)
    return _read_grant_segments(
        ledger,
        None,
        handle=handle,
        sids=requested_sids,
        max_tokens=max_tokens,
        groups=groups,
        runtime_context=runtime_context,
    )


def _read_grant_segments(
    ledger: CapabilityLedger,
    grant: Grant | None,
    *,
    handle: str | None,
    sids: list[str] | None,
    max_tokens: int,
    groups: dict[str, list[str]] | None = None,
    runtime_context: ToolRuntimeContext | None = None,
) -> str:
    """Read and render one object (legacy) or multiple sid groups (new path)."""

    store = ledger.object_store or ContextObjectStore()
    owner_user = ledger.user_id or (
        str(runtime_context.user_id or "") if runtime_context else ""
    )
    owner_thread = ledger.thread_id or (
        str(runtime_context.thread_id or "") if runtime_context else ""
    )
    if groups is None:
        if grant is None:
            return json.dumps(
                {
                    "error": "internal_grant_missing",
                    "instruction": "当前 sid 未能映射到本轮授权对象。",
                },
                ensure_ascii=False,
            )
        groups = {grant.oid: list(sids or [])}
    effective_groups: dict[str, list[str]] = {
        oid: sids for oid, sids in groups.items() if str(oid or "").strip()
    }
    if not effective_groups:
        return json.dumps(
            {
                "error": "segment_not_found",
                "handle": handle,
                "sids": sids,
                "instruction": "指定的 sid 未能映射到本轮授权对象，请核对【证据台账】。",
            },
            ensure_ascii=False,
        )
    # 池耗尽保底:context_read 是模型定向取证的最后入口,预算被共享池清零
    # 时静默返回空 content 会被误读为\u201c证据不存在\u201d(且无任何 error 字段)。
    # 给有界保底配额(仍经 reserve 计费,池计数饱和于上限),并在响应里
    # 显式携带 pool_exhausted。
    pool_exhausted = ledger.remaining_tokens <= 0
    budget = min(
        max(128, int(max_tokens or 2000)),
        ledger.max_tokens_per_read,
        max(ledger.remaining_tokens, _EXHAUSTED_READ_FLOOR_TOKENS),
    )
    remaining_budget = max(0, budget)
    rendered_blocks: list[str] = []
    spent = 0
    truncated = False
    deduped_sids: list[str] = []
    dedup_hit = False
    requested_sids = list(sids or [])
    remaining_groups = len(effective_groups)
    for oid, group_sids in effective_groups.items():
        obj = store.read(oid)
        if obj is None:
            # The object was garbage-collected (TTL) or its storage is gone.
            # Reporting this as an ownership/authorization failure sent the
            # model into a dead end: the body is unrecoverable here, so a
            # bounded re-retrieval is the honest recovery path.
            return json.dumps(
                {
                    "error": "object_expired",
                    "handle": handle,
                    "sids": group_sids,
                    "instruction": (
                        "该证据对象已过期并被清理，正文无法取回。"
                        "请优先基于本轮台账中其他证据作答；"
                        "若该正文确属必需，可就该主题重新发起一次知识库检索。"
                    ),
                },
                ensure_ascii=False,
            )
        origin = dict(obj.get("origin") or {})
        owners = list(obj.get("owners") or [])
        if not context_owner_allowed(
            origin,
            owners,
            user_id=owner_user,
            thread_id=owner_thread,
        ):
            return json.dumps(
                {
                    "error": "artifact_access_denied",
                    "handle": handle,
                    "sids": group_sids,
                    "instruction": "对象未通过当前用户/线程的 owner 校验。",
                },
                ensure_ascii=False,
            )
        already_read = [
            sid for sid in group_sids if sid in ledger.navigator_read_sids
        ]
        fresh_sids = [
            sid for sid in group_sids if sid not in ledger.navigator_read_sids
        ]
        if already_read:
            pointer = "【证据已读取（见上文）】" + "、".join(
                f"sid={sid}" for sid in already_read
            )
            pointer_cost = ledger.count_tokens(pointer)
            spent += ledger.reserve(pointer_cost)
            rendered_blocks.append(pointer)
            deduped_sids.extend(already_read)
            dedup_hit = True
        if not fresh_sids:
            remaining_groups = max(1, remaining_groups - 1)
            continue
        segments = store.read_segments(oid, fresh_sids)
        if not segments:
            return json.dumps(
                {
                    "error": "segment_not_found",
                    "handle": handle,
                    "sids": group_sids,
                    "instruction": "指定的片段在当前对象中不存在，请核对【证据台账】中的 sid。",
                },
                ensure_ascii=False,
            )
        # Divide by the groups still to render, not the original total:
        # dividing by the total let earlier groups' spending shrink later
        # groups' quota below their fair share.
        per_group_budget = max(
            1,
            remaining_budget // max(1, remaining_groups),
        )
        result = ledger.compiler.render_navigator(
            segments,
            handle=handle or "",
            budget_tokens=per_group_budget,
            turn_id=ledger.turn_id,
        )
        actual = ledger.reserve(result.tokens_spent)
        spent += actual
        rendered_blocks.append(result.rendered)
        remaining_budget = max(0, budget - spent)
        remaining_groups = max(1, remaining_groups - 1)
        total_segment_tokens = sum(
            int(segment.get("tokens") or 0) for segment in segments
        )
        group_truncated = actual < total_segment_tokens
        truncated = truncated or group_truncated
        if not group_truncated:
            ledger.mark_navigator_sids_read(fresh_sids)
        dedup_hit = dedup_hit or bool(result.receipt.get("omitted_sids"))
    ledger.record_navigator_call(dedup_hit=dedup_hit)
    payload: dict[str, Any] = {
        "handle": handle,
        "sids": requested_sids,
        "content": "\n".join(rendered_blocks),
        "deduped_sids": deduped_sids,
        "tokens_spent": spent,
        "tokens_budget": budget,
        "truncated": truncated,
    }
    if pool_exhausted:
        payload["pool_exhausted"] = True
        payload["instruction"] = (
            "共享投影池已耗尽,本次读取使用了有界保底配额;"
            "请优先基于已有证据作答,避免继续扩大读取范围。"
        )
    return json.dumps(payload, ensure_ascii=False)


@tool("context_read")
def context_read(
    sids: list[str],
    handle: str | None = None,
    max_tokens: int = 2000,
) -> str:
    """按 sid 读取本轮证据正文片段。

    Args:
        sids: 需要取回的片段 ID 列表，必须来自【证据台账】。
        handle: 兼容参数；旧版 obj:xxxx 句柄，可省略。
        max_tokens: 本次读取允许注入的最大 token 数。
    """
    return _context_read_for_scope(
        sids,
        handle,
        max_tokens,
        runtime_context=None,
    )


def get_scoped_context_read_tool(
    runtime_context: ToolRuntimeContext | None = None,
) -> StructuredTool:
    """Materialize the navigator with request identity bound."""

    def _scoped_context_read(
        sids: list[str],
        handle: str | None = None,
        max_tokens: int = 2000,
    ) -> str:
        return _context_read_for_scope(
            sids,
            handle,
            max_tokens,
            runtime_context=runtime_context,
        )

    return StructuredTool.from_function(
        func=_scoped_context_read,
        name="context_read",
        description=(
            "按【证据台账】中的 sid 读取本轮证据正文。"
            "sids 必须来自本轮动态上下文；句柄为兼容旧调用的可选参数。"
            "跨轮 sid 只有当同一证据本轮仍被授权时才能解析；"
            "解析失败时禁止重新发起知识库检索。"
        ),
    )


__all__ = [
    "capability_ledger_scope",
    "context_read",
    "get_capability_ledger",
    "get_scoped_context_read_tool",
    "install_request_capability_ledger",
    "reset_request_capability_ledger",
    "set_request_capability_ledger",
    "uninstall_request_capability_ledger",
]
