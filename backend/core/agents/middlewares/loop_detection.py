"""Middleware to detect and break repetitive tool call loops."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from typing_extensions import override

logger = logging.getLogger(__name__)

_DEFAULT_WARN_THRESHOLD = 3
_DEFAULT_HARD_LIMIT = 5
_DEFAULT_WINDOW_SIZE = 20
_DEFAULT_MAX_TRACKED_THREADS = 100
_LOOP_WARNING_MARKER = "loop_detection_warning"


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def _message_value(message: Any, key: str, default: Any = None) -> Any:
    if isinstance(message, Mapping):
        return message.get(key, default)
    return getattr(message, key, default)


def _message_type(message: Any) -> str:
    raw_type = str(
        _message_value(message, "type", None)
        or _message_value(message, "role", None)
        or ""
    ).lower()
    return {
        "assistant": "ai",
        "user": "human",
    }.get(raw_type, raw_type)


def _tool_calls(message: Any) -> list[dict[str, Any]]:
    raw_calls = _message_value(message, "tool_calls", None) or []
    calls: list[dict[str, Any]] = []
    for call in raw_calls:
        if isinstance(call, Mapping):
            calls.append(dict(call))
        else:
            calls.append(
                {
                    "name": getattr(call, "name", ""),
                    "args": getattr(call, "args", {}),
                }
            )
    return calls


def _hash_tool_calls(tool_calls: list[dict[str, Any]]) -> str:
    normalized: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        normalized.append(
            {
                "name": tool_call.get("name", ""),
                "args": tool_call.get("args", {}),
            }
        )
    normalized.sort(key=lambda item: (item["name"], _stable_json(item["args"])))
    return hashlib.sha256(_stable_json(normalized).encode()).hexdigest()[:16]


def _hash_tool_result(message: Any, originating_call_hash: str = "") -> str:
    """Hash the meaningful result, deliberately excluding per-call identifiers."""

    payload = {
        "originating_call": originating_call_hash,
        "name": _message_value(message, "name", ""),
        "status": _message_value(message, "status", ""),
        "content": _message_value(message, "content", None),
        "artifact": _message_value(message, "artifact", None),
    }
    return hashlib.sha256(_stable_json(payload).encode()).hexdigest()[:16]


def _hash_progress(result_hashes: set[str]) -> str:
    return hashlib.sha256(_stable_json(sorted(result_hashes)).encode()).hexdigest()[:16]


def _is_successful_tool_result(message: Any) -> bool:
    """Treat structured provider errors as failures even without error status.

    Several tools return JSON error payloads so the model can recover instead
    of raising an exception.  Counting those payloads as new evidence made a
    sequence of different failing tools look like progress and delayed loop
    convergence until the model-turn limit was exhausted.
    """
    if str(_message_value(message, "status", "") or "").casefold() == "error":
        return False
    content = _message_value(message, "content", None)
    if not isinstance(content, str):
        return True
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return True
    return not (isinstance(payload, Mapping) and payload.get("error"))


_WARNING_MSG = (
    "[LOOP DETECTED] You are repeating tool calls without making progress. "
    "Stop calling tools and produce your final answer now. "
    "If you cannot complete the task, summarize what you accomplished so far."
)
_LEGACY_WARNING_MSG = (
    "[LOOP DETECTED] You are repeating the same tool calls. "
    "Stop calling tools and produce your final answer now. "
    "If you cannot complete the task, summarize what you accomplished so far."
)

_HARD_STOP_MSG = (
    "[FORCED STOP] Repeated tool calls exceeded the safety limit. "
    "Producing final answer with results collected so far."
)


@dataclass(frozen=True)
class _ToolRound:
    message_index: int
    call_hash: str
    progress_hash: str


class LoopDetectionMiddleware(AgentMiddleware[AgentState]):
    """Detect consecutive repeated calls and consecutive no-progress rounds.

    Detection is reconstructed from the current state's messages instead of a
    process-global per-thread counter.  This makes independent runs isolated by
    construction and prevents calls from unrelated anonymous runs from sharing
    the former ``"default"`` history bucket.
    """

    def __init__(
        self,
        warn_threshold: int = _DEFAULT_WARN_THRESHOLD,
        hard_limit: int = _DEFAULT_HARD_LIMIT,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        max_tracked_threads: int = _DEFAULT_MAX_TRACKED_THREADS,
    ):
        super().__init__()
        self.warn_threshold = max(1, int(warn_threshold))
        self.hard_limit = max(self.warn_threshold, int(hard_limit))
        self.window_size = max(1, int(window_size))
        # Kept for constructor compatibility.  The middleware is now stateless
        # across runs, so there are no tracked thread entries to evict.
        self.max_tracked_threads = max(1, int(max_tracked_threads))

    @staticmethod
    def _context_value(container: Any, *keys: str) -> str | None:
        for key in keys:
            if isinstance(container, Mapping):
                value = container.get(key)
            else:
                value = getattr(container, key, None) if container is not None else None
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    def _execution_label(self, state: AgentState, runtime: Runtime | None) -> str:
        """Return a diagnostic run/thread label without a shared default key."""

        context = getattr(runtime, "context", None) if runtime is not None else None
        config = getattr(runtime, "config", None) if runtime is not None else None
        configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
        metadata = config.get("metadata", {}) if isinstance(config, Mapping) else {}
        trace_context = state.get("trace_context", {})

        thread_id = self._context_value(context, "thread_id", "internal_thread_id")
        thread_id = thread_id or self._context_value(configurable, "thread_id")
        run_id = self._context_value(context, "run_id", "request_id")
        run_id = run_id or self._context_value(
            configurable, "run_id", "request_id", "trace_run_id"
        )
        run_id = run_id or self._context_value(metadata, "run_id", "request_id")
        run_id = run_id or self._context_value(trace_context, "run_id", "request_id")

        identifiers = []
        if thread_id:
            identifiers.append(f"thread={thread_id}")
        if run_id:
            identifiers.append(f"run={run_id}")
        if identifiers:
            return ",".join(identifiers)
        owner = runtime if runtime is not None else state
        return f"anonymous={id(owner):x}"

    @staticmethod
    def _is_internal_warning(message: Any) -> bool:
        if _message_type(message) != "human":
            return False
        additional_kwargs = _message_value(message, "additional_kwargs", {})
        if isinstance(additional_kwargs, Mapping):
            if additional_kwargs.get(_LOOP_WARNING_MARKER):
                return True
        # Backward compatibility for checkpoints created before the marker was
        # introduced.
        return _message_value(message, "content", None) in {
            _WARNING_MSG,
            _LEGACY_WARNING_MSG,
        }

    def _recent_turn_segment(
        self, messages: Sequence[Any]
    ) -> list[tuple[int, Any]]:
        """Return at most ``window_size`` tool rounds from the current user turn."""

        segment_reversed: list[tuple[int, Any]] = []
        tool_rounds = 0
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if _message_type(message) == "human" and not self._is_internal_warning(message):
                break
            segment_reversed.append((index, message))
            if _message_type(message) == "ai" and _tool_calls(message):
                tool_rounds += 1
                if tool_rounds >= self.window_size:
                    break
        segment_reversed.reverse()
        return segment_reversed

    def _tool_rounds(self, messages: Sequence[Any]) -> list[_ToolRound]:
        result_hashes: set[str] = set()
        call_hash_by_id: dict[str, str] = {}
        rounds: list[_ToolRound] = []
        for message_index, message in self._recent_turn_segment(messages):
            message_type = _message_type(message)
            if message_type == "tool":
                tool_call_id = str(_message_value(message, "tool_call_id", "") or "")
                if _is_successful_tool_result(message):
                    result_hashes.add(
                        _hash_tool_result(message, call_hash_by_id.get(tool_call_id, ""))
                    )
                continue
            calls = _tool_calls(message) if message_type == "ai" else []
            if calls:
                for call in calls:
                    call_id = str(call.get("id") or "")
                    if call_id:
                        call_hash_by_id[call_id] = _hash_tool_calls([call])
                rounds.append(
                    _ToolRound(
                        message_index=message_index,
                        call_hash=_hash_tool_calls(calls),
                        progress_hash=_hash_progress(result_hashes),
                    )
                )
        return rounds

    @staticmethod
    def _trailing_streak(
        rounds: Sequence[_ToolRound], attribute: str
    ) -> tuple[int, int]:
        if not rounds:
            return 0, 0
        last_value = getattr(rounds[-1], attribute)
        start = len(rounds) - 1
        while start > 0 and getattr(rounds[start - 1], attribute) == last_value:
            start -= 1
        return len(rounds) - start, rounds[start].message_index

    def _warning_since(self, messages: Sequence[Any], start_index: int) -> bool:
        return any(
            self._is_internal_warning(message) for message in messages[start_index:]
        )

    def _track_and_check(
        self, state: AgentState, runtime: Runtime | None
    ) -> tuple[str | None, bool]:
        messages = state.get("messages", [])
        if not messages:
            return None, False
        last_message = messages[-1]
        if _message_type(last_message) != "ai" or not _tool_calls(last_message):
            return None, False

        rounds = self._tool_rounds(messages)
        repeated_calls, repeated_start = self._trailing_streak(rounds, "call_hash")
        no_progress, no_progress_start = self._trailing_streak(
            rounds, "progress_hash"
        )
        max_streak = max(repeated_calls, no_progress)
        reasons = []
        if repeated_calls >= self.warn_threshold:
            reasons.append(f"same_call={repeated_calls}")
        if no_progress >= self.warn_threshold:
            reasons.append(f"no_progress={no_progress}")

        tool_names = [call.get("name", "?") for call in _tool_calls(last_message)]
        execution = self._execution_label(state, runtime)
        if max_streak >= self.hard_limit:
            logger.error(
                "Loop hard limit reached - forcing stop (%s, tools=%s, %s)",
                execution,
                tool_names,
                ",".join(reasons),
            )
            return _HARD_STOP_MSG, True

        if reasons:
            qualifying_starts = []
            if repeated_calls >= self.warn_threshold:
                qualifying_starts.append(repeated_start)
            if no_progress >= self.warn_threshold:
                qualifying_starts.append(no_progress_start)
            warning_start = min(qualifying_starts)
            if not self._warning_since(messages, warning_start):
                logger.warning(
                    "Repetitive tool loop detected (%s, tools=%s, %s)",
                    execution,
                    tool_names,
                    ",".join(reasons),
                )
                return _WARNING_MSG, False
        return None, False

    @staticmethod
    def _append_text(content: str | list | None, text: str) -> str | list:
        if content is None:
            return text
        if isinstance(content, list):
            return [*content, {"type": "text", "text": f"\n\n{text}"}]
        if isinstance(content, str):
            return content + f"\n\n{text}"
        return str(content) + f"\n\n{text}"

    def _apply(self, state: AgentState, runtime: Runtime | None) -> dict | None:
        warning, hard_stop = self._track_and_check(state, runtime)
        if hard_stop:
            messages = state.get("messages", [])
            last_message = messages[-1]
            stripped_message = last_message.model_copy(
                update={
                    "tool_calls": [],
                    "content": self._append_text(last_message.content, _HARD_STOP_MSG),
                }
            )
            return {"messages": [stripped_message]}
        if warning:
            return {
                "messages": [
                    HumanMessage(
                        content=warning,
                        additional_kwargs={_LOOP_WARNING_MARKER: True},
                    )
                ]
            }
        return None

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    def reset(self, thread_id: str | None = None) -> None:
        """Compatibility no-op; run-local message state now owns detection history."""
        return None
