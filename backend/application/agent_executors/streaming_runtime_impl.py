"""AgentStreamRuntime 主流式实现 _aquery_stream_impl(首参 self;拆自 streaming_runtime.py)。

2026-08-13 阶段化重写:原单个约 1000 行的异步生成器拆为编排器 +
四个阶段(配置装配 / 轻量分支 / 图事件循环 / 流后收敛),阶段间共享的
可变状态经 ``_StreamSession`` 传递;yield 语义经子生成器逐级转发,
编排器对子生成器显式 ``aclose()`` 以保持消费方提前关闭时的确定性清理。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import asyncio
import os
import time
import uuid

from backend.application.agent_executors.evidence_convergence import (
    EvidenceConvergenceController,
)
from backend.application.agent_executors.stream_decisions import (
    StreamExitReason,
    needs_empty_answer_recovery as _needs_empty_answer_recovery,
    should_emit_timeout_chunk,
    should_resume_incomplete_round,
    should_run_writer_stage,
    should_write_fallback_state,
)
from backend.application.agent_executors.writer_stage import (
    EvidenceWriterStage,
    WriterStageConfig,
)
from backend.core.agents.control import (
    RunBudget,
    get_agent_runtime_manager,
    reset_agent_runtime_manager,
    set_agent_runtime_manager,
)
from backend.core.agents.runtime_manager import create_agent_runtime_manager
from backend.core.skills.skill_runtime_state import (
    get_missing_required_skills,
    reset_request_skill_state,
    set_request_skill_state,
)
from backend.core.tools.request_limits import (
    get_subagent_execution_budget,
    reset_subagent_execution_limit,
    reset_tool_budgets,
    reset_turn_kb_context_budget,
    reset_web_search_max_results,
    set_subagent_execution_limit,
    set_tool_budgets,
    set_web_search_max_results,
)
from backend.domain.agents.retrieval_policy import RetrievalRuntimePolicy
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from backend.application.agent_executors.stream_tool_evidence import (
    _is_messages_mode_stream_event,
)
from backend.application.agent_executors.streaming_runtime_lifecycle import (
    _optional_budget_float,
    _optional_budget_int,
    cancel_pending_stream_event,
    close_owned_agent_runtime,
    close_stream_event_iterator,
    wait_for_stream_event,
)


logger = logging.getLogger(__name__)


EventAsyncIterator = AsyncIterator[dict[str, Any]]


@dataclass
class _StreamSession:
    """一次全图流式请求在各阶段之间共享的可变状态。

    编排器的 finally 拆卸依赖 ``pending_event_task``/``event_iter``/
    ``event_stream`` 三个字段,阶段内更新这些字段时必须同步写回本对象。
    """

    question: str
    effective_config: dict[str, Any]
    effective_thread_id: str
    policy: Any
    stream_started_at: float
    handler: Any = None
    evidence_convergence: Any = None
    deep_research_mode: bool = False
    deep_research_timeout_budget: float = 0.0
    deep_research_finalize_budget_applied: bool = False
    deep_research_stall_budget_applied: bool = False
    last_upstream_event_at: float = 0.0
    first_event_delay_ms: int | None = None
    first_model_event_delay_ms: int | None = None
    first_chunk_delay_ms: int | None = None
    graph_recursion_error: bool = False
    stream_exit_reason: Any = StreamExitReason.UNKNOWN
    event_stream: Any = None
    event_iter: Any = None
    pending_event_task: Any = None


def _build_stream_config(
    cfg: Any,
    policy: Any,
    *,
    thread_id: str | None,
    config: RunnableConfig | None,
) -> tuple[dict[str, Any], str]:
    """阶段 1:把执行器配置装配为本次请求的 runnable config(纯构造)。"""

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
        if policy.recursion_limit is not None:
            configured_limit = int(policy.recursion_limit)
        else:
            # 通用变量名优先;GROUNDWATER_ 前缀是历史遗留(它一直控制所有域的
            # 默认值,而非仅地下水域),保留为兼容回退。
            raw_limit = os.getenv("AGENT_GRAPH_RECURSION_LIMIT") or os.getenv(
                "GROUNDWATER_GRAPH_RECURSION_LIMIT"
            )
            try:
                configured_limit = (
                    int(raw_limit)
                    if raw_limit is not None and str(raw_limit).strip()
                    else default_recursion_limit
                )
            except (TypeError, ValueError):
                configured_limit = default_recursion_limit
        # 50 是防止多步工具流程被过低配置憋死的安全下限;截断不再静默,
        # 让配置了更小值的部署能从日志发现实际生效值。
        effective_recursion_limit = max(50, configured_limit)
        if effective_recursion_limit != configured_limit:
            logger.warning(
                "配置的图递归上限 %d 低于安全下限，已按 %d 生效",
                configured_limit,
                effective_recursion_limit,
            )
        effective_config["recursion_limit"] = effective_recursion_limit
    return effective_config, effective_thread_id


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
        load_stream_timeout_policy,
    )

    cfg = self._config
    policy = cfg.timeout_policy or load_stream_timeout_policy()
    effective_config, effective_thread_id = _build_stream_config(
        cfg,
        policy,
        thread_id=thread_id,
        config=config,
    )

    stream_started_at = time.monotonic()
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
    # 直接复用 legacy 信号量已截断的封顶值(set_subagent_execution_limit 内部
    # min(5)),两个并发口径由同一处逻辑决定,不再各自维护一份截断。
    active_subagent_limit = get_subagent_execution_budget().max_concurrent
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
    required_skills = {
        str(item).strip()
        for item in (cfg.request_metadata or {}).get("_required_skills", [])
        if str(item or "").strip()
    }

    if cfg.lightweight_mode:
        lightweight_stream = _run_lightweight_stream(
            self,
            question=question,
            image_contents=image_contents,
            effective_config=effective_config,
            stream_started_at=stream_started_at,
            required_skills=required_skills,
            subagent_execution_token=subagent_execution_token,
            runtime_manager=runtime_manager,
            owns_runtime_manager=owns_runtime_manager,
            runtime_manager_token=runtime_manager_token,
        )
        try:
            async for event in lightweight_stream:
                yield event
        finally:
            # 消费方提前关闭时,确保子生成器的清理 finally 同步执行。
            await lightweight_stream.aclose()
        return

    # 注意:image registry 重置已在轻量分支判定前统一执行,此处不再重复。
    reset_turn_kb_context_budget()

    configurable = effective_config.get("configurable", {})
    runtime_task_type = str(
        configurable.get("task_type") or cfg.task_type or ""
    ).strip()
    deep_research_mode = runtime_task_type == cfg.deep_research_task_type
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
    skill_state_token = set_request_skill_state(required_skills)
    session = _StreamSession(
        question=question,
        effective_config=effective_config,
        effective_thread_id=effective_thread_id,
        policy=policy,
        stream_started_at=stream_started_at,
        handler=handler,
        evidence_convergence=EvidenceConvergenceController.from_runtime(
            model_name=cfg.model_name,
            prompt_domain=cfg.prompt_domain,
            request_metadata=cfg.request_metadata,
            policy=cfg.evidence_convergence_policy,
            label=cfg.label,
        ),
        deep_research_mode=deep_research_mode,
        deep_research_timeout_budget=float(
            cfg.deep_research_stream_timeout_seconds
        ),
        last_upstream_event_at=stream_started_at,
    )

    try:
        graph_passes = _stream_graph_passes(
            self,
            session,
            image_contents=image_contents,
        )
        try:
            async for sse in graph_passes:
                yield sse
        finally:
            # 消费方提前关闭时,确保阶段生成器内部的 finally 同步执行。
            await graph_passes.aclose()

        finalization = _finalize_stream_output(self, session)
        try:
            async for sse in finalization:
                yield sse
        finally:
            await finalization.aclose()
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
                        session.pending_event_task
                    )
                    await close_stream_event_iterator(
                        (
                            session.event_iter
                            if session.event_iter is not None
                            else session.event_stream
                        ),
                        active_read_task=(
                            None
                            if pending_event_stopped
                            else session.pending_event_task
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


async def _run_lightweight_stream(
    self,
    *,
    question: str,
    image_contents: list[dict] | None,
    effective_config: dict[str, Any],
    stream_started_at: float,
    required_skills: set[str],
    subagent_execution_token: Any,
    runtime_manager: Any,
    owns_runtime_manager: bool,
    runtime_manager_token: Any,
) -> EventAsyncIterator:
    """阶段 2:轻量模式分支(自带预算/技能状态安装与全套清理)。"""

    from backend.application.agent_executors.lightweight_qa_agent import (
        LightweightQAAgent,
    )

    cfg = self._config
    run_status = "interrupted"
    run_error: str | None = None
    active_failure: BaseException | None = None
    reset_turn_kb_context_budget()
    lightweight_policy = RetrievalRuntimePolicy.for_mode(lightweight=True)
    budget_token = set_tool_budgets(
        {
            **lightweight_policy.tool_budgets(),
            "multi_kb_search": cfg.multi_kb_search_max_calls,
        }
    )
    skill_state_token = set_request_skill_state(required_skills)
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
        lightweight_event_stream = lightweight_agent.aquery_stream(
            question=question,
            effective_config=effective_config,
            stream_started_at=stream_started_at,
            image_contents=image_contents,
        )
        try:
            async for event in lightweight_event_stream:
                yield event
        finally:
            # 消费方提前关闭时同步收尾最内层生成器(有界),不等 GC 异步补刀。
            await close_stream_event_iterator(lightweight_event_stream)
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


def _grant_deep_research_stall_grace(
    session: _StreamSession,
    handler: Any,
    cfg: Any,
) -> bool:
    """深研总预算耗尽但委派子任务仍活跃时，一次性延长收尾窗口。

    与 finalize 宽限同款的一次性预算扩展：把 stall 宽限直接加进
    ``deep_research_timeout_budget``，让顶部预算检查、事件后预算检查和
    空转超时分支天然共享同一个延长后的截止点。此前该宽限只在超时分支里
    ``continue``，下一轮迭代立刻被顶部预算检查打断，配置从未真正生效，
    用户还会看到「继续等待」与「执行超时」两条矛盾状态背靠背出现。
    """

    if session.deep_research_stall_budget_applied:
        return False
    if not handler.deep_research_active_task_ids:
        return False
    grace_seconds = float(cfg.deep_research_task_stall_grace_seconds)
    if grace_seconds <= 0:
        return False
    stall_seconds = (
        time.monotonic() - handler.deep_research_last_task_runtime_at
    )
    if stall_seconds >= grace_seconds:
        return False
    session.deep_research_timeout_budget += grace_seconds
    session.deep_research_stall_budget_applied = True
    logger.info(
        "[%s] 深度研究总预算耗尽但子任务仍活跃(stall=%.0fs)，一次性延长收尾窗口 %.0fs",
        cfg.label,
        stall_seconds,
        grace_seconds,
    )
    return True


async def _stream_graph_passes(
    self,
    session: _StreamSession,
    *,
    image_contents: list[dict] | None,
) -> EventAsyncIterator:
    """阶段 3:LangGraph 事件主循环(续跑 pass、超时阶梯、证据收敛)。"""

    cfg = self._config
    policy = session.policy
    handler = session.handler
    question = session.question
    effective_config = session.effective_config
    effective_thread_id = session.effective_thread_id
    stream_started_at = session.stream_started_at
    deep_research_mode = session.deep_research_mode
    evidence_convergence = session.evidence_convergence

    yield {"type": "status", "content": "已接收问题，正在分析..."}
    if deep_research_mode:
        yield {
            "type": "status",
            "content": "深度研究模式已启用：将拆解任务、并行研究并放宽检索轮数。",
        }

    user_content = self._build_user_content(question, image_contents)
    logger.info(
        "[%s] 流式等待配置: idle=%ds model_round_idle=%ds heartbeat=%ds retry=%d total=%s",
        cfg.label,
        int(policy.stream_idle_timeout_seconds),
        int(policy.model_round_idle_timeout_seconds),
        int(policy.heartbeat_interval_seconds),
        policy.incomplete_round_retry_max,
        policy.normal_stream_total_timeout_seconds,
    )

    max_stream_passes = (
        1
        if deep_research_mode
        else (1 + policy.incomplete_round_retry_max)
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
            if not should_resume_incomplete_round(
                session.stream_exit_reason,
                handler.has_incomplete_model_round(),
            ):
                break
            logger.warning(
                "[%s] 模型轮次未完成，从 checkpoint 续跑 pass=%d/%d thread_id=%s",
                cfg.label,
                stream_pass,
                policy.incomplete_round_retry_max,
                effective_thread_id,
            )
            yield {
                "type": "status",
                "content": "模型仍在生成中，正在延长等待并尝试续跑...",
            }
            graph_input = None
            session.last_upstream_event_at = time.monotonic()

        session.event_stream = cfg.graph.astream_events(
            graph_input,
            config=effective_config,
            version="v2",
            stream_mode=["messages", "custom", "updates", "tasks"],
        )
        session.event_iter = session.event_stream.__aiter__()
        session.pending_event_task = None
        session.stream_exit_reason = StreamExitReason.UNKNOWN

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
                    idle_timeout = policy.resolve_normal_idle_timeout(
                        handler.has_incomplete_model_round(),
                    )

                if deep_research_mode:
                    elapsed_seconds = time.monotonic() - stream_started_at
                    remaining_total = (
                        session.deep_research_timeout_budget - elapsed_seconds
                    )
                    if remaining_total <= 0 and _grant_deep_research_stall_grace(
                        session, handler, cfg
                    ):
                        yield {
                            "type": "status",
                            "content": "深度研究已达时间预算，但子任务仍在运行，已延长收尾等待窗口...",
                        }
                        remaining_total = (
                            session.deep_research_timeout_budget - elapsed_seconds
                        )
                    if remaining_total <= 0:
                        handler.timeout_reached = True
                        session.stream_exit_reason = (
                            StreamExitReason.DEEP_RESEARCH_TOTAL_TIMEOUT
                        )
                        yield {
                            "type": "status",
                            "content": "深度研究执行超时，正在整理阶段性结果并强制生成研究报告...",
                        }
                        break
                    wait_timeout = min(idle_timeout, max(1.0, remaining_total))
                else:
                    elapsed_total = time.monotonic() - stream_started_at
                    total_timeout = (
                        policy.normal_stream_total_timeout_seconds
                    )
                    if total_timeout and elapsed_total >= total_timeout:
                        handler.timeout_reached = True
                        session.stream_exit_reason = (
                            StreamExitReason.STREAM_TOTAL_TIMEOUT
                        )
                        yield {
                            "type": "status",
                            "content": "当前流程执行总时长超过限制，正在提前收敛并返回阶段性结果...",
                        }
                        break
                    idle_elapsed = max(
                        0.0, time.monotonic() - session.last_upstream_event_at
                    )
                    remaining_idle = max(1.0, idle_timeout - idle_elapsed)
                    wait_timeout = min(
                        policy.heartbeat_interval_seconds, remaining_idle
                    )

                event, session.pending_event_task = await wait_for_stream_event(
                    session.event_iter,
                    session.pending_event_task,
                    timeout=wait_timeout,
                )
                if event is None:
                    raise asyncio.TimeoutError  # noqa: UP041
            except StopAsyncIteration:
                session.pending_event_task = None
                session.stream_exit_reason = (
                    StreamExitReason.LANGGRAPH_STREAM_COMPLETED
                )
                break
            except GraphRecursionError:
                session.pending_event_task = None
                session.graph_recursion_error = True
                session.stream_exit_reason = (
                    StreamExitReason.GRAPH_RECURSION_LIMIT
                )
                handler.timeout_reached = True
                logger.error(
                    "[%s] LangGraph 达到递归上限，停止继续调用工具: thread_id=%s tools=%s",
                    cfg.label,
                    effective_thread_id,
                    handler.tools_invoked,
                )
                yield {
                    "type": "status",
                    "content": "工具调用次数过多，已停止继续检索并基于现有证据生成答案...",
                }
                break
            except asyncio.TimeoutError:  # noqa: UP041
                if not deep_research_mode:
                    idle_elapsed = (
                        time.monotonic() - session.last_upstream_event_at
                    )
                    idle_timeout = policy.resolve_normal_idle_timeout(
                        handler.has_incomplete_model_round(),
                    )
                    if idle_elapsed < idle_timeout:
                        yield {
                            "type": "ping",
                            "timestamp": int(time.time() * 1000),
                        }
                        continue

                if deep_research_mode:
                    elapsed_seconds = time.monotonic() - stream_started_at
                    if elapsed_seconds < session.deep_research_timeout_budget:
                        yield {
                            "type": "status",
                            "content": "深度研究仍在执行，正在汇总结果并准备保存报告...",
                        }
                        continue

                    if _grant_deep_research_stall_grace(session, handler, cfg):
                        yield {
                            "type": "status",
                            "content": "子任务仍在运行，已延长收尾等待窗口并继续汇总研究结果...",
                        }
                        continue

                handler.timeout_reached = True
                session.stream_exit_reason = (
                    StreamExitReason.DEEP_RESEARCH_IDLE_TIMEOUT
                    if deep_research_mode
                    else StreamExitReason.IDLE_TIMEOUT
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

            session.last_upstream_event_at = time.monotonic()
            kind = event.get("event")
            name = event.get("name", "")

            if session.first_event_delay_ms is None:
                session.first_event_delay_ms = int(
                    (time.monotonic() - stream_started_at) * 1000
                )
                logger.info(
                    "[%s] 首个 LangGraph 事件耗时: %dms, kind=%s, name=%s",
                    cfg.label,
                    session.first_event_delay_ms,
                    kind,
                    name,
                )
            if (
                kind == "on_chat_model_start"
                and session.first_model_event_delay_ms is None
            ):
                session.first_model_event_delay_ms = int(
                    (time.monotonic() - stream_started_at) * 1000
                )
                logger.info(
                    "[%s] 首个模型事件耗时: %dms",
                    cfg.label,
                    session.first_model_event_delay_ms,
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
                if (
                    sse.get("type") == "chunk"
                    and session.first_chunk_delay_ms is None
                ):
                    session.first_chunk_delay_ms = int(
                        (time.monotonic() - stream_started_at) * 1000
                    )
                    logger.info(
                        "[%s] 首个输出 chunk 耗时: %dms",
                        cfg.label,
                        session.first_chunk_delay_ms,
                    )
                yield sse
                if not deep_research_mode and handler.output_truncated:
                    should_break_stream = True

            if (
                deep_research_mode
                and handler.deep_research_grace_applied
                and not session.deep_research_finalize_budget_applied
            ):
                session.deep_research_timeout_budget += (
                    cfg.deep_research_finalize_grace_seconds
                )
                session.deep_research_finalize_budget_applied = True

            if kind in {
                "on_tool_end",
                "on_chain_end",
                "on_custom_event",
                "on_chain_stream",
            } and not _is_messages_mode_stream_event(event):
                # messages 流模式下每个模型 token 都是根链 on_chain_stream:
                # 工件只在节点完成时变化,逐 token 轮询 aget_state 会经全应用
                # 共享的单条 aiosqlite 连接读整份 checkpoint 并反序列化全部
                # 历史消息,长会话 × 数千 token × 并发 = 确定性逐 token 热点。
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
                    session.stream_exit_reason = (
                        StreamExitReason.EVIDENCE_CONVERGED
                    )
                    break

            if (
                deep_research_mode
                and (time.monotonic() - stream_started_at)
                > session.deep_research_timeout_budget
            ):
                if _grant_deep_research_stall_grace(session, handler, cfg):
                    yield {
                        "type": "status",
                        "content": "深度研究已达时间预算，但子任务仍在运行，已延长收尾等待窗口...",
                    }
                else:
                    handler.timeout_reached = True
                    session.stream_exit_reason = (
                        StreamExitReason.DEEP_RESEARCH_BUDGET_EXCEEDED
                    )
                    yield {
                        "type": "status",
                        "content": "深度研究执行超时，正在整理阶段性结果并强制生成研究报告...",
                    }
                    break

            if should_break_stream:
                session.stream_exit_reason = StreamExitReason.OUTPUT_TRUNCATED
                break

        pending_event_stopped = await cancel_pending_stream_event(
            session.pending_event_task
        )
        if pending_event_stopped:
            session.pending_event_task = None

        stream_pass += 1
        # 旧读取任务仍在驱动上一轮 astream_events（取消未在超时内生效）时绝不
        # 续跑：否则同一 thread_id 上两个图执行会并发交错写同一 checkpoint。
        if not pending_event_stopped:
            break
        if not should_resume_incomplete_round(
            session.stream_exit_reason,
            handler.has_incomplete_model_round(),
        ):
            break
        # 续跑会用新的 astream_events 覆盖 session.event_stream/event_iter。
        # 覆盖前显式关闭上一轮生成器：编排器 finally 只关最终指针，被覆盖的
        # 生成器否则要等 GC 收尾，旧图的 run manager/子任务在此窗口内仍可能
        # 与新 pass 交错写同一 checkpoint。
        try:
            await close_stream_event_iterator(
                (
                    session.event_iter
                    if session.event_iter is not None
                    else session.event_stream
                ),
                active_read_task=None,
            )
        except Exception:
            logger.debug(
                "[%s] 续跑前关闭上一轮事件流失败", cfg.label, exc_info=True
            )
        finally:
            session.event_iter = None
            session.event_stream = None
            session.pending_event_task = None


async def _finalize_stream_output(
    self,
    session: _StreamSession,
) -> EventAsyncIterator:
    """阶段 4:流后收敛(writer 兜底、深研归档、兜底写回、超时提示、统计)。"""

    cfg = self._config
    handler = session.handler
    question = session.question
    effective_config = session.effective_config
    effective_thread_id = session.effective_thread_id
    stream_started_at = session.stream_started_at
    deep_research_mode = session.deep_research_mode
    stream_exit_reason = session.stream_exit_reason

    handler.set_stream_exit_reason(stream_exit_reason)
    idle_since_last_event_ms = int(
        (time.monotonic() - session.last_upstream_event_at) * 1000
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
            handler.model_generation_round,
            len(handler.model_round_summaries),
            stream_exit_reason,
        )

    # 无论以何种原因退出事件循环(证据收敛、超时、递归上限、正常完成),都先
    # 确定性关闭旧的 astream_events:图可能仍在后台执行并开启新一轮模型调用,
    # 晚关闭会让 writer、深研归档和兜底 aupdate_state 与旧图并发交错写同一
    # checkpoint(此前只有 writer 分支先关流)。正常完成时关闭是无代价空操作。
    # 与续跑循环同款护栏:关闭可能只是"注册延迟关闭"(读取任务未在超时内停,
    # 例如节点吞取消或阻塞在同步调用里),此时旧图并未死透,后续所有
    # aupdate_state 恢复路径必须放弃,否则与旧图交错写同一 checkpoint。
    old_graph_quiesced = True
    try:
        reader_stopped = await cancel_pending_stream_event(
            session.pending_event_task
        )
        iterator_closed = await close_stream_event_iterator(
            (
                session.event_iter
                if session.event_iter is not None
                else session.event_stream
            ),
            active_read_task=(
                None if reader_stopped else session.pending_event_task
            ),
        )
        old_graph_quiesced = reader_stopped and iterator_closed
    except Exception:
        old_graph_quiesced = False
        logger.debug(
            "[%s] 流后收敛前关闭事件流失败", cfg.label, exc_info=True
        )
    finally:
        session.pending_event_task = None
        session.event_iter = None
        session.event_stream = None
    if not old_graph_quiesced:
        logger.warning(
            "[%s] 旧图事件流未在超时内死透: thread_id=%s reason=%s;"
            "跳过流后状态写入恢复以避免并发写同一 checkpoint",
            cfg.label,
            effective_thread_id,
            stream_exit_reason,
        )

    request_metadata = cfg.request_metadata or {}
    request_id = str(
        request_metadata.get("_context_scope_id")
        or request_metadata.get("_request_id")
        or ""
    ).strip()
    from backend.application.tools.providers.context_read_tool import (
        get_capability_ledger,
    )

    request_ledger = get_capability_ledger(request_id or None)
    has_writer_evidence = bool(
        handler.project_evidence_text
        or handler.retrieved_evidence_text
        or (
            request_ledger is not None
            and bool(request_ledger.ledger_rows())
        )
    )
    needs_empty_answer_recovery = _needs_empty_answer_recovery(
        stream_exit_reason,
        handler.answer_fragments,
        handler.any_tool_executed,
    )
    run_writer_stage = should_run_writer_stage(
        stream_exit_reason,
        deep_research_mode=deep_research_mode,
        has_writer_evidence=has_writer_evidence,
        answer_fragments=handler.answer_fragments,
        any_tool_executed=handler.any_tool_executed,
    )
    if run_writer_stage and not old_graph_quiesced:
        # writer 通过 graph.aupdate_state 写同一 thread 的状态,未死透的
        # 旧图仍可能并发追加消息;与续跑循环的 break 同语义,放弃兜底。
        logger.warning(
            "[%s] 旧图未死透,跳过无工具撰写兜底: thread_id=%s",
            cfg.label,
            effective_thread_id,
        )
        run_writer_stage = False
    if run_writer_stage:
        if needs_empty_answer_recovery:
            logger.warning(
                "[%s] LangGraph 正常结束但未生成可见正文，"
                "正在使用已收集证据启动无工具撰写兜底: thread_id=%s",
                cfg.label,
                effective_thread_id,
            )
        # 旧事件流已在本阶段开头统一关闭,无工具撰写不会与旧图并发。
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
                and session.first_chunk_delay_ms is None
            ):
                session.first_chunk_delay_ms = int(
                    (time.monotonic() - stream_started_at) * 1000
                )
                logger.info(
                    "[%s] 首个撰写输出 chunk 耗时: %dms",
                    cfg.label,
                    session.first_chunk_delay_ms,
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
    write_fallback_state = should_write_fallback_state(
        stream_exit_reason,
        deep_research_mode=deep_research_mode,
        has_incomplete_model_round=handler.has_incomplete_model_round(),
        timeout_reached=handler.timeout_reached,
        answer_fragments=handler.answer_fragments,
        writer_persisted=handler.answer_persisted_to_state,
    )
    if write_fallback_state and not old_graph_quiesced:
        # 兜底写回同样走 aupdate_state;旧图未死透时放弃,用户已在流里
        # 看到兜底文本,只是刷新后的历史缺这一条,不能换来状态交错。
        logger.warning(
            "[%s] 旧图未死透,跳过中断回答兜底写回: thread_id=%s",
            cfg.label,
            effective_thread_id,
        )
        write_fallback_state = False
    if write_fallback_state:
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

    if should_emit_timeout_chunk(
        stream_exit_reason,
        timeout_reached=handler.timeout_reached,
        answer_fragments=handler.answer_fragments,
        graph_recursion_error=session.graph_recursion_error,
    ):
        # 只有深研模式能走到这里:该门槛要求正文为空,而 normal 模式
        # handler.finalize() 的兜底分支保证 answer_fragments 非空。
        yield {
            "type": "chunk",
            "content": "深度研究主流程超时，已停止继续等待并返回当前可用阶段性结果。",
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
        handler.normal_output_chars,
        handler.timeout_reached,
        handler.output_truncated,
        total_ms,
        session.first_event_delay_ms,
        session.first_model_event_delay_ms,
        session.first_chunk_delay_ms,
    )
    if (
        cfg.warn_low_visible_output
        and not deep_research_mode
        and handler.normal_output_chars < 300
        and (handler.any_tool_executed or handler.model_generation_round > 0)
    ):
        logger.warning(
            "[%s] 本轮可见输出偏少: thread_id=%s answer_chars=%d model_rounds=%d tools=%s html_ppt_ok=%s",
            cfg.label,
            effective_thread_id,
            handler.normal_output_chars,
            handler.model_generation_round,
            handler.tools_invoked,
            handler.html_ppt_succeeded,
        )
