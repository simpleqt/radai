"""Shared LangGraph streaming runtime for profiled agents."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig

from backend.application.agent_executors.evidence_convergence import (
    EvidenceConvergencePolicy,
)
from backend.application.agent_executors.stream_timeouts import StreamTimeoutPolicy
from backend.core.utils.async_tasks import close_async_iterator_safely
from backend.application.retrieval.query_resolution import (
    reset_retrieval_turn_context,
    set_retrieval_turn_context,
)
from backend.core.agents.control import RunBudget

from backend.application.agent_executors.streaming_runtime_lifecycle import (  # noqa: F401  (含测试兼容 re-export)
    _agent_drain_timeout,
    _consume_background_task_result,
    _iterator_cleanup_timeout,
    _optional_budget_float,
    _optional_budget_int,
    cancel_pending_stream_event,
    close_owned_agent_runtime,
    close_stream_event_iterator,
    drain_agent_control,
    wait_for_stream_event,
)
from backend.application.agent_executors.streaming_runtime_impl import (
    _aquery_stream_impl as _impl_aquery_stream_impl,
)

logger = logging.getLogger(__name__)

EventAsyncIterator = AsyncIterator[dict[str, Any]]
ArtifactEmitter = Callable[..., EventAsyncIterator]
DeepResearchFinalizer = Callable[..., EventAsyncIterator]
HistoryImageResolver = Callable[[RunnableConfig], Any]


@dataclass(frozen=True)
class StreamRuntimeConfig:
    label: str
    graph: Any
    model_name: str
    user_id: str | None
    kb_ids: list[str]
    use_search: bool
    subagent_enabled: bool
    max_concurrent_subagents: int
    selected_skills: set[str] | None
    task_type: str | None
    lightweight_mode: bool
    lightweight_thread_id: str | None
    lightweight_external_thread_id: str | None
    thinking_enabled: bool
    prompt_domain: str
    guardrail_policy: str
    runtime_permissions: dict[str, Any]
    guardrail_instructions: str
    time_context: str
    lightweight_allowed_tools: list[str]
    runtime_tools: dict[str, Any]
    normal_max_output_chars: int
    multi_kb_search_max_calls: int
    deep_research_task_type: str
    deep_research_stream_timeout_seconds: int
    deep_research_idle_timeout_seconds: int
    deep_research_finalize_idle_timeout_seconds: int
    deep_research_task_stall_grace_seconds: int
    deep_research_finalize_grace_seconds: int
    emit_graph_artifacts: ArtifactEmitter
    finalize_deep_research_report: DeepResearchFinalizer
    resolve_history_image_tokens: HistoryImageResolver
    prompt_profile: str | None = None
    prompt_provider: Any | None = None
    request_metadata: dict[str, Any] = field(default_factory=dict)
    runtime_agent_specs: dict[str, Any] = field(default_factory=dict)
    warn_low_visible_output: bool = False
    run_budget: RunBudget | None = None
    context_policy: Any | None = None
    timeout_policy: StreamTimeoutPolicy | None = None
    evidence_convergence_policy: EvidenceConvergencePolicy | None = None
    compaction_checkpoints: Any | None = None
    checkpointer: Any | None = None


class AgentStreamRuntime:
    """Run the common LangGraph SSE loop used by full-feature agents."""

    def __init__(self, config: StreamRuntimeConfig):
        self._config = config

    async def aquery_stream(
        self,
        *,
        question: str,
        thread_id: str | None = None,
        config: RunnableConfig | None = None,
        image_contents: list[dict] | None = None,
    ) -> EventAsyncIterator:
        """Relay one request from a stable producer task.

        Some orchestrators call each ``__anext__`` from a fresh task. Context
        variables belong to tasks rather than async generators, so driving the
        implementation from one producer is what keeps request budgets, Skill
        state, and AgentRuntimeManager visible for the whole graph execution.
        """

        event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1)

        async def produce() -> None:
            retrieval_context_token = set_retrieval_turn_context(question)
            # 显式持有内层实现生成器:队列 maxsize=1,断连时 producer 多半阻塞在
            # put,取消 producer 会让 async for 从 put 处解栈但把 agen 弃置在
            # yield 点,其 finally(interrupt/drain≤15s/finalize_run/token reset)
            # 只能等 GC 的 asyncgen finalizer 在外来 Context 里补做——reset 抛
            # different-Context、清理无人等待、旧图仍在跑。改为在本任务 finally
            # 显式 aclose,使收尾始终在 producer 任务与正确 Context 内完成。
            agen = self._aquery_stream_impl(
                question=question,
                thread_id=thread_id,
                config=config,
                image_contents=image_contents,
            )
            try:
                try:
                    async for event in agen:
                        await event_queue.put(("event", event))
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    await event_queue.put(("error", exc))
                else:
                    await event_queue.put(("done", None))
            finally:
                # 15s drain + finalize 需要足够时限;close_async_iterator_safely
                # 内部以独立 close_task 驱动 aclose,即便本 finally 再遭取消,
                # aclose 仍在该 close_task 中带结果回收地跑完。
                await close_async_iterator_safely(agen, timeout=18.0)
                reset_retrieval_turn_context(retrieval_context_token)

        producer_task = asyncio.create_task(produce())
        queue_get_task: asyncio.Task[tuple[str, Any]] | None = None
        try:
            while True:
                queue_get_task = asyncio.create_task(event_queue.get())
                done, _ = await asyncio.wait(
                    {queue_get_task, producer_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if queue_get_task in done or not event_queue.empty():
                    kind, payload = await queue_get_task
                else:
                    # A producer-side cancellation does not pass through the
                    # normal queue protocol.  Observe the producer directly so
                    # the consumer cannot wait forever for a message that will
                    # never be written.
                    queue_get_task.cancel()
                    try:
                        await queue_get_task
                    except asyncio.CancelledError:
                        pass
                    producer_task.result()
                    return
                if kind == "event":
                    yield payload
                    continue
                if kind == "error":
                    raise payload
                return
        finally:
            # Deliver both cancellation signals before the first cleanup
            # await.  A relay can be cancelled again while it is already in
            # ``finally`` (for example disconnect followed by a deadline);
            # that second cancellation must not leave the producer running.
            if queue_get_task is not None and not queue_get_task.done():
                queue_get_task.cancel()
            if not producer_task.done():
                producer_task.cancel()
            await cancel_pending_stream_event(queue_get_task)
            await cancel_pending_stream_event(producer_task)

    # 实现在 streaming_runtime_impl.py(模块级函数,self 经 descriptor 绑定)
    _aquery_stream_impl = _impl_aquery_stream_impl

    def _trace_context(self) -> dict[str, Any]:
        metadata = dict(self._config.request_metadata or {})
        if not metadata.get("_trace_enabled"):
            return {}
        run_id = str(
            metadata.get("_trace_run_id") or metadata.get("_request_id") or ""
        ).strip()
        if not run_id:
            return {}
        trace_context: dict[str, Any] = {
            "enabled": True,
            "run_id": run_id,
            "workflow_name": metadata.get("coordination_workflow")
            or metadata.get("_workflow_name")
            or self._config.task_type
            or "full",
        }
        trace_root = metadata.get("agent_trace_root")
        if trace_root:
            trace_context["trace_root"] = str(trace_root)
        unit_id = metadata.get("coordination_task_id") or metadata.get(
            "coordination_role"
        )
        if unit_id:
            trace_context["unit_id"] = str(unit_id)
        return trace_context

    @staticmethod
    def _build_user_content(
        question: str, image_contents: list[dict] | None
    ) -> list[dict] | str:
        if not image_contents:
            return question
        user_content: list[dict] = [{"type": "text", "text": question}]
        for image in image_contents:
            if image.get("image_url"):
                user_content.append(
                    {"type": "image_url", "image_url": {"url": image["image_url"]}}
                )
                continue
            if image.get("base64"):
                b64 = image.get("base64", "")
                mime = image.get("mime_type", "image/png")
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    }
                )
        return user_content
