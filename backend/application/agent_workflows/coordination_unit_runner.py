"""Lifecycle and streaming execution for one coordinated agent unit."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import replace
from typing import Any

from backend.application.agent_executors.stream_utils import make_process_event
from backend.application.agent_trace import AgentTraceRecorder
from backend.application.agent_trace.models import utc_now_iso
from backend.application.agent_workflows import coordinated_agent_workflow as _legacy
from backend.application.agent_workflows.coordination_component import (
    CoordinationComponent,
)
from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
)
from backend.application.blocking import run_blocking
from backend.core.agents.context.subagent_adapter import (
    build_subagent_result,
    normalize_subagent_result,
    validate_subagent_result_strict,
)
from backend.core.agents.resource_lease import (
    InMemoryResourceLeaseManager,
    Lease,
    ResourceLeaseAcquireTimeout,
    ResourceLeaseError,
)
from backend.core.agents.runtime_manager import (
    get_agent_runtime_manager,
    get_current_agent_id,
)
from backend.core.utils.async_tasks import close_async_iterator_safely
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext

logger = logging.getLogger(__name__)
WorkflowEventSink = _legacy.WorkflowEventSink
_DEEP_RESEARCH_BRANCH_WEB_CALLS = _legacy._DEEP_RESEARCH_BRANCH_WEB_CALLS
_ManagedCoordinationUnitFailure = _legacy._ManagedCoordinationUnitFailure
_ManagedCoordinationUnitTimeout = _legacy._ManagedCoordinationUnitTimeout
_cancel_task_bounded = _legacy._cancel_task_bounded

from backend.application.agent_workflows.coordination_runtime_support import (  # noqa: E402
    _consume_background_task_result,
)


class CoordinationUnitRunner(CoordinationComponent):
    async def _run_unit(
        self,
        *,
        role: str,
        question: str,
        request: AgentRequest,
        context: AgentRuntimeContext,
        tool_access: bool = True,
        event_sink: WorkflowEventSink | None = None,
        progress_title: str | None = None,
        task_id: str | None = None,
        handoff_evidence_refs: list[str] | None = None,
        handoff_evidence_records: list[dict[str, Any]] | None = None,
        stream_answer: bool = False,
        allowed_tools: set[str] | None = None,
        timeout_override_seconds: float | None = None,
    ) -> CoordinationUnitResult:
        """Execute one unit through the request-tree runtime manager."""

        runtime_manager = get_agent_runtime_manager()
        if runtime_manager is None:
            return await self._host._run_unit_unmanaged(
                role=role,
                question=question,
                request=request,
                context=context,
                tool_access=tool_access,
                event_sink=event_sink,
                progress_title=progress_title,
                task_id=task_id,
                handoff_evidence_refs=handoff_evidence_refs,
                handoff_evidence_records=handoff_evidence_records,
                stream_answer=stream_answer,
                allowed_tools=allowed_tools,
                timeout_override_seconds=timeout_override_seconds,
            )

        unit_id = task_id or f"{context.request_id}:{self.name}:{role}"
        selected_model = self._role_model_name(role, context)
        child_thread_id = self._unit_thread_id(
            context,
            unit_key=task_id or role,
        )
        if get_current_agent_id() == unit_id:
            return await self._host._run_unit_unmanaged(
                role=role,
                question=question,
                request=request,
                context=context,
                tool_access=tool_access,
                event_sink=event_sink,
                progress_title=progress_title,
                task_id=task_id,
                handoff_evidence_refs=handoff_evidence_refs,
                handoff_evidence_records=handoff_evidence_records,
                stream_answer=stream_answer,
                allowed_tools=allowed_tools,
                timeout_override_seconds=timeout_override_seconds,
            )
        try:
            async with runtime_manager.spawn(
                unit_id,
                metadata={
                    "workflow": self.name,
                    "role": role,
                    "task_id": task_id,
                    "model_name": selected_model,
                    "model_tier": self._model_tier(selected_model, context),
                    "thread_id": child_thread_id,
                    "parent_thread_id": context.internal_thread_id,
                },
            ):
                cancellation = await runtime_manager.token_for(unit_id)
                result = await self._host._run_unit_unmanaged(
                    role=role,
                    question=question,
                    request=request,
                    context=context,
                    tool_access=tool_access,
                    event_sink=event_sink,
                    progress_title=progress_title,
                    task_id=task_id,
                    handoff_evidence_refs=handoff_evidence_refs,
                    handoff_evidence_records=handoff_evidence_records,
                    stream_answer=stream_answer,
                    allowed_tools=allowed_tools,
                    timeout_override_seconds=timeout_override_seconds,
                )
                cancellation.raise_if_cancelled()
                if result.error:
                    if result.timed_out:
                        raise _ManagedCoordinationUnitTimeout(result)
                    raise _ManagedCoordinationUnitFailure(result)
                return result
        except _ManagedCoordinationUnitFailure as exc:
            return exc.result

    async def _run_unit_unmanaged(
        self,
        *,
        role: str,
        question: str,
        request: AgentRequest,
        context: AgentRuntimeContext,
        tool_access: bool = True,
        event_sink: WorkflowEventSink | None = None,
        progress_title: str | None = None,
        task_id: str | None = None,
        handoff_evidence_refs: list[str] | None = None,
        handoff_evidence_records: list[dict[str, Any]] | None = None,
        stream_answer: bool = False,
        allowed_tools: set[str] | None = None,
        timeout_override_seconds: float | None = None,
    ) -> CoordinationUnitResult:
        requested_tool_access = tool_access
        tool_access = self._effective_tool_access(
            role=role,
            requested=tool_access,
            context=context,
        )
        allowed_tools = self._effective_allowed_tools(
            role=role,
            requested_allowed_tools=allowed_tools,
            context=context,
        )
        role_spec = self._role_spec(context, role)
        role_model_name = self._role_model_name(role, context)
        unit_timeout_seconds = (
            max(0.001, float(timeout_override_seconds))
            if timeout_override_seconds is not None
            else self._role_timeout_seconds(role, context)
        )
        role_allowed_tools = (
            set(allowed_tools)
            if allowed_tools is not None
            else (
                set(role_spec.allowed_tools)
                if role_spec is not None and role_spec.allowed_tools is not None
                else None
            )
        )
        if role_allowed_tools is not None:
            if not (tool_access and context.use_search):
                role_allowed_tools -= {"web_search", "academic_search"}
            if not (tool_access and context.kb_ids):
                role_allowed_tools.discard("multi_kb_search")
        effective_role_spec = (
            replace(
                role_spec,
                allowed_tools=role_allowed_tools,
                tool_access=tool_access,
                can_use_network=(
                    tool_access and role_spec.can_use_network and context.use_search
                ),
                can_read_kb=(
                    tool_access and role_spec.can_read_kb and bool(context.kb_ids)
                ),
            )
            if role_spec is not None
            else None
        )
        trace = AgentTraceRecorder.from_context(context, workflow_name=self.name)
        unit_id = task_id or f"{context.request_id}:{self.name}:{role}"
        trace_started_at = utc_now_iso()
        trace.record_event(
            sequence=0,
            event_type="coordination.unit.started",
            unit_id=unit_id,
            payload={
                "workflow": self.name,
                "role": role,
                "task_id": task_id,
                "requested_tool_access": requested_tool_access,
                "tool_access": tool_access,
                "runtime_agent": role_spec.name if role_spec else None,
                "model_name": role_model_name,
                "model_tier": self._model_tier(role_model_name, context),
            },
        )
        unit_metadata = {
            **dict(request.metadata or {}),
            **dict(context.request_metadata or {}),
            "coordination_workflow": self.name,
            "coordination_role": role,
            "context_handoff_mode": (
                "isolated_execution" if tool_access else "result_only"
            ),
            "tool_access": tool_access,
            "requested_tool_access": requested_tool_access,
            "coordination_model_name": role_model_name,
            "coordination_model_tier": self._model_tier(role_model_name, context),
            **(
                {
                    "runtime_agent": role_spec.name,
                    "runtime_agent_max_turns": role_spec.max_turns,
                    "runtime_agent_timeout_seconds": role_spec.timeout_seconds,
                    "runtime_agent_output_schema": role_spec.output_schema,
                    "runtime_agent_can_use_network": role_spec.can_use_network,
                    "runtime_agent_can_read_kb": role_spec.can_read_kb,
                    "coordination_required_tools": sorted(role_spec.required_tools),
                    "coordination_disallowed_tools": sorted(role_spec.disallowed_tools),
                }
                if role_spec is not None
                else {}
            ),
            **(
                {"coordination_allowed_tools": sorted(role_allowed_tools)}
                if role_allowed_tools is not None
                else {}
            ),
        }
        unit_request = replace(
            request,
            question=question,
            requested_workflow="full",
            subagent_enabled=False,
            metadata=unit_metadata,
        )
        unit_context = replace(
            context,
            workflow_name="full",
            internal_thread_id=self._unit_thread_id(
                context,
                unit_key=task_id or role,
            ),
            subagent_enabled=False,
            model_name=role_model_name,
            use_search=(
                context.use_search
                if tool_access and self._role_can_use_network(role, context)
                else False
            ),
            kb_ids=(
                list(context.kb_ids)
                if tool_access and self._role_can_read_kb(role, context)
                else []
            ),
            # ``deep-research`` governs the parent workflow.  Its method is
            # already injected through the branch execution contract, so
            # requiring every isolated worker to load the same Skill again is
            # redundant and can leave a false "missing Skill" warning when a
            # worker correctly proceeds directly to evidence retrieval.
            selected_skills=(
                [
                    skill_name
                    for skill_name in context.selected_skills
                    if skill_name != "deep-research"
                ]
                if tool_access
                else []
            ),
            request_metadata=unit_metadata,
            agent_specs={
                **dict(context.agent_specs),
                **(
                    {effective_role_spec.name: effective_role_spec}
                    if effective_role_spec is not None
                    else {}
                ),
            },
        )
        answer_parts: list[str] = []
        errors: list[str] = []
        timed_out = False
        process_events: list[dict[str, Any]] = []
        try:
            branch_web_search_max_calls = max(
                1,
                int(
                    unit_metadata.get("coordination_web_search_max_calls")
                    or _DEEP_RESEARCH_BRANCH_WEB_CALLS
                ),
            )
        except (TypeError, ValueError):
            branch_web_search_max_calls = _DEEP_RESEARCH_BRANCH_WEB_CALLS
        completed_web_searches = 0
        completed_web_result_ids: set[str] = set()
        convergence_reason: str | None = None
        structured_convergence_handoff: dict[str, Any] | None = None
        hard_timeout_seconds = float(unit_timeout_seconds)
        configured_convergence_seconds = float(
            self._runtime_budget.deep_research_branch_convergence_timeout_seconds
            if self.name == "deep_research" and task_id is not None
            else self._runtime_budget.partial_finalization_wait_seconds
        )
        convergence_grace_seconds = min(
            configured_convergence_seconds,
            hard_timeout_seconds / 2,
        )
        retrieval_timeout_seconds = max(
            0.0,
            hard_timeout_seconds - convergence_grace_seconds,
        )
        pending_event: asyncio.Task[Any] | None = None
        stream: Any = None
        acquired_leases: list[Lease] = []
        lease_renewal_tasks: list[asyncio.Task[None]] = []
        unit_started_at = time.monotonic()

        def remaining_unit_seconds() -> float:
            remaining = hard_timeout_seconds - (time.monotonic() - unit_started_at)
            if remaining <= 0:
                raise TimeoutError(
                    f"{role} setup exceeded hard timeout after "
                    f"{hard_timeout_seconds:g}s"
                )
            return remaining

        try:
            llm_capacity = self._research_llm_capacity()
            per_user_model_capacity = min(
                llm_capacity,
                int(self._runtime_budget.max_concurrent_per_user_model),
            )
            # ``plan_solve_max_concurrent_subagents`` controls scheduler fan-out,
            # not provider capacity. All coordinated workflows share the same
            # process/Redis-backed global LLM ceiling.
            shared_global_capacity = int(self._runtime_budget.max_concurrent_research)
            limiter_specs = (
                (
                    f"llm-user-model:{unit_context.user_id}:{unit_context.model_name}",
                    per_user_model_capacity,
                ),
                ("llm-global", shared_global_capacity),
            )
            lease_manager = (
                self._resource_lease_manager or _legacy._get_resource_lease_manager()
            )
            lease_ttl_seconds = _legacy._resource_lease_ttl_seconds()
            lease_owner_id = f"{context.request_id}:{unit_id}:{uuid.uuid4().hex}"
            owner_task = asyncio.current_task()
            for resource_key, capacity in limiter_specs:
                effective_resource_key = resource_key
                if isinstance(lease_manager, InMemoryResourceLeaseManager):
                    # Local tests/dev may intentionally construct workflows
                    # with different immutable budget snapshots in one loop.
                    # Keep equal configurations process-shared without making
                    # distinct capacities conflict in the fallback backend.
                    effective_resource_key = f"{resource_key}:capacity:{capacity}"
                lease = await lease_manager.acquire(
                    effective_resource_key,
                    owner_id=lease_owner_id,
                    capacity=capacity,
                    ttl_seconds=lease_ttl_seconds,
                    timeout_seconds=remaining_unit_seconds(),
                )
                acquired_leases.append(lease)
                lease_renewal_tasks.append(
                    asyncio.create_task(
                        _legacy._renew_resource_lease(
                            lease,
                            ttl_seconds=lease_ttl_seconds,
                            owner_task=owner_task,
                        ),
                        name=f"agent-resource-lease:{lease.lease_id}",
                    )
                )
            executor = await asyncio.wait_for(
                run_blocking(
                    self._delegate.build_executor,
                    unit_request,
                    unit_context,
                ),
                timeout=remaining_unit_seconds(),
            )
            stream = executor.aquery_stream(
                question,
                unit_context.internal_thread_id,
                self._role_runnable_config(role, unit_context),
            ).__aiter__()
            started_at = unit_started_at
            last_event_at = time.monotonic()
            while True:
                now = time.monotonic()
                total_elapsed = now - started_at
                idle_elapsed = now - last_event_at
                if total_elapsed >= hard_timeout_seconds:
                    timed_out = True
                    errors.append(
                        f"{role} execution exceeded hard timeout after "
                        f"{hard_timeout_seconds:g}s"
                    )
                    break
                if (
                    task_id is not None
                    and tool_access
                    and total_elapsed >= retrieval_timeout_seconds
                ):
                    convergence_reason = "retrieval_timeout"
                    await self._emit_process_event(
                        event_sink,
                        make_process_event(
                            "task_progress" if task_id else "status",
                            "检索时限已到，正在收敛结论",
                            "检索执行阶段已达到时间上限，现已停止工具调用；"
                            "将使用单元总时限内预留的窗口，基于已捕获证据独立整理结论。",
                            tool="task" if task_id else None,
                            level="active",
                            metadata={
                                "task_id": task_id,
                                "runtime_type": "task_converging",
                                "role": role,
                                "completed_web_searches": completed_web_searches,
                                "search_timeout_suspended": True,
                                "search_timeout_reached": True,
                                "retrieval_timeout_seconds": retrieval_timeout_seconds,
                                "hard_timeout_seconds": hard_timeout_seconds,
                            },
                        ),
                    )
                    break
                if idle_elapsed >= self._runtime_budget.unit_idle_timeout_seconds:
                    timed_out = True
                    errors.append(
                        f"{role} produced no events for "
                        f"{self._runtime_budget.unit_idle_timeout_seconds}s"
                    )
                    break

                pending_event = pending_event or asyncio.create_task(stream.__anext__())
                wait_budgets = [
                    float(self._runtime_budget.unit_heartbeat_seconds),
                    float(self._runtime_budget.unit_idle_timeout_seconds)
                    - idle_elapsed,
                    hard_timeout_seconds - total_elapsed,
                ]
                if (
                    task_id is not None
                    and tool_access
                    and total_elapsed < retrieval_timeout_seconds
                ):
                    wait_budgets.append(retrieval_timeout_seconds - total_elapsed)
                wait_seconds = min(wait_budgets)
                try:
                    raw_event = await asyncio.wait_for(
                        asyncio.shield(pending_event),
                        timeout=max(0.01, wait_seconds),
                    )
                    pending_event = None
                # Python 3.10 keeps asyncio.TimeoutError distinct from the
                # builtin TimeoutError used by newer runtimes.
                except asyncio.TimeoutError:  # noqa: UP041
                    elapsed_seconds = max(1, int(time.monotonic() - started_at))
                    progress_detail = (
                        f"已等待 {elapsed_seconds} 秒，任务仍在运行；"
                        "达到检索执行上限后将自动停止工具并使用阶段性结果收敛。"
                    )
                    await self._emit_process_event(
                        event_sink,
                        make_process_event(
                            "task_progress" if task_id else "status",
                            progress_title or f"{role} 仍在执行",
                            progress_detail,
                            tool="task" if task_id else None,
                            level="active",
                            metadata={
                                "task_id": task_id,
                                "runtime_type": (
                                    "task_running" if task_id else "unit_running"
                                ),
                                "elapsed_seconds": elapsed_seconds,
                                "role": role,
                            },
                        ),
                    )
                    continue
                except StopAsyncIteration:
                    pending_event = None
                    break

                last_event_at = time.monotonic()
                event = self._normalize_event(raw_event, context)
                if event.type == "message.delta":
                    content = str(event.payload.get("content") or "")
                    answer_parts.append(content)
                    if stream_answer and content:
                        await self._emit_event(
                            event_sink,
                            "message.delta",
                            {
                                "content": content,
                                "metadata": {
                                    "coordination_role": role,
                                    "streaming_final_answer": True,
                                },
                            },
                        )
                elif event.type == "error":
                    errors.append(
                        str(
                            event.payload.get("message")
                            or event.payload.get("error")
                            or ""
                        )
                    )
                else:
                    process_event = self._task_scoped_unit_process_event(
                        event,
                        task_id=task_id,
                        role=role,
                    )
                    if process_event is not None:
                        process_events.append(process_event)
                        await self._emit_process_event(
                            event_sink,
                            {
                                key: value
                                for key, value in process_event.items()
                                if key != "_coordination_evidence_records"
                            },
                        )
                        if (
                            task_id is not None
                            and tool_access
                            and convergence_reason is None
                            and process_event.get("kind") == "tool_result"
                            and str(process_event.get("tool") or "").strip()
                            == "web_search"
                            and str(process_event.get("level") or "").lower()
                            not in {"warning", "error"}
                        ):
                            result_id = str(process_event.get("id") or "").strip()
                            if (
                                not result_id
                                or result_id not in completed_web_result_ids
                            ):
                                if result_id:
                                    completed_web_result_ids.add(result_id)
                                completed_web_searches += 1
                            if completed_web_searches >= branch_web_search_max_calls:
                                convergence_reason = "web_search_budget_completed"
                                await self._emit_process_event(
                                    event_sink,
                                    make_process_event(
                                        "task_progress",
                                        "检索完成，正在收敛结论",
                                        "本分支已达到网页检索次数上限，现已停止工具调用；"
                                        "将使用有界收敛窗口，基于已有证据整理最终结论。",
                                        tool="task",
                                        level="active",
                                        metadata={
                                            "task_id": task_id,
                                            "runtime_type": "task_converging",
                                            "role": role,
                                            "completed_web_searches": completed_web_searches,
                                            "search_timeout_suspended": True,
                                            "hard_timeout_seconds": hard_timeout_seconds,
                                        },
                                    ),
                                )
                    if convergence_reason is not None:
                        break
        except (TimeoutError, ResourceLeaseAcquireTimeout) as exc:
            timed_out = True
            errors.append(
                str(exc)
                or f"{role} execution exceeded hard timeout after "
                f"{hard_timeout_seconds:g}s"
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
        finally:
            # 清理链跑在分离任务里并用 shield 等待:链上前两步各有约 1 秒的
            # await,若二次取消(deadline 先取消一次、断连触发的父任务退出回调
            # 再补一刀)落在该窗口,普通 finally 会从中段抛出,续租取消与租约
            # 释放永不执行——孤儿续租每 TTL/3 续一次,容量槽位泄漏到进程重启。
            # 分离任务不受本任务取消影响,保证释放必然执行;期间收到的取消在
            # 清理结束后重新抛出,不吞取消契约。
            async def _release_unit_resources() -> None:
                pending_event_stopped = await _cancel_task_bounded(pending_event)
                with suppress(Exception, asyncio.CancelledError):
                    await close_async_iterator_safely(
                        stream,
                        active_read_task=(
                            None if pending_event_stopped else pending_event
                        ),
                        timeout=1.0,
                    )
                for renewal_task in lease_renewal_tasks:
                    await _cancel_task_bounded(renewal_task)
                for lease in reversed(acquired_leases):
                    try:
                        await lease.release()
                    except ResourceLeaseError:
                        logger.warning(
                            "resource lease release failed: resource=%s owner=%s token=%s",
                            lease.resource,
                            lease.owner_id,
                            lease.fencing_token,
                            exc_info=True,
                        )

            cleanup_task = asyncio.create_task(
                _release_unit_resources(),
                name=f"coordination-unit-cleanup:{unit_id}",
            )
            cleanup_task.add_done_callback(_consume_background_task_result)
            # shield 上的取消照常向外传播(取消契约不变)——分离任务继续在
            # 后台完成续租取消与租约释放,结果由 done callback 消费。
            await asyncio.shield(cleanup_task)
        answer = "".join(answer_parts).strip()
        effective_evidence_refs, effective_evidence_records = (
            self._collect_dynamic_web_evidence(
                process_events,
                evidence_refs=handoff_evidence_refs,
                evidence_records=handoff_evidence_records,
            )
        )
        if convergence_reason is not None:
            remaining_convergence_seconds = min(
                convergence_grace_seconds,
                hard_timeout_seconds - (time.monotonic() - unit_started_at),
            )
            if remaining_convergence_seconds <= 0.01:
                timed_out = True
                errors.append(
                    f"{role} convergence exceeded hard timeout after "
                    f"{hard_timeout_seconds:g}s"
                )
            else:
                convergence_result = await self._host._run_unit_unmanaged(
                    role=role,
                    question=self._build_tool_free_convergence_prompt(
                        objective=question,
                        partial_answer=answer,
                        evidence_refs=effective_evidence_refs,
                        evidence_records=effective_evidence_records,
                    ),
                    request=request,
                    context=context,
                    tool_access=False,
                    event_sink=event_sink,
                    progress_title=progress_title,
                    task_id=f"{task_id}:convergence",
                    handoff_evidence_refs=effective_evidence_refs,
                    handoff_evidence_records=effective_evidence_records,
                    stream_answer=stream_answer,
                    allowed_tools=set(),
                    timeout_override_seconds=remaining_convergence_seconds,
                )
                process_events.extend(convergence_result.process_events)
                if convergence_result.handoff:
                    structured_convergence_handoff = dict(convergence_result.handoff)
                if structured_convergence_handoff:
                    answer = str(
                        structured_convergence_handoff.get("summary") or ""
                    ).strip()
                elif convergence_result.answer.strip():
                    answer = convergence_result.answer.strip()
                cited_convergence_claims = [
                    claim
                    for claim in (convergence_result.handoff or {}).get("claims") or []
                    if str(claim.get("claim") or "").strip()
                    and bool(claim.get("supporting_refs"))
                ]
                recovered_convergence_timeout = bool(
                    convergence_result.timed_out and answer and cited_convergence_claims
                )
                if recovered_convergence_timeout:
                    recovery_event = make_process_event(
                        "task_progress",
                        "证据收敛已保留有效阶段性结论",
                        "收敛模型达到防循环保护上限，但已生成带来源的可核验结论；"
                        "本分支按有效结果继续，未将该保护性停止升级为整分支失败。",
                        tool="task",
                        level="warning",
                        metadata={
                            "task_id": task_id,
                            "runtime_type": "task_convergence_recovered",
                            "role": role,
                            "convergence_timeout_seconds": remaining_convergence_seconds,
                            "cited_claim_count": len(cited_convergence_claims),
                        },
                    )
                    process_events.append(recovery_event)
                    await self._emit_process_event(event_sink, recovery_event)
                else:
                    if convergence_result.error:
                        errors.append(convergence_result.error)
                    timed_out = timed_out or convergence_result.timed_out
        error = "; ".join(filter(None, errors)) or None
        handoff = (
            self._sanitize_structured_convergence_handoff(
                structured_convergence_handoff,
                role=role,
                objective=question,
                answer_fallback=answer,
                task_id=task_id or role,
                evidence_refs=effective_evidence_refs,
                evidence_records=effective_evidence_records,
            )
            if structured_convergence_handoff is not None
            else None
        )
        if handoff is None and task_id and task_id.endswith(":convergence"):
            handoff = self._sanitize_structured_convergence_handoff(
                answer,
                role=role,
                objective=question,
                answer_fallback=answer,
                task_id=task_id,
                evidence_refs=effective_evidence_refs,
                evidence_records=effective_evidence_records,
            )
        if handoff is None:
            handoff = build_subagent_result(
                role=role,
                objective=question,
                answer=answer,
                error=error,
                task_id=task_id or role,
                evidence_refs=effective_evidence_refs,
                evidence_records=effective_evidence_records,
            )
        # Timeout and semantic-turn guards are safety stops, not evidence
        # invalidators.  A branch may already have emitted useful prose and
        # completed retrieval before the next model call trips a guard.  Keep
        # only claims whose supporting references were actually captured by
        # this unit; do not let an uncited claim or a model-invented URL hitch
        # a ride on the recovered handoff.
        protection_stopped = bool(
            error
            and (
                timed_out
                or "semantic model-turn limit" in error.lower()
                or "recursion limit" in error.lower()
            )
        )
        captured_ref_map = self._captured_evidence_ref_map(
            effective_evidence_refs,
            effective_evidence_records,
        )
        recovered_claims: list[dict[str, Any]] = []
        if self.name == "deep_research" and task_id and protection_stopped and answer:
            for raw_claim in handoff.get("claims") or []:
                claim = dict(raw_claim)
                verified_refs = [
                    str(ref).strip().rstrip(").,;:，。；：）")
                    for ref in claim.get("supporting_refs") or []
                    if str(ref).strip().rstrip(").,;:，。；：）") in captured_ref_map
                ]
                if not verified_refs:
                    continue
                claim["supporting_refs"] = list(dict.fromkeys(verified_refs))
                recovered_claims.append(claim)
        if recovered_claims:
            recovered_refs = list(
                dict.fromkeys(
                    ref
                    for claim in recovered_claims
                    for ref in claim.get("supporting_refs") or []
                )
            )
            handoff = normalize_subagent_result(
                {
                    **handoff,
                    "claims": recovered_claims,
                    "evidence_refs": recovered_refs,
                    "validation_warnings": [],
                },
                role=role,
                objective=question,
                answer_fallback=answer,
                task_id=task_id or role,
                evidence_refs=recovered_refs,
                evidence_records=effective_evidence_records,
            )
            recovered_error = error
            error = None
            timed_out = False
            recovery_event = make_process_event(
                "task_progress",
                "已保留保护性停止前的有效证据",
                "分支达到超时或防循环保护上限，但停止前已形成可回溯到实际检索来源的结论；"
                "仅保留这些带来源结论，其余未核验内容已从交接中剔除。",
                tool="task",
                level="warning",
                metadata={
                    "task_id": task_id,
                    "runtime_type": "task_partial_evidence_recovered",
                    "role": role,
                    "recovered_claim_count": len(recovered_claims),
                    "protection_stop": recovered_error,
                },
            )
            process_events.append(recovery_event)
            await self._emit_process_event(event_sink, recovery_event)
        validate_subagent_result_strict(handoff)
        trace.record_unit(
            unit_id=unit_id,
            role=role,
            agent_type=role,
            task_title=progress_title or role,
            task_question=question,
            status="timed_out" if timed_out else "failed" if error else "completed",
            started_at=trace_started_at,
            timeout_seconds=unit_timeout_seconds,
            tool_access=tool_access,
        )
        trace.record_handoff(
            handoff_id=f"{unit_id}:handoff",
            unit_id=unit_id,
            role=role,
            payload=handoff,
        )
        return CoordinationUnitResult(
            role=role,
            answer=answer,
            error=error,
            process_events=tuple(process_events),
            handoff=handoff,
            timed_out=timed_out,
            captured_evidence_refs=tuple(effective_evidence_refs),
        )
