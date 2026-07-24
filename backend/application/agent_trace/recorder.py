"""Request-scoped trace recorder facade."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any

from backend.application.agent_trace.models import (
    AgentRunCompaction,
    AgentRunContextPack,
    AgentRunEvent,
    AgentRunHandoff,
    AgentRunToolCall,
    AgentRunTrace,
    AgentRunUnit,
    utc_now_iso,
)
from backend.application.agent_trace.store import (
    AgentTraceStoreProtocol,
    create_agent_trace_store,
)
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext


_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth(?:orization)?|client[_-]?secret|password|secret|token)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")


class AgentTraceRecorder:
    """Thin facade that keeps trace writes optional for workflow code."""

    def __init__(
        self,
        *,
        run_id: str,
        workflow_name: str,
        enabled: bool,
        store: AgentTraceStoreProtocol | None = None,
        request_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.workflow_name = workflow_name
        self.enabled = enabled
        self.request_id = str(request_id or "")
        self.thread_id = str(thread_id or "")
        self._store = store or create_agent_trace_store()
        self._run_finished = False

    @classmethod
    def from_context(
        cls,
        context: AgentRuntimeContext,
        *,
        workflow_name: str,
        store: AgentTraceStoreProtocol | None = None,
    ) -> AgentTraceRecorder:
        run_id = context.run_id or context.request_id
        trace_root = context.request_metadata.get("agent_trace_root")
        return cls(
            run_id=run_id,
            workflow_name=workflow_name,
            enabled=bool(context.trace_enabled and run_id),
            store=store
            or create_agent_trace_store(
                trace_root,
                backend="jsonl" if trace_root else None,
            ),
            request_id=context.request_id,
            thread_id=context.external_thread_id,
        )

    def record_run_started(
        self,
        *,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> None:
        if not self.enabled:
            return
        self._store.append_run(
            AgentRunTrace(
                run_id=self.run_id,
                request_id=context.request_id,
                user_id=context.user_id,
                thread_id=context.external_thread_id,
                agent_code=request.agent_code or context.agent_profile.code,
                workflow=self.workflow_name,
                model_name=context.model_name,
                selected_skills=list(context.selected_skills),
                tool_policy=str(context.permissions.get("tool_policy") or ""),
                status="running",
            )
        )

    def record_run_finished(
        self,
        *,
        request: AgentRequest,
        context: AgentRuntimeContext,
        status: str,
        error_code: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        if self._run_finished:
            return
        self._run_finished = True
        self._store.append_run(
            AgentRunTrace(
                run_id=self.run_id,
                request_id=context.request_id,
                user_id=context.user_id,
                thread_id=context.external_thread_id,
                agent_code=request.agent_code or context.agent_profile.code,
                workflow=self.workflow_name,
                model_name=context.model_name,
                selected_skills=list(context.selected_skills),
                tool_policy=str(context.permissions.get("tool_policy") or ""),
                status=status,
                ended_at=utc_now_iso(),
                error_code=error_code,
            )
        )

    def record_event(
        self,
        *,
        sequence: int,
        event_type: str,
        payload: dict[str, Any],
        unit_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        event = AgentEvent(
            type=event_type,
            request_id=self.request_id,
            thread_id=self.thread_id,
            sequence=sequence,
            payload=_json_safe(payload),
        )
        self.record_agent_event(event, unit_id=unit_id)

    def record_agent_event(
        self,
        event: AgentEvent,
        *,
        unit_id: str | None = None,
        causation_id: str | None = None,
        producer: str | None = None,
    ) -> None:
        """Persist the canonical V2 envelope while retaining trace fields."""

        if not self.enabled:
            return
        envelope = event.to_envelope(
            correlation_id=self.run_id,
            causation_id=causation_id,
            producer=producer or f"agent-workflow:{self.workflow_name}",
            unit_id=unit_id,
        )
        self._store.append_event(
            AgentRunEvent(
                run_id=self.run_id,
                sequence=envelope.sequence,
                event_type=envelope.type,
                unit_id=envelope.unit_id,
                payload=_json_safe(envelope.payload),
                created_at=envelope.created_at,
                event_id=envelope.event_id,
                schema_version=envelope.schema_version,
                request_id=envelope.request_id,
                thread_id=envelope.thread_id,
                correlation_id=envelope.correlation_id,
                causation_id=envelope.causation_id,
                producer=envelope.producer,
                idempotency_key=envelope.idempotency_key,
            )
        )

    def record_unit(
        self,
        *,
        unit_id: str,
        role: str,
        agent_type: str,
        task_title: str,
        task_question: str,
        status: str,
        started_at: str,
        timeout_seconds: int | None,
        tool_access: bool,
        parent_unit_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        self._store.append_unit(
            AgentRunUnit(
                unit_id=unit_id,
                run_id=self.run_id,
                role=role,
                agent_type=agent_type,
                task_title=_redact_text(task_title) or "",
                task_question=_redact_text(task_question) or "",
                parent_unit_id=parent_unit_id,
                status=status,
                started_at=started_at,
                ended_at=utc_now_iso(),
                timeout_seconds=timeout_seconds,
                tool_access=tool_access,
            )
        )

    def record_handoff(
        self,
        *,
        handoff_id: str,
        unit_id: str | None,
        role: str,
        payload: dict[str, Any],
    ) -> None:
        if not self.enabled:
            return
        self._store.append_handoff(
            AgentRunHandoff(
                handoff_id=handoff_id,
                run_id=self.run_id,
                unit_id=unit_id,
                role=role,
                payload=_json_safe(payload),
                schema_version=str(payload.get("schema_version") or "2.0"),
            )
        )

    def record_tool_call(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        status: str,
        started_at: str,
        ended_at: str | None = None,
        elapsed_ms: float | None = None,
        args_preview: dict[str, Any] | None = None,
        result_preview: str | None = None,
        error: str | None = None,
        unit_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        self._store.append_tool_call(
            AgentRunToolCall(
                tool_call_id=tool_call_id,
                run_id=self.run_id,
                tool_name=tool_name,
                status=status,
                unit_id=unit_id,
                started_at=started_at,
                ended_at=ended_at,
                elapsed_ms=elapsed_ms,
                args_preview=_json_safe(args_preview or {}),
                result_preview=_redact_text(result_preview),
                error=_redact_text(error),
                metadata=_json_safe(metadata or {}),
            )
        )

    def record_context_pack(
        self,
        *,
        pack_id: str,
        unit_id: str | None,
        model_call_index: int,
        base_tokens: int,
        dynamic_token_budget: int,
        dynamic_tokens_used: int,
        items_total: int,
        items_injected: int,
        selected_context_ids: list[str] | None = None,
        usage_by_category: dict[str, int] | None = None,
    ) -> None:
        if not self.enabled:
            return
        self._store.append_context_pack(
            AgentRunContextPack(
                pack_id=pack_id,
                run_id=self.run_id,
                unit_id=unit_id,
                model_call_index=model_call_index,
                base_tokens=base_tokens,
                dynamic_token_budget=dynamic_token_budget,
                dynamic_tokens_used=dynamic_tokens_used,
                items_total=items_total,
                items_injected=items_injected,
                selected_context_ids=list(selected_context_ids or []),
                usage_by_category=_json_safe(usage_by_category or {}),
            )
        )

    def record_compaction(
        self,
        *,
        compaction_id: str,
        unit_id: str | None,
        model_call_index: int,
        trigger: str,
        window_id: str,
        previous_window_id: str | None,
        tokens_before: int,
        tokens_after: int,
        summary_tokens: int,
        retained_user_messages: int,
        removed_messages: int,
        threshold_tokens: int = 0,
        fixed_request_tokens: int = 0,
        status: str = "completed",
        error: str | None = None,
        summary_preview: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        self._store.append_compaction(
            AgentRunCompaction(
                compaction_id=compaction_id,
                run_id=self.run_id,
                unit_id=unit_id,
                model_call_index=model_call_index,
                trigger=trigger,
                window_id=window_id,
                previous_window_id=previous_window_id,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                summary_tokens=summary_tokens,
                retained_user_messages=retained_user_messages,
                removed_messages=removed_messages,
                threshold_tokens=threshold_tokens,
                fixed_request_tokens=fixed_request_tokens,
                status=status,
                error=_redact_text(error),
                summary_preview=_redact_text(summary_preview),
            )
        )


def _json_safe(value: Any, *, key: str | None = None) -> Any:
    if key and _normalize_key(key) in _SENSITIVE_KEYS:
        return _REDACTED
    if is_dataclass(value):
        return _json_safe(asdict(value), key=key)
    if isinstance(value, dict):
        return {
            str(item_key): _json_safe(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _redact_text(value) if isinstance(value, str) else value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _normalize_key(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _redact_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = _BEARER_RE.sub("Bearer " + _REDACTED, text)
    text = _ASSIGNMENT_RE.sub(lambda match: match.group(1) + match.group(2) + _REDACTED, text)
    return _OPENAI_KEY_RE.sub(_REDACTED, text)
