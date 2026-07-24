"""Shared LangGraph streaming runtime for profiled agents."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError

from backend.application.agent_executors.evidence_convergence import (
    EvidenceConvergenceController,
    EvidenceConvergencePolicy,
)
from backend.application.agent_executors.writer_stage import (
    EvidenceWriterStage,
    WriterStageConfig,
)
from backend.application.retrieval.query_resolution import (
    reset_retrieval_turn_context,
    set_retrieval_turn_context,
)
from backend.core.agents.control import (
    AgentRuntimeManager,
    RunBudget,
    get_agent_runtime_manager,
    reset_agent_runtime_manager,
    set_agent_runtime_manager,
)
from backend.core.agents.runtime_manager import (
    create_agent_runtime_manager,
    unregister_agent_runtime_manager,
)
from backend.core.skills.skill_runtime_state import (
    get_missing_required_skills,
    reset_request_skill_state,
    set_request_skill_state,
)
from backend.core.tools.request_limits import (
    reset_subagent_execution_limit,
    reset_tool_budgets,
    reset_turn_kb_context_budget,
    reset_web_search_max_results,
    set_subagent_execution_limit,
    set_tool_budgets,
    set_web_search_max_results,
)
from backend.core.utils.async_tasks import close_async_iterator_safely
from backend.domain.agents.retrieval_policy import RetrievalRuntimePolicy

logger = logging.getLogger(__name__)

EventAsyncIterator = AsyncIterator[dict[str, Any]]
ArtifactEmitter = Callable[..., EventAsyncIterator]
DeepResearchFinalizer = Callable[..., EventAsyncIterator]
HistoryImageResolver = Callable[[RunnableConfig], Any]


def _positive_timeout_env(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


_STREAM_ITERATOR_CLEANUP_TIMEOUT_SECONDS = _positive_timeout_env(
    "AGENT_STREAM_ITERATOR_CLEANUP_TIMEOUT_SECONDS",
    5.0,
)
_STREAM_AGENT_DRAIN_TIMEOUT_SECONDS = _positive_timeout_env(
    "AGENT_STREAM_DRAIN_TIMEOUT_SECONDS",
    15.0,
)
# Compatibility knob retained for deployments/tests that patched the former
# single cleanup timeout. New deployments should use the two scoped settings.
_STREAM_CLEANUP_TIMEOUT_SECONDS = _STREAM_AGENT_DRAIN_TIMEOUT_SECONDS


def _iterator_cleanup_timeout() -> float:
    return min(
        _STREAM_ITERATOR_CLEANUP_TIMEOUT_SECONDS,
        _STREAM_CLEANUP_TIMEOUT_SECONDS,
    )


def _agent_drain_timeout() -> float:
    return min(_STREAM_AGENT_DRAIN_TIMEOUT_SECONDS, _STREAM_CLEANUP_TIMEOUT_SECONDS)


def _optional_budget_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _optional_budget_float(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


async def wait_for_stream_event(
    event_iter: AsyncIterator[dict[str, Any]],
    pending_task: asyncio.Task[dict[str, Any]] | None,
    *,
    timeout: float,
) -> tuple[dict[str, Any] | None, asyncio.Task[dict[str, Any]] | None]:
    """Wait for one event without cancelling the upstream iterator on heartbeat."""
    task = pending_task or asyncio.create_task(event_iter.__anext__())
    try:
        event = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    # Python 3.10 keeps asyncio.TimeoutError distinct from builtin TimeoutError.
    except asyncio.TimeoutError:  # noqa: UP041
        return None, task
    return event, None


async def cancel_pending_stream_event(
    task: asyncio.Task[Any] | None,
) -> bool:
    """Request cancellation without letting an uncooperative read block cleanup."""

    if task is None:
        return True
    if not task.done():
        task.cancel()
        done, _ = await asyncio.wait(
            {task},
            timeout=_iterator_cleanup_timeout(),
        )
        if not done:
            logger.warning("取消待处理事件读取任务超时，继续执行根请求清理")
            task.add_done_callback(_consume_background_task_result)
            return False
    _consume_background_task_result(task)
    return True


def _consume_background_task_result(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    except Exception:
        logger.debug("后台流任务清理时忽略异常", exc_info=True)


async def close_stream_event_iterator(
    event_iter: Any,
    *,
    active_read_task: asyncio.Task[Any] | None = None,
) -> bool:
    """Bound ``aclose`` so root lifecycle cleanup cannot be held indefinitely."""

    return await close_async_iterator_safely(
        event_iter,
        active_read_task=active_read_task,
        timeout=_iterator_cleanup_timeout(),
    )


async def drain_agent_control(
    control: AgentRuntimeManager,
    *,
    label: str,
) -> bool:
    """Bound root teardown and report descendants that outlive the request."""

    try:
        await control.wait(timeout=_agent_drain_timeout())
        return True
    except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
        try:
            await control.interrupt(
                control.root_agent_id,
                reason="agent runtime drain timeout",
            )
        except BaseException:
            logger.exception("[%s] 二次取消未排空子智能体失败", label)
        remaining = [info for info in await control.list() if not info.status.terminal]
        logger.warning(
            "[%s] 根请求清理等待子智能体退出超时: %s",
            label,
            [
                {
                    "agent_id": info.agent_id,
                    "parent_id": info.parent_id,
                    "status": info.status.value,
                }
                for info in remaining
            ],
        )
        return False


async def close_owned_agent_runtime(
    manager: AgentRuntimeManager,
    *,
    label: str,
    status: str,
    error: str | None,
    suppress_errors: bool,
    requested_cancelled: bool | None = None,
    cancellation_reason: str | None = None,
) -> None:
    """Interrupt, drain, and durably finalize one owned request tree."""

    try:
        if requested_cancelled is None:
            requested_cancelled = bool(manager.cancellation.cancelled)
        if cancellation_reason is None:
            cancellation_reason = manager.cancellation.reason
        failures: list[BaseException] = []
        drained = True
        try:
            await manager.interrupt(
                manager.root_agent_id,
                reason="root request stream closed",
            )
        except BaseException as exc:
            failures.append(exc)
            logger.exception("[%s] 中断根 AgentRuntimeManager 失败", label)
        try:
            drained = await drain_agent_control(manager, label=label)
        except BaseException as exc:
            failures.append(exc)
            drained = False
            logger.exception("[%s] 等待 AgentRuntimeManager 排空失败", label)

        final_status = status
        final_error = error
        if status == "completed" and requested_cancelled:
            final_status = "cancelled"
            final_error = cancellation_reason or "request cancelled"
        if status == "completed" and not drained:
            final_status = "interrupted"
            final_error = "runtime manager drain timed out"
        finalize = getattr(manager, "finalize_run", None)
        if callable(finalize):
            try:
                await finalize(final_status, error=final_error)
            except BaseException as exc:
                failures.append(exc)
                logger.exception("[%s] 持久化 AgentRuntimeManager 终态失败", label)
        if failures and not suppress_errors:
            raise failures[0]
    finally:
        unregister_agent_runtime_manager(manager.root_agent_id, manager)


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
            try:
                try:
                    async for event in self._aquery_stream_impl(
                        question=question,
                        thread_id=thread_id,
                        config=config,
                        image_contents=image_contents,
                    ):
                        await event_queue.put(("event", event))
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    await event_queue.put(("error", exc))
                else:
                    await event_queue.put(("done", None))
            finally:
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

    async def _aquery_stream_impl(
        self,
        *,
        question: str,
        thread_id: str | None = None,
        config: RunnableConfig | None = None,
        image_contents: list[dict] | None = None,
    ) -> EventAsyncIterator:
        from backend.application.agent_executors.stream_handler import (
            NEAgentStreamHandler,
        )
        from backend.application.agent_executors.stream_timeouts import (
            INCOMPLETE_ROUND_STREAM_RETRY_MAX,
            MODEL_ROUND_IDLE_TIMEOUT_SECONDS,
            STREAM_HEARTBEAT_INTERVAL_SECONDS,
            STREAM_IDLE_TIMEOUT_SECONDS,
            resolve_normal_stream_idle_timeout,
        )

        cfg = self._config
        effective_config = dict(config or {})
        effective_config["configurable"] = dict(
            effective_config.get("configurable") or {}
        )

        effective_thread_id = thread_id or str(uuid.uuid4())
        effective_config["configurable"]["thread_id"] = effective_thread_id
        effective_config["configurable"]["kb_ids"] = cfg.kb_ids
        effective_config["configurable"]["use_search"] = cfg.use_search
        effective_config["configurable"]["subagent_enabled"] = cfg.subagent_enabled
        effective_config["configurable"][
            "max_concurrent_subagents"
        ] = cfg.max_concurrent_subagents
        effective_config["configurable"]["domain"] = cfg.request_metadata.get(
            "_domain", cfg.prompt_domain
        )
        effective_config["configurable"]["workflow_name"] = (
            "lightweight" if cfg.lightweight_mode else cfg.task_type or "full"
        )
        effective_config["configurable"]["tool_policy"] = cfg.request_metadata.get(
            "_tool_policy", ""
        )
        effective_config["configurable"]["guardrail_policy"] = cfg.guardrail_policy
        effective_config["configurable"]["runtime_permissions"] = dict(
            cfg.runtime_permissions
        )
        effective_config["configurable"]["request_id"] = cfg.request_metadata.get(
            "_request_id", ""
        )
        effective_config["configurable"]["user_id"] = cfg.user_id
        effective_config["configurable"]["agent_code"] = cfg.request_metadata.get(
            "_agent_code", ""
        )
        effective_config["configurable"]["time_context"] = cfg.time_context
        effective_config["configurable"]["request_metadata"] = dict(
            cfg.request_metadata
        )
        if cfg.context_policy is not None:
            effective_config["configurable"]["context_policy"] = cfg.context_policy
        if cfg.checkpointer is not None:
            # The task delegation tool builds an isolated child graph at call
            # time. Pass the same durable saver through runtime config so the
            # child does not silently lose checkpoint/resume semantics.
            effective_config["configurable"]["agent_checkpointer"] = cfg.checkpointer
        if cfg.runtime_agent_specs:
            effective_config["configurable"]["runtime_agent_specs"] = dict(
                cfg.runtime_agent_specs
            )
        metadata_domain_context = cfg.request_metadata.get("_domain_context", {})
        effective_config["configurable"]["domain_context"] = dict(
            metadata_domain_context
            if isinstance(metadata_domain_context, dict)
            else (
                cfg.runtime_permissions.get("domain_context", {})
                if isinstance(cfg.runtime_permissions.get("domain_context"), dict)
                else {}
            )
        )
        if cfg.task_type:
            effective_config["configurable"]["task_type"] = cfg.task_type
        if cfg.selected_skills is not None:
            effective_config["configurable"]["selected_skills"] = sorted(
                cfg.selected_skills
            )

        effective_config["metadata"] = dict(effective_config.get("metadata") or {})
        effective_config["metadata"]["model_name"] = cfg.model_name
        # Groundwater questions commonly require several table joins and a
        # standards lookup.  Give them more room than generic chat, while the
        # evidence convergence prevents an unbounded query loop.
        default_recursion_limit = 160 if cfg.prompt_domain == "groundwater" else 100
        # A coordinated role supplies a smaller recursion limit derived from
        # its manifest ``max_turns``.  Preserving that caller limit is what
        # prevents a research branch from silently expanding into a long
        # search loop.  Standalone requests still receive the broad default.
        if "recursion_limit" not in effective_config:
            try:
                effective_config["recursion_limit"] = max(
                    50,
                    int(
                        os.getenv(
                            "GROUNDWATER_GRAPH_RECURSION_LIMIT",
                            str(default_recursion_limit),
                        )
                    ),
                )
            except (TypeError, ValueError):
                effective_config["recursion_limit"] = default_recursion_limit

        stream_started_at = time.monotonic()
        first_event_delay_ms: int | None = None
        first_model_event_delay_ms: int | None = None
        first_chunk_delay_ms: int | None = None
        logger.info(
            "[%s] 开始流式查询, thread_id=%s, model=%s, thinking=%s, search=%s, subagent=%s",
            cfg.label,
            effective_thread_id,
            cfg.model_name,
            cfg.thinking_enabled,
            cfg.use_search,
            cfg.subagent_enabled,
        )

        from backend.application.agent_executors.image_url_registry import (
            reset_registry,
        )

        is_coordinated = bool((cfg.request_metadata or {}).get("coordination_workflow"))
        if not is_coordinated:
            reset_registry()
        subagent_execution_token = set_subagent_execution_limit(
            cfg.max_concurrent_subagents
        )
        # One control plane is shared by the root stream and every delegated
        # task created from it.  The legacy semaphore remains installed as a
        # compatibility fallback for direct task-tool callers, but task_tool
        # prefers this richer lifecycle controller when both are present.
        active_subagent_limit = max(
            1,
            min(5, int(cfg.max_concurrent_subagents)),
        )
        runtime_manager = get_agent_runtime_manager()
        owns_runtime_manager = runtime_manager is None
        runtime_manager_token = None
        if runtime_manager is None:
            tree_budget = cfg.run_budget or RunBudget(
                max_spawned_agents=16,
                max_active_agents=active_subagent_limit,
                max_depth=1,
                max_tokens=_optional_budget_int("AGENT_TREE_MAX_TOKENS"),
                max_tool_calls=_optional_budget_int("AGENT_TREE_MAX_TOOL_CALLS"),
                max_elapsed_seconds=_optional_budget_float(
                    "AGENT_TREE_MAX_ELAPSED_SECONDS"
                ),
                deadline_at=_optional_budget_float("AGENT_TREE_DEADLINE_AT"),
            )
            runtime_manager = create_agent_runtime_manager(
                root_agent_id=str(
                    effective_config["configurable"].get("request_id")
                    or f"{effective_thread_id}:{uuid.uuid4()}"
                ),
                budget=tree_budget,
                run_metadata={
                    "request_id": effective_config["configurable"].get(
                        "request_id",
                        "",
                    ),
                    "user_id": cfg.user_id or "",
                    "thread_id": effective_thread_id,
                    "workflow": (
                        "lightweight"
                        if cfg.lightweight_mode
                        else cfg.task_type or "full"
                    ),
                    "model_name": cfg.model_name,
                },
            )
            runtime_manager_token = set_agent_runtime_manager(runtime_manager)
        run_status = "interrupted"
        run_error: str | None = None
        active_failure: BaseException | None = None

        if cfg.lightweight_mode:
            from backend.application.agent_executors.lightweight_qa_agent import (
                LightweightQAAgent,
            )

            reset_turn_kb_context_budget()
            lightweight_policy = RetrievalRuntimePolicy.for_mode(lightweight=True)
            budget_token = set_tool_budgets(
                {
                    **lightweight_policy.tool_budgets(),
                    "multi_kb_search": cfg.multi_kb_search_max_calls,
                }
            )
            skill_state_token = set_request_skill_state(cfg.selected_skills)
            try:
                lightweight_agent = LightweightQAAgent(
                    graph=cfg.graph,
                    model_name=cfg.model_name,
                    user_id=cfg.user_id,
                    kb_ids=list(cfg.kb_ids),
                    use_search=cfg.use_search,
                    node_name="model",
                    thread_id=cfg.lightweight_thread_id,
                    uploaded_files_thread_id=cfg.lightweight_external_thread_id,
                    thinking_enabled=cfg.thinking_enabled,
                    prompt_domain=cfg.prompt_domain,
                    prompt_profile=cfg.prompt_profile,
                    prompt_provider=cfg.prompt_provider,
                    selected_skills=set(cfg.selected_skills or ()),
                    domain=str(
                        cfg.request_metadata.get("_domain") or cfg.prompt_domain
                    ),
                    guardrail_policy=cfg.guardrail_policy,
                    runtime_permissions=dict(cfg.runtime_permissions),
                    guardrail_instructions=cfg.guardrail_instructions,
                    time_context=cfg.time_context,
                    allowed_tools=list(cfg.lightweight_allowed_tools),
                    runtime_tools=dict(cfg.runtime_tools),
                    request_metadata=dict(cfg.request_metadata),
                    context_policy=cfg.context_policy,
                    compaction_checkpoints=cfg.compaction_checkpoints,
                    use_direct_query=str(cfg.lightweight_thread_id or "").startswith(
                        "explore__"
                    ),
                )
                async for event in lightweight_agent.aquery_stream(
                    question=question,
                    effective_config=effective_config,
                    stream_started_at=stream_started_at,
                    image_contents=image_contents,
                ):
                    yield event
                run_status = "completed"
            except asyncio.CancelledError as exc:
                run_status = "cancelled"
                run_error = "root request stream cancelled"
                active_failure = exc
                raise
            except GeneratorExit as exc:
                run_status = "interrupted"
                run_error = "root request stream consumer closed"
                active_failure = exc
                raise
            except BaseException as exc:
                run_status = "failed"
                run_error = str(exc) or type(exc).__name__
                active_failure = exc
                raise
            finally:
                try:
                    if owns_runtime_manager:
                        await close_owned_agent_runtime(
                            runtime_manager,
                            label=cfg.label,
                            status=run_status,
                            error=run_error,
                            suppress_errors=active_failure is not None,
                        )
                finally:
                    try:
                        reset_tool_budgets(budget_token)
                        missing_skills = get_missing_required_skills()
                        if missing_skills:
                            logger.warning(
                                "[%s] 轻量模式结束仍有未加载 Skills: %s",
                                cfg.label,
                                sorted(missing_skills),
                            )
                        reset_request_skill_state(skill_state_token)
                        reset_subagent_execution_limit(subagent_execution_token)
                    finally:
                        if runtime_manager_token is not None:
                            reset_agent_runtime_manager(runtime_manager_token)
            return

        is_coordinated = bool((cfg.request_metadata or {}).get("coordination_workflow"))
        if not is_coordinated:
            reset_registry()
        reset_turn_kb_context_budget()

        configurable = effective_config.get("configurable", {})
        runtime_task_type = str(
            configurable.get("task_type") or cfg.task_type or ""
        ).strip()
        deep_research_mode = runtime_task_type == cfg.deep_research_task_type
        deep_research_timeout_budget = float(cfg.deep_research_stream_timeout_seconds)
        deep_research_finalize_budget_applied = False
        last_upstream_event_at = stream_started_at
        handler = NEAgentStreamHandler(
            is_deep_research=deep_research_mode,
            thread_id=effective_thread_id,
            question=question,
            max_output_chars=cfg.normal_max_output_chars,
            selected_skills=cfg.selected_skills,
            kb_ids=cfg.kb_ids,
            use_search=cfg.use_search,
            capture_tool_evidence=is_coordinated,
        )

        runtime_retrieval_policy = RetrievalRuntimePolicy.for_mode(
            deep_research=deep_research_mode,
            academic_enabled=bool(
                cfg.selected_skills and "academic-search" in cfg.selected_skills
            ),
        )
        runtime_budgets = runtime_retrieval_policy.tool_budgets()
        if not deep_research_mode:
            runtime_budgets["multi_kb_search"] = cfg.multi_kb_search_max_calls
        coordination_metadata = dict(cfg.request_metadata or {})
        if coordination_metadata.get("coordination_workflow") == "deep_research":
            try:
                coordinated_web_calls = int(
                    coordination_metadata.get("coordination_web_search_max_calls")
                    or runtime_budgets.get("web_search")
                    or 1
                )
            except (TypeError, ValueError):
                coordinated_web_calls = runtime_budgets.get("web_search", 1)
            runtime_budgets["web_search"] = max(1, coordinated_web_calls)
        if not (
            deep_research_mode
            or (cfg.selected_skills and "academic-search" in cfg.selected_skills)
        ):
            runtime_budgets.pop("academic_search", None)
        budget_token = set_tool_budgets(runtime_budgets)
        try:
            coordinated_web_max_results = int(
                coordination_metadata.get("coordination_web_search_max_results")
            )
        except (TypeError, ValueError):
            coordinated_web_max_results = None
        web_result_limit_token = set_web_search_max_results(coordinated_web_max_results)
        skill_state_token = set_request_skill_state(cfg.selected_skills)
        event_stream = None
        event_iter = None
        pending_event_task = None
        graph_recursion_error = False
        evidence_convergence = EvidenceConvergenceController.from_runtime(
            model_name=cfg.model_name,
            prompt_domain=cfg.prompt_domain,
            request_metadata=cfg.request_metadata,
            policy=cfg.evidence_convergence_policy,
            label=cfg.label,
        )

        try:
            yield {"type": "status", "content": "已接收问题，正在分析..."}
            if deep_research_mode:
                yield {
                    "type": "status",
                    "content": "深度研究模式已启用：将拆解任务、并行研究并放宽检索轮数。",
                }

            user_content = self._build_user_content(question, image_contents)
            logger.info(
                "[%s] 流式等待配置: idle=%ds model_round_idle=%ds heartbeat=%ds retry=%d",
                cfg.label,
                STREAM_IDLE_TIMEOUT_SECONDS,
                MODEL_ROUND_IDLE_TIMEOUT_SECONDS,
                STREAM_HEARTBEAT_INTERVAL_SECONDS,
                INCOMPLETE_ROUND_STREAM_RETRY_MAX,
            )

            stream_exit_reason = "unknown"
            max_stream_passes = (
                1 if deep_research_mode else (1 + INCOMPLETE_ROUND_STREAM_RETRY_MAX)
            )
            stream_pass = 0

            while stream_pass < max_stream_passes:
                if stream_pass == 0:
                    graph_input: dict | None = {
                        "messages": [{"role": "user", "content": user_content}]
                    }
                    trace_context = self._trace_context()
                    if trace_context:
                        graph_input["trace_context"] = trace_context
                    preloaded_skills = cfg.request_metadata.get(
                        "_coordination_preloaded_skills", {}
                    )
                    if isinstance(preloaded_skills, dict) and preloaded_skills:
                        graph_input["loaded_skills"] = dict(preloaded_skills)
                    project_skill = str(
                        cfg.request_metadata.get("_coordination_project_skill") or ""
                    ).strip()
                    if project_skill:
                        graph_input["project_skill"] = project_skill
                    project_bootstrap = cfg.request_metadata.get(
                        "_coordination_project_bootstrap", {}
                    )
                    if isinstance(project_bootstrap, dict) and project_bootstrap:
                        graph_input["project_bootstrap"] = dict(project_bootstrap)
                else:
                    if (
                        stream_exit_reason != "langgraph_stream_completed"
                        or not handler.has_incomplete_model_round()
                    ):
                        break
                    logger.warning(
                        "[%s] 模型轮次未完成，从 checkpoint 续跑 pass=%d/%d thread_id=%s",
                        cfg.label,
                        stream_pass,
                        INCOMPLETE_ROUND_STREAM_RETRY_MAX,
                        effective_thread_id,
                    )
                    yield {
                        "type": "status",
                        "content": "模型仍在生成中，正在延长等待并尝试续跑...",
                    }
                    graph_input = None
                    last_upstream_event_at = time.monotonic()

                event_stream = cfg.graph.astream_events(
                    graph_input,
                    config=effective_config,
                    version="v2",
                    stream_mode=["messages", "custom", "updates", "tasks"],
                )
                event_iter = event_stream.__aiter__()
                pending_event_task = None
                stream_exit_reason = "unknown"

                while True:
                    should_break_stream = False
                    try:
                        if deep_research_mode:
                            if (
                                handler.deep_research_grace_applied
                                and not handler.deep_research_active_task_ids
                            ):
                                idle_timeout = (
                                    cfg.deep_research_finalize_idle_timeout_seconds
                                )
                            else:
                                idle_timeout = cfg.deep_research_idle_timeout_seconds
                        else:
                            idle_timeout = resolve_normal_stream_idle_timeout(
                                handler=handler,
                                deep_research_mode=False,
                            )

                        if deep_research_mode:
                            elapsed_seconds = time.monotonic() - stream_started_at
                            remaining_total = (
                                deep_research_timeout_budget - elapsed_seconds
                            )
                            if remaining_total <= 0:
                                handler.timeout_reached = True
                                stream_exit_reason = "deep_research_total_timeout"
                                yield {
                                    "type": "status",
                                    "content": "深度研究执行超时，正在整理阶段性结果并强制生成研究报告...",
                                }
                                break
                            wait_timeout = min(idle_timeout, max(1.0, remaining_total))
                        else:
                            idle_elapsed = max(
                                0.0, time.monotonic() - last_upstream_event_at
                            )
                            remaining_idle = max(1.0, idle_timeout - idle_elapsed)
                            wait_timeout = min(
                                STREAM_HEARTBEAT_INTERVAL_SECONDS, remaining_idle
                            )

                        event, pending_event_task = await wait_for_stream_event(
                            event_iter,
                            pending_event_task,
                            timeout=wait_timeout,
                        )
                        if event is None:
                            raise asyncio.TimeoutError  # noqa: UP041
                    except StopAsyncIteration:
                        pending_event_task = None
                        stream_exit_reason = "langgraph_stream_completed"
                        break
                    except GraphRecursionError:
                        pending_event_task = None
                        graph_recursion_error = True
                        stream_exit_reason = "graph_recursion_limit"
                        handler.timeout_reached = True
                        logger.error(
                            "[%s] LangGraph 达到递归上限，停止继续调用工具: thread_id=%s tools=%s",
                            cfg.label,
                            effective_thread_id,
                            handler._tools_invoked,
                        )
                        yield {
                            "type": "status",
                            "content": "工具调用次数过多，已停止继续检索并基于现有证据生成答案...",
                        }
                        break
                    except asyncio.TimeoutError:  # noqa: UP041
                        if not deep_research_mode:
                            idle_elapsed = time.monotonic() - last_upstream_event_at
                            idle_timeout = resolve_normal_stream_idle_timeout(
                                handler=handler,
                                deep_research_mode=False,
                            )
                            if idle_elapsed < idle_timeout:
                                yield {
                                    "type": "ping",
                                    "timestamp": int(time.time() * 1000),
                                }
                                continue

                        if deep_research_mode:
                            now_monotonic = time.monotonic()
                            elapsed_seconds = now_monotonic - stream_started_at
                            if elapsed_seconds < deep_research_timeout_budget:
                                yield {
                                    "type": "status",
                                    "content": "深度研究仍在执行，正在汇总结果并准备保存报告...",
                                }
                                continue

                            task_stall_seconds = (
                                now_monotonic
                                - handler.deep_research_last_task_runtime_at
                            )
                            if (
                                handler.deep_research_active_task_ids
                                and task_stall_seconds
                                < cfg.deep_research_task_stall_grace_seconds
                            ):
                                yield {
                                    "type": "status",
                                    "content": "子任务仍在运行，继续等待并汇总最新研究结果...",
                                }
                                continue

                        handler.timeout_reached = True
                        stream_exit_reason = (
                            "deep_research_idle_timeout"
                            if deep_research_mode
                            else "idle_timeout"
                        )
                        yield {
                            "type": "status",
                            "content": (
                                "深度研究阶段长时间无新进展，正在强制收敛并生成研究报告..."
                                if deep_research_mode
                                else "当前流程长时间无新进展，正在提前收敛并返回阶段性结果..."
                            ),
                        }
                        break

                    last_upstream_event_at = time.monotonic()
                    kind = event.get("event")
                    name = event.get("name", "")

                    if first_event_delay_ms is None:
                        first_event_delay_ms = int(
                            (time.monotonic() - stream_started_at) * 1000
                        )
                        logger.info(
                            "[%s] 首个 LangGraph 事件耗时: %dms, kind=%s, name=%s",
                            cfg.label,
                            first_event_delay_ms,
                            kind,
                            name,
                        )
                    if (
                        kind == "on_chat_model_start"
                        and first_model_event_delay_ms is None
                    ):
                        first_model_event_delay_ms = int(
                            (time.monotonic() - stream_started_at) * 1000
                        )
                        logger.info(
                            "[%s] 首个模型事件耗时: %dms",
                            cfg.label,
                            first_model_event_delay_ms,
                        )
                    if handler.event_count <= 10 or kind == "on_chat_model_stream":
                        logger.debug(
                            "[%s] 事件 #%s: kind=%s, name=%s",
                            cfg.label,
                            handler.event_count + 1,
                            kind,
                            name,
                        )

                    async for sse in handler.handle(event):
                        if sse.get("type") == "chunk" and first_chunk_delay_ms is None:
                            first_chunk_delay_ms = int(
                                (time.monotonic() - stream_started_at) * 1000
                            )
                            logger.info(
                                "[%s] 首个输出 chunk 耗时: %dms",
                                cfg.label,
                                first_chunk_delay_ms,
                            )
                        yield sse
                        if not deep_research_mode and handler.output_truncated:
                            should_break_stream = True

                    if (
                        deep_research_mode
                        and handler.deep_research_grace_applied
                        and not deep_research_finalize_budget_applied
                    ):
                        deep_research_timeout_budget += (
                            cfg.deep_research_finalize_grace_seconds
                        )
                        deep_research_finalize_budget_applied = True

                    if kind in {
                        "on_tool_end",
                        "on_chain_end",
                        "on_custom_event",
                        "on_chain_stream",
                    }:
                        async for artifact_event in cfg.emit_graph_artifacts(
                            effective_config=effective_config,
                            effective_thread_id=effective_thread_id,
                            handler=handler,
                        ):
                            yield artifact_event

                    if kind == "on_tool_end":
                        evidence_converged = False
                        async for update in evidence_convergence.on_tool_event(
                            question=question,
                            state=handler,
                        ):
                            yield {
                                "type": "status",
                                "content": update.message,
                            }
                            evidence_converged = evidence_converged or update.converged
                        if evidence_converged:
                            stream_exit_reason = "evidence_converged"
                            break

                    if (
                        deep_research_mode
                        and (time.monotonic() - stream_started_at)
                        > deep_research_timeout_budget
                    ):
                        handler.timeout_reached = True
                        stream_exit_reason = "deep_research_budget_exceeded"
                        yield {
                            "type": "status",
                            "content": "深度研究执行超时，正在整理阶段性结果并强制生成研究报告...",
                        }
                        break

                    if should_break_stream:
                        stream_exit_reason = "output_truncated"
                        break

                pending_event_stopped = await cancel_pending_stream_event(
                    pending_event_task
                )
                if pending_event_stopped:
                    pending_event_task = None

                stream_pass += 1
                if (
                    stream_exit_reason != "langgraph_stream_completed"
                    or not handler.has_incomplete_model_round()
                ):
                    break

            handler.set_stream_exit_reason(stream_exit_reason)
            idle_since_last_event_ms = int(
                (time.monotonic() - last_upstream_event_at) * 1000
            )
            logger.info(
                "[%s] LangGraph 事件流结束 reason=%s thread_id=%s events=%d "
                "idle_since_last_event_ms=%d open_model_round=%s",
                cfg.label,
                stream_exit_reason,
                effective_thread_id,
                handler.event_count,
                idle_since_last_event_ms,
                handler.has_incomplete_model_round(),
            )
            if handler.has_incomplete_model_round():
                logger.warning(
                    "[%s] 检测到未完成的模型轮次: thread_id=%s started_rounds=%d ended_rounds=%d stream_exit=%s",
                    cfg.label,
                    effective_thread_id,
                    handler._model_generation_round,
                    len(handler._model_round_summaries),
                    stream_exit_reason,
                )

            if (
                not deep_research_mode
                and stream_exit_reason
                in {"evidence_converged", "graph_recursion_limit"}
                and bool(
                    handler.project_evidence_text or handler.retrieved_evidence_text
                )
            ):
                # The graph may keep running after evidence convergence and start
                # another model round. Close it before launching the no-tool writer.
                try:
                    iterator_to_close = (
                        event_iter if event_iter is not None else event_stream
                    )
                    await close_stream_event_iterator(
                        iterator_to_close,
                        active_read_task=pending_event_task,
                    )
                except Exception:
                    logger.debug(
                        "[%s] 证据收敛时关闭原事件流失败", cfg.label, exc_info=True
                    )
                finally:
                    pending_event_task = None
                    event_iter = None
                    event_stream = None

                writer_stage = EvidenceWriterStage(
                    WriterStageConfig(
                        label=cfg.label,
                        graph=cfg.graph,
                        model_name=cfg.model_name,
                        request_metadata=dict(cfg.request_metadata),
                    )
                )
                async for writer_event in writer_stage.stream(
                    question=question,
                    effective_config=effective_config,
                    handler=handler,
                ):
                    if (
                        writer_event.get("type") == "chunk"
                        and first_chunk_delay_ms is None
                    ):
                        first_chunk_delay_ms = int(
                            (time.monotonic() - stream_started_at) * 1000
                        )
                        logger.info(
                            "[%s] 首个撰写输出 chunk 耗时: %dms",
                            cfg.label,
                            first_chunk_delay_ms,
                        )
                    yield writer_event

            if deep_research_mode and not handler.report_saved:
                finalizer = cfg.finalize_deep_research_report(
                    question=question,
                    effective_thread_id=effective_thread_id,
                    handler=handler,
                )
                finalizer_iter = finalizer.__aiter__()
                finalize_deadline = time.monotonic() + max(
                    1,
                    cfg.deep_research_finalize_idle_timeout_seconds,
                )
                try:
                    while True:
                        remaining = finalize_deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        try:
                            event = await asyncio.wait_for(
                                finalizer_iter.__anext__(),
                                timeout=remaining,
                            )
                        except StopAsyncIteration:
                            break
                        yield event
                except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
                    handler.timeout_reached = True
                    logger.warning(
                        "[%s] 深度研究最终归档超过 %d 秒，已终止归档",
                        cfg.label,
                        cfg.deep_research_finalize_idle_timeout_seconds,
                    )
                finally:
                    close_finalizer = getattr(finalizer_iter, "aclose", None)
                    if close_finalizer is not None:
                        try:
                            await close_finalizer()
                        except Exception:
                            logger.debug(
                                "[%s] 关闭深度研究归档迭代器失败",
                                cfg.label,
                                exc_info=True,
                            )
                await cfg.resolve_history_image_tokens(effective_config)

            async for final_event in handler.finalize():
                yield final_event

            # ``handler.finalize`` can synthesize a user-visible fallback after
            # an unfinished model round or timeout.  LangGraph has no final AI
            # message in that path, so append the fallback explicitly; otherwise
            # a refresh only shows tool traces (or an empty conversation).
            if not deep_research_mode and (
                handler.has_incomplete_model_round() or handler.timeout_reached
            ):
                fallback_answer = "".join(handler.answer_fragments).strip()
                if fallback_answer:
                    try:
                        await asyncio.wait_for(
                            cfg.graph.aupdate_state(
                                effective_config,
                                {"messages": [AIMessage(content=fallback_answer)]},
                                as_node="model",
                            ),
                            timeout=3.0,
                        )
                    except Exception:
                        logger.warning(
                            "[%s] 中断回答兜底写入历史失败",
                            cfg.label,
                            exc_info=True,
                        )

            if not deep_research_mode:
                await cfg.resolve_history_image_tokens(effective_config)

            if (
                handler.timeout_reached
                and not handler.answer_fragments
                and not graph_recursion_error
            ):
                yield {
                    "type": "chunk",
                    "content": (
                        "深度研究主流程超时，已停止继续等待并返回当前可用阶段性结果。"
                        if deep_research_mode
                        else "当前流程超时，系统已提前结束本轮并返回可用阶段性结果；建议新建会话后重试。"
                    ),
                }

            total_ms = int((time.monotonic() - stream_started_at) * 1000)
            answer_len = len("".join(handler.answer_fragments))
            logger.info(
                "[%s] 流式查询完成, 共 %d 个事件, %d 个 chunk, answer_chars=%d "
                "visible_chars=%d timeout=%s truncated=%s, total=%dms, "
                "first_event=%sms, first_model=%sms, first_chunk=%sms",
                cfg.label,
                handler.event_count,
                handler.chunk_count,
                answer_len,
                handler._normal_output_chars,
                handler.timeout_reached,
                handler.output_truncated,
                total_ms,
                first_event_delay_ms,
                first_model_event_delay_ms,
                first_chunk_delay_ms,
            )
            if (
                cfg.warn_low_visible_output
                and not deep_research_mode
                and handler._normal_output_chars < 300
                and (handler._any_tool_executed or handler._model_generation_round > 0)
            ):
                logger.warning(
                    "[%s] 本轮可见输出偏少: thread_id=%s answer_chars=%d model_rounds=%d tools=%s html_ppt_ok=%s",
                    cfg.label,
                    effective_thread_id,
                    handler._normal_output_chars,
                    handler._model_generation_round,
                    handler._tools_invoked,
                    handler._html_ppt_succeeded,
                )
            run_status = "completed"
        except asyncio.CancelledError as exc:
            run_status = "cancelled"
            run_error = "root request stream cancelled"
            active_failure = exc
            raise
        except GeneratorExit as exc:
            run_status = "interrupted"
            run_error = "root request stream consumer closed"
            active_failure = exc
            raise
        except BaseException as exc:
            run_status = "failed"
            run_error = str(exc) or type(exc).__name__
            active_failure = exc
            raise
        finally:
            requested_cancelled = bool(runtime_manager.cancellation.cancelled)
            cancellation_reason = runtime_manager.cancellation.reason
            try:
                # Close the request tree before touching potentially
                # uncooperative upstream iterators.  Manager interruption is
                # non-blocking with respect to child task termination, so all
                # descendants receive cancellation even if stream cleanup
                # later reaches its bounded timeout.
                if owns_runtime_manager:
                    try:
                        await runtime_manager.interrupt(
                            runtime_manager.root_agent_id,
                            reason="root request stream closed",
                        )
                    except BaseException:
                        logger.exception(
                            "[%s] 首次中断根 AgentRuntimeManager 失败，清理阶段将重试",
                            cfg.label,
                        )
            finally:
                try:
                    try:
                        pending_event_stopped = await cancel_pending_stream_event(
                            pending_event_task
                        )
                        await close_stream_event_iterator(
                            event_iter if event_iter is not None else event_stream,
                            active_read_task=(
                                None if pending_event_stopped else pending_event_task
                            ),
                        )
                    finally:
                        if owns_runtime_manager:
                            await close_owned_agent_runtime(
                                runtime_manager,
                                label=cfg.label,
                                status=run_status,
                                error=run_error,
                                suppress_errors=active_failure is not None,
                                requested_cancelled=requested_cancelled,
                                cancellation_reason=cancellation_reason,
                            )
                finally:
                    try:
                        reset_tool_budgets(budget_token)
                        reset_web_search_max_results(web_result_limit_token)
                        missing_skills = get_missing_required_skills()
                        if missing_skills:
                            logger.warning(
                                "[%s] 本轮结束仍有未加载 Skills: %s",
                                cfg.label,
                                sorted(missing_skills),
                            )
                        reset_request_skill_state(skill_state_token)
                        reset_subagent_execution_limit(subagent_execution_token)
                    finally:
                        if runtime_manager_token is not None:
                            reset_agent_runtime_manager(runtime_manager_token)

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
