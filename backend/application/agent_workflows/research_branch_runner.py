"""Execution of one isolated coordinated research branch."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from backend.application.agent_executors.stream_utils import (
    extract_web_search_metadata,
    make_process_event,
)
from backend.application.agent_workflows import coordinated_agent_workflow as _legacy
from backend.application.agent_workflows.coordination_component import (
    CoordinationComponent,
)
from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
    ResearchTask,
)
from backend.application.agent_workflows.evidence_verifier import normalize_confidence
from backend.application.tools.runtime_invoker import (
    RuntimeToolInvoker,
    parse_tool_json_result,
)
from backend.core.agents.context.subagent_adapter import (
    extract_evidence_refs,
    format_handoff_bundle,
)
from backend.core.agents.runtime_manager import get_agent_runtime_manager
from backend.core.tools.request_limits import (
    get_retrieval_request_cache,
    reset_turn_kb_context_scope,
    set_turn_kb_context_scope,
)
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.retrieval_policy import RetrievalRuntimePolicy
from backend.domain.agents.runtime_context import AgentRuntimeContext

if TYPE_CHECKING:
    from backend.application.agent_workflows.coordinated_agent_workflow import (
        WorkflowEventSink,
        _AdaptiveAnalysisGate,
    )
else:
    WorkflowEventSink = _legacy.WorkflowEventSink
    _AdaptiveAnalysisGate = _legacy._AdaptiveAnalysisGate
_DEEP_RESEARCH_BRANCH_WEB_CALLS = _legacy._DEEP_RESEARCH_BRANCH_WEB_CALLS
_DEEP_RESEARCH_WEB_MAX_RESULTS = _legacy._DEEP_RESEARCH_WEB_MAX_RESULTS
_ManagedCoordinationUnitFailure = _legacy._ManagedCoordinationUnitFailure
_ManagedCoordinationUnitTimeout = _legacy._ManagedCoordinationUnitTimeout
_cancel_child_on_parent_exit = _legacy._cancel_child_on_parent_exit
_compact_web_repair_evidence = _legacy._compact_web_repair_evidence
_join_unique_errors = _legacy._join_unique_errors
_tool_result_error = _legacy._tool_result_error

_IMAGE_TOKEN_PATTERN = re.compile(r"\[IMAGE:\d+\]")


def _remap_image_tokens_deep(value: Any, remap: dict[str, str]) -> Any:
    """把分支本地 [IMAGE:nnn] 编号替换为主 registry 编号(递归遍历嵌套结构)。

    证据记录的文本字段结构不固定(content/snippet/引用元数据等),按字符串值
    统一处理;非文本值原样返回。
    """
    if isinstance(value, str):
        if "[IMAGE:" not in value:
            return value
        return _IMAGE_TOKEN_PATTERN.sub(
            lambda m: remap.get(m.group(0), m.group(0)), value
        )
    if isinstance(value, dict):
        return {key: _remap_image_tokens_deep(item, remap) for key, item in value.items()}
    if isinstance(value, list):
        return [_remap_image_tokens_deep(item, remap) for item in value]
    if isinstance(value, tuple):
        return tuple(_remap_image_tokens_deep(item, remap) for item in value)
    return value


_RecordFn = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class _ResearchBranchState:
    """单个研究分支在各执行阶段之间传递的共享状态。

    原 ``_run_research_task_unmanaged`` 是约 770 行的单方法,阶段间状态以
    数十个局部变量存在;拆分后由本对象承载,阶段方法只读写自己声明的字段。
    ``task_metadata`` 与 ``task_context.request_metadata`` 是同一个 dict:
    执行计划阶段就地写入的超时键必须能被 ``_run_unit`` 读到,不要替换该引用。
    """

    task: ResearchTask
    role: str
    task_id: str
    selected_model: str | None
    model_tier: str
    branch_web_search_max_calls: int
    task_metadata: dict[str, Any]
    task_context: AgentRuntimeContext
    retrieval_policy: RetrievalRuntimePolicy
    research_deadline: float | None
    role_spec: Any = None
    required_tools: set[str] = field(default_factory=set)
    expected_tools: set[str] = field(default_factory=set)
    role_tool_access: bool = False
    use_web: bool = False
    prefetch_web: bool = False
    use_kb: bool = False
    process_events: list[dict[str, Any]] = field(default_factory=list)
    evidence_parts: list[str] = field(default_factory=list)
    structured_evidence_records: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    prefetch_errors: dict[str, str] = field(default_factory=dict)
    prefetched_tools: set[str] = field(default_factory=set)
    web_task: asyncio.Task[str] | None = None
    web_contract_satisfied: bool = False

    def phase_bounded_timeout(self, maximum_seconds: float) -> float:
        maximum = max(0.001, float(maximum_seconds))
        if self.research_deadline is None:
            return maximum
        remaining = self.research_deadline - time.monotonic()
        # Leave a small scheduler margin for task_end, handoff validation
        # and future completion so the outer phase barrier does not cancel
        # a branch immediately after its final model token.
        handoff_margin = min(2.0, max(0.0, remaining * 0.05))
        return max(
            0.001,
            min(maximum, remaining - handoff_margin),
        )


class ResearchBranchRunner(CoordinationComponent):
    async def _run_research_task(
        self,
        *,
        task: ResearchTask,
        index: int,
        role: str,
        kb_circuit_open: asyncio.Event,
        request: AgentRequest,
        context: AgentRuntimeContext,
        event_sink: WorkflowEventSink | None = None,
        analysis_gate: _AdaptiveAnalysisGate | None = None,
        research_deadline: float | None = None,
        dependency_results: list[CoordinationUnitResult] | None = None,
    ) -> CoordinationUnitResult:
        """Run a branch with an isolated KB slice inside the request budget."""

        scope_token = set_turn_kb_context_scope(
            f"{context.request_id}:{task.key or index}",
            max_chars=self._runtime_budget.coordinated_kb_context_per_branch_chars,
        )
        try:
            return await self._run_research_task_scoped(
                task=task,
                index=index,
                role=role,
                kb_circuit_open=kb_circuit_open,
                request=request,
                context=context,
                event_sink=event_sink,
                analysis_gate=analysis_gate,
                research_deadline=research_deadline,
                dependency_results=dependency_results,
            )
        finally:
            reset_turn_kb_context_scope(scope_token)

    async def _run_research_task_scoped(
        self,
        *,
        task: ResearchTask,
        index: int,
        role: str,
        kb_circuit_open: asyncio.Event,
        request: AgentRequest,
        context: AgentRuntimeContext,
        event_sink: WorkflowEventSink | None = None,
        analysis_gate: _AdaptiveAnalysisGate | None = None,
        research_deadline: float | None = None,
        dependency_results: list[CoordinationUnitResult] | None = None,
    ) -> CoordinationUnitResult:
        """Manage the complete research branch, including prefetch and validation."""

        runtime_manager = get_agent_runtime_manager()
        if runtime_manager is None:
            return await self._host._run_research_task_unmanaged(
                task=task,
                index=index,
                role=role,
                kb_circuit_open=kb_circuit_open,
                request=request,
                context=context,
                event_sink=event_sink,
                analysis_gate=analysis_gate,
                research_deadline=research_deadline,
                dependency_results=dependency_results,
            )

        task_id = f"{context.request_id}:research:{index}"
        selected_model = self._research_task_model_name(
            task=task,
            role=role,
            context=context,
        )
        try:
            async with runtime_manager.spawn(
                task_id,
                metadata={
                    "workflow": self.name,
                    "role": role,
                    "task_id": task_id,
                    "task_key": task.key,
                    "task_title": task.title,
                    "task_budget": task.budget,
                    "model_name": selected_model,
                    "model_tier": self._model_tier(selected_model, context),
                    "thread_id": self._unit_thread_id(
                        context,
                        unit_key=task_id,
                    ),
                    "parent_thread_id": context.internal_thread_id,
                },
            ):
                cancellation = await runtime_manager.token_for(task_id)
                result = await self._host._run_research_task_unmanaged(
                    task=task,
                    index=index,
                    role=role,
                    kb_circuit_open=kb_circuit_open,
                    request=request,
                    context=context,
                    event_sink=event_sink,
                    analysis_gate=analysis_gate,
                    research_deadline=research_deadline,
                    dependency_results=dependency_results,
                )
                cancellation.raise_if_cancelled()
                if result.error:
                    if result.timed_out:
                        raise _ManagedCoordinationUnitTimeout(result)
                    raise _ManagedCoordinationUnitFailure(result)
                return result
        except _ManagedCoordinationUnitFailure as exc:
            return exc.result

    async def _run_research_task_unmanaged(
        self,
        *,
        task: ResearchTask,
        index: int,
        role: str,
        kb_circuit_open: asyncio.Event,
        request: AgentRequest,
        context: AgentRuntimeContext,
        event_sink: WorkflowEventSink | None = None,
        analysis_gate: _AdaptiveAnalysisGate | None = None,
        research_deadline: float | None = None,
        dependency_results: list[CoordinationUnitResult] | None = None,
    ) -> CoordinationUnitResult:
        """顺序执行一个研究分支:准备 → 预检索 → 单元执行 → 契约补修 → 收敛。

        阶段间共享状态经 ``_ResearchBranchState`` 传递;各阶段的行为与拆分前
        逐块对应,事件顺序与错误语义保持不变。
        """

        state = self._prepare_research_branch_state(
            task=task,
            index=index,
            role=role,
            context=context,
            research_deadline=research_deadline,
        )

        async def record(process_event: dict[str, Any]) -> None:
            state.process_events.append(process_event)
            await self._emit_process_event(event_sink, process_event)

        await record(
            make_process_event(
                "task_start",
                task.title,
                task.question,
                tool="task",
                level="active",
                metadata={
                    "task_id": state.task_id,
                    "description": task.title,
                    "subagent_type": role,
                    "runtime_type": "task_started",
                    "task_budget": task.budget,
                    "web_search_max_calls": state.branch_web_search_max_calls,
                    "model_name": state.selected_model,
                    "model_tier": state.model_tier,
                },
            )
        )
        self._resolve_branch_capabilities(state, context=context)
        await self._start_web_prefetch(state, context=context, record=record)
        await self._prefetch_kb_evidence(
            state,
            context=context,
            kb_circuit_open=kb_circuit_open,
            record=record,
        )
        await self._collect_web_prefetch(state, record=record)
        seed_evidence = self._assemble_seed_evidence(
            state,
            dependency_results=dependency_results,
        )
        if analysis_gate is not None:
            await analysis_gate.mark_ready_and_wait()
        await record(
            make_process_event(
                "task_progress",
                "子智能体正在分析证据",
                "检索证据已准备完成，已通过自适应分析门控协调，研究子智能体正在提取事实、出处和争议。",
                tool="task",
                level="active",
                metadata={
                    "task_id": state.task_id,
                    "description": task.title,
                    "subagent_type": role,
                    "runtime_type": "task_running",
                    "scheduler_state": "partial_ready",
                },
            )
        )
        question, branch_allowed_tools, unit_timeout_override = (
            self._build_branch_execution_plan(
                state,
                request=request,
                context=context,
                seed_evidence=seed_evidence,
            )
        )
        result = await self._run_unit(
            role=role,
            question=question,
            request=request,
            context=state.task_context,
            tool_access=state.role_tool_access,
            event_sink=event_sink,
            progress_title=f"子智能体“{task.title}”仍在分析",
            task_id=state.task_id,
            handoff_evidence_refs=extract_evidence_refs(seed_evidence),
            handoff_evidence_records=state.structured_evidence_records,
            allowed_tools=branch_allowed_tools,
            timeout_override_seconds=unit_timeout_override,
        )
        result = await self._repair_missing_web_contract(
            state,
            result,
            request=request,
            event_sink=event_sink,
            record=record,
        )
        return await self._finalize_branch_result(
            state,
            result,
            record=record,
            event_sink=event_sink,
        )

    def _prepare_research_branch_state(
        self,
        *,
        task: ResearchTask,
        index: int,
        role: str,
        context: AgentRuntimeContext,
        research_deadline: float | None,
    ) -> _ResearchBranchState:
        """阶段 1:分支标识、模型选择、预算与请求级上下文(纯构造,无事件)。"""

        task_id = f"{context.request_id}:research:{index}"
        selected_model = self._research_task_model_name(
            task=task,
            role=role,
            context=context,
        )
        model_tier = self._model_tier(selected_model, context)
        branch_web_search_max_calls = (
            _DEEP_RESEARCH_BRANCH_WEB_CALLS
            if self.name == "deep_research"
            else RetrievalRuntimePolicy.for_mode().web_calls
        )
        task_metadata = {
            **dict(context.request_metadata or {}),
            "coordination_task_budget": task.budget,
            "coordination_primary_model_name": context.model_name,
            "coordination_model_name": selected_model,
            "coordination_model_tier": model_tier,
            "coordination_web_search_max_calls": branch_web_search_max_calls,
            "coordination_web_search_max_results": (
                _DEEP_RESEARCH_WEB_MAX_RESULTS
                if self.name == "deep_research"
                else RetrievalRuntimePolicy.for_mode().web_max_results
            ),
        }
        task_context = replace(
            context,
            model_name=selected_model,
            request_metadata=task_metadata,
        )
        return _ResearchBranchState(
            task=task,
            role=role,
            task_id=task_id,
            selected_model=selected_model,
            model_tier=model_tier,
            branch_web_search_max_calls=branch_web_search_max_calls,
            task_metadata=task_metadata,
            task_context=task_context,
            retrieval_policy=RetrievalRuntimePolicy.for_mode(
                deep_research=self.name == "deep_research",
            ),
            research_deadline=research_deadline,
        )

    def _resolve_branch_capabilities(
        self,
        state: _ResearchBranchState,
        *,
        context: AgentRuntimeContext,
    ) -> None:
        """阶段 2:角色能力清单与预检索开关(纯读,无事件)。"""

        state.role_spec = self._role_spec(context, state.role)
        state.required_tools = self._role_required_tools(state.role, context)
        state.expected_tools = set(state.task.expected_tools) | state.required_tools
        state.role_tool_access = self._effective_tool_access(
            role=state.role,
            requested=True,
            context=context,
        )
        state.use_web = (
            context.use_search
            and (not state.expected_tools or "web_search" in state.expected_tools)
            and state.role_tool_access
            and self._role_can_use_network(state.role, context)
        )
        # Deep research delegates query formulation and result-driven query
        # revision to the specialist agent. Plan-Solve keeps its legacy
        # orchestration prefetch behavior for compatibility.
        state.prefetch_web = state.use_web and self.name != "deep_research"
        state.use_kb = (
            bool(context.kb_ids)
            and (
                not state.expected_tools
                or "multi_kb_search" in state.expected_tools
            )
            and state.role_tool_access
            and self._role_can_read_kb(state.role, context)
        )

    async def _start_web_prefetch(
        self,
        state: _ResearchBranchState,
        *,
        context: AgentRuntimeContext,
        record: _RecordFn,
    ) -> None:
        """阶段 3:Plan-Solve 兼容路径的联网预检索并行启动。"""

        if not state.prefetch_web:
            return
        invoker_kwargs: dict[str, Any] = {
            "context": context,
            "workflow_name": self.name,
            "enforce_skill_load": False,
        }
        if self._resolved_definition is not None:
            invoker_kwargs["resolved_agent_definition"] = self._resolved_definition
        tool_invoker: RuntimeToolInvoker = _legacy.RuntimeToolInvoker(**invoker_kwargs)
        await record(
            make_process_event(
                "tool_start",
                "联网检索",
                state.task.question,
                tool="web_search",
                level="active",
                metadata={"task_id": state.task_id, "query": state.task.question},
            )
        )
        state.web_task = asyncio.create_task(
            tool_invoker.invoke(
                "web_search",
                state.retrieval_policy.web_tool_args(state.task.question),
            )
        )
        _cancel_child_on_parent_exit(state.web_task)

    async def _prefetch_kb_evidence(
        self,
        state: _ResearchBranchState,
        *,
        context: AgentRuntimeContext,
        kb_circuit_open: asyncio.Event,
        record: _RecordFn,
    ) -> None:
        """阶段 4:知识库预检索(熔断、图片 token 桥接、证据入列)。"""

        if not state.use_kb:
            return
        # Materialize the request cache before spawning the tool child task so
        # its copied Context inherits this exact shared object.
        retrieval_request_cache = get_retrieval_request_cache()
        await record(
            make_process_event(
                "tool_start",
                "检索已选知识库",
                state.task.question,
                tool="multi_kb_search",
                level="active",
                metadata={
                    "task_id": state.task_id,
                    "query": state.task.question,
                    "kb_ids": list(context.kb_ids),
                },
            )
        )
        try:
            if kb_circuit_open.is_set():
                raise RuntimeError("本次研究的知识库检索已因前序分支超时而停止")
            kb_timeout_seconds = state.phase_bounded_timeout(
                self._runtime_budget.kb_search_timeout_seconds
            )
            try:
                kb_result = await asyncio.wait_for(
                    self._invoke_runtime_tool_structured(
                        context,
                        self.name,
                        "multi_kb_search",
                        state.retrieval_policy.kb_tool_args(state.task.question),
                        _legacy.RuntimeToolInvoker,
                        self._resolved_definition,
                    ),
                    timeout=kb_timeout_seconds,
                )
            except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
                nominal_timeout = float(
                    self._runtime_budget.kb_search_timeout_seconds
                )
                # 只有以(接近)名义引信超时才说明知识库本身故障。晚启动分支的
                # 引信被 phase_bounded_timeout 按剩余时间收缩(补研分支可低至
                # ~90 秒),用收缩引信对全请求拉闸会让后续证据恢复分支全部死在
                # 无 KB 的通道里;此时只让本分支放弃预取。
                circuit_worthy = kb_timeout_seconds >= nominal_timeout * 0.9
                if circuit_worthy:
                    kb_circuit_open.set()
                    retrieval_request_cache.cancel_provider_requests()
                raise RuntimeError(
                    "知识库检索超过 "
                    f"{kb_timeout_seconds:g} 秒，"
                    + (
                        "已停止本次研究的后续知识库检索"
                        if circuit_worthy
                        else "本分支放弃知识库预取"
                    )
                ) from exc
            kb_text = str(kb_result.get("text") or "")
            kb_stats = dict(kb_result.get("stats") or {})
            if kb_stats.get("context_budget_exhausted"):
                raise RuntimeError(
                    kb_text or "知识库检索上下文预算已用尽，未获得可注入证据"
                )
            if kb_stats.get("cancelled"):
                # 熔断后的占位结果是"成功形状"的取消告示:当成证据注入会把
                # 取消文本喂给专家提示词,还让 prefetched_tools 契约核验误判
                # multi_kb_search 已成功完成,缺失 KB 维度不进降级通道。
                raise RuntimeError(
                    kb_text or "知识库检索已因熔断取消，未获得可注入证据"
                )
            branch_evidence_records = list(
                kb_result.get("evidence_records") or []
            )

            # Bridge registered images back to the main-thread registry.
            # 必须走 register()：并发分支各自从 001 编号，直接写 _entries 会
            # token 串号，且用裸 url 作 key 污染去重表。register() 加锁分配主
            # registry 的唯一 token，再把本分支证据文本里的旧 token 一次性重映射。
            registered_images = kb_result.get("registered_images") or []
            if registered_images:
                from backend.application.agent_executors.image_url_registry import (
                    get_registry,
                )

                registry = get_registry()
                token_remap: dict[str, str] = {}
                for img in registered_images:
                    worker_token = img.get("token")
                    url = img.get("url")
                    caption = img.get("caption") or ""
                    if not worker_token or not url:
                        continue
                    new_token = registry.register(url, caption)
                    if new_token != worker_token:
                        token_remap[worker_token] = new_token
                if token_remap:
                    kb_text = re.sub(
                        r"\[IMAGE:\d+\]",
                        lambda m: token_remap.get(m.group(0), m.group(0)),
                        kb_text,
                    )
                    # 证据记录里的文本同样带分支本地编号:不重映射的话,
                    # 残留的 worker token 会被答案管线当未注册引用删除,
                    # 分支图片证据静默丢失。必须在合并进汇总列表前完成。
                    branch_evidence_records = [
                        _remap_image_tokens_deep(record_item, token_remap)
                        for record_item in branch_evidence_records
                    ]

            state.structured_evidence_records.extend(branch_evidence_records)

            state.evidence_parts.append("### 知识库证据\n" + kb_text)
            state.prefetched_tools.add("multi_kb_search")
            await record(
                make_process_event(
                    "tool_result",
                    "知识库检索完成",
                    "已将选中知识库的召回结果交给子智能体。",
                    tool="multi_kb_search",
                    level="success",
                    metadata={
                        "task_id": state.task_id,
                        "query": state.task.question,
                        "kb_ids": list(context.kb_ids),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            state.prefetch_errors["multi_kb_search"] = f"知识库检索失败: {exc}"
            await record(
                make_process_event(
                    "tool_result",
                    "知识库检索失败",
                    str(exc),
                    tool="multi_kb_search",
                    level="warning",
                    metadata={"task_id": state.task_id, "query": state.task.question},
                )
            )

    async def _collect_web_prefetch(
        self,
        state: _ResearchBranchState,
        *,
        record: _RecordFn,
    ) -> None:
        """阶段 5:收取并行联网预检索的结果或失败。"""

        if state.web_task is None:
            return
        try:
            web_result = await asyncio.wait_for(
                state.web_task,
                timeout=state.phase_bounded_timeout(
                    self._runtime_budget.web_search_timeout_seconds
                ),
            )
            state.evidence_parts.append("### 联网证据\n" + str(web_result))
            state.prefetched_tools.add("web_search")
            await record(
                make_process_event(
                    "tool_result",
                    "联网检索完成",
                    "已将网页检索结果交给子智能体。",
                    tool="web_search",
                    level="success",
                    metadata={
                        "task_id": state.task_id,
                        "query": state.task.question,
                        **extract_web_search_metadata(str(web_result)),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            state.prefetch_errors["web_search"] = f"联网检索失败: {exc}"
            await record(
                make_process_event(
                    "tool_result",
                    "联网检索失败",
                    str(exc),
                    tool="web_search",
                    level="warning",
                    metadata={"task_id": state.task_id, "query": state.task.question},
                )
            )

    def _assemble_seed_evidence(
        self,
        state: _ResearchBranchState,
        *,
        dependency_results: list[CoordinationUnitResult] | None,
    ) -> str:
        """阶段 6:依赖任务交接注入与种子证据拼装。"""

        successful_dependency_results = [
            result
            for result in dependency_results or []
            if not result.error and result.answer.strip() and result.handoff
        ]
        failed_dependency_results = [
            result
            for result in dependency_results or []
            if result.error or not result.answer.strip() or not result.handoff
        ]
        dependency_handoffs = format_handoff_bundle(
            [
                result.handoff
                for result in successful_dependency_results
                if result.handoff
            ],
            max_chars=10000,
        )
        if dependency_handoffs:
            state.evidence_parts.insert(0, "### 依赖任务交接\n" + dependency_handoffs)
        if failed_dependency_results:
            dependency_gaps = "\n".join(
                "- "
                + (result.task_title or result.task_key or result.role)
                + ": "
                + (result.error or "未返回可用结论")[:500]
                for result in failed_dependency_results[:12]
            )
            state.evidence_parts.insert(
                0,
                "### 依赖证据缺口（不得作为事实依据）\n"
                + dependency_gaps
                + "\n请仅使用其余成功交接形成条件式结论，并明确这些缺口的影响。",
            )
        seed_evidence = "\n\n".join(state.evidence_parts)
        if not seed_evidence:
            seed_evidence = (
                "未预加载联网证据；web_search 已交由本子智能体自主调用。"
                if state.use_web and self.name == "deep_research"
                else "当前未配置可用的知识库或联网检索。"
            )
        return seed_evidence

    def _build_branch_execution_plan(
        self,
        state: _ResearchBranchState,
        *,
        request: AgentRequest,
        context: AgentRuntimeContext,
        seed_evidence: str,
    ) -> tuple[str, set[str] | None, float | None]:
        """阶段 7:专家提示、执行契约、工具白名单与单元超时的装配。

        对 ``state.task_metadata`` 的超时键是就地写入,经共享引用同步进
        ``state.task_context.request_metadata``。
        """

        specialist_prompt = (self._role_instruction(state.role, context) or "").strip()
        specialist_prompt = (specialist_prompt + "\n\n") if specialist_prompt else ""
        execution_contract = ""
        if self.name == "deep_research":
            execution_contract += (
                "研究工具纪律：当前分支的技能已经由主控预选；如需领域规则，只调用 read_local_skill_file "
                "读取相关 SKILL.md 一次，随后立即进入知识库或网络证据检索。不要枚举技能，不要猜测或遍历 Skill "
                "参考路径，不要用读取说明文件代替检索。获得足以支撑本子问题的证据后立即总结并结束分支。\n\n"
            )
        if self.name == "deep_research" and state.use_web:
            execution_contract += (
                "联网研究约束：你必须自主调用 web_search 开展检索，不要把完整任务说明直接作为搜索词。"
                "先从较宽的单一检索意图开始，再针对证据缺口缩窄；每次工具调用只搜索一个意图。"
                "每次阅读返回的标题、摘要和来源后，先总结已找到内容与仍缺内容，再决定下一次搜索。"
                "若结果偏题、过泛或不足，必须改写术语、限定条件或语言继续检索，而不是接受无关结果。"
                "不同论点应分别检索；在条件允许时让关键结论获得两个相互独立的相关来源。"
                "当所需论点已覆盖，或连续两次检索不再产生新证据时停止；"
                f"单个分支最多调用 {state.branch_web_search_max_calls} 次 web_search。"
                "最后一次检索结果会明确提示检索预算已用完；看到该提示后禁止再次调用工具，"
                "必须立即基于已有证据整理结论。"
                "结论中每项事实必须紧邻保留工具实际返回的 URL；不要只写无法解析回 URL 的 [1]、[^1] "
                "等孤立编号，也不要自行生成未检索到的参考文献。\n\n"
            )
        if "query_project_tables" in state.expected_tools:
            execution_contract += (
                "执行约束：本分支必须至少实际调用一次 query_project_tables 获取当前项目数据后才能形成数据结论；"
                "输出‘准备查询/正在查询’不算完成工具调用。若查询失败，必须明确报告失败原因和缺失数据，"
                "不得用预加载的表结构、模型知识或文字叙述冒充项目实测结果。\n\n"
            )
        # ``expected_tools`` is an execution contract, but not the branch
        # security ACL. Restricting the branch to that subset can remove
        # mandatory skill-control tools (and other safe role capabilities).
        # The role manifest remains the capability ceiling; contract success
        # is checked after execution and already-prefetched tools are removed.
        if state.role_spec is not None and state.role_spec.allowed_tools is not None:
            branch_allowed_tools = (
                set(state.role_spec.allowed_tools) - state.prefetched_tools
            )
        else:
            branch_allowed_tools = None
        if state.role_spec is not None and state.role_spec.required_tools:
            branch_allowed_tools = set(branch_allowed_tools or set()) | (
                set(state.role_spec.required_tools) - state.prefetched_tools
            )
        unit_timeout_override: float | None = None
        if state.research_deadline is not None:
            # Deep-research branches are governed by the shared research-phase
            # deadline plus idle/search/turn loop guards.  Capping a healthy,
            # still-streaming branch at the role's legacy 300 seconds leaves
            # too little room for evidence convergence and does not protect the
            # writer (the writer already owns a separate fresh budget).
            unit_timeout_override = state.phase_bounded_timeout(
                self._runtime_budget.deep_research_branch_total_timeout_seconds
                if self.name == "deep_research"
                else self._role_timeout_seconds(state.role, state.task_context)
            )
            state.task_metadata["coordination_effective_unit_timeout_seconds"] = (
                unit_timeout_override
            )
            state.task_metadata["coordination_timeout_source"] = (
                "research_phase_remaining"
                if self.name == "deep_research"
                else "role_timeout_bounded_by_phase"
            )
        question = (
            specialist_prompt
            + execution_contract
            + "你是由深度研究监督器调度的独立专业子智能体。请围绕分配的子问题分析下方检索证据，提取事实、"
            "数据、出处、冲突和证据缺口，形成可供最终报告直接引用的研究结论。"
            "若证据不足，可使用当前允许的工具继续核验，但不要编造引用。\n\n"
            f"原始任务：\n{request.question}\n\n"
            f"子问题：\n{state.task.question}\n\n"
            f"编排层预检索证据：\n{seed_evidence}"
        )
        return question, branch_allowed_tools, unit_timeout_override

    async def _repair_missing_web_contract(
        self,
        state: _ResearchBranchState,
        result: CoordinationUnitResult,
        *,
        request: AgentRequest,
        event_sink: WorkflowEventSink | None,
        record: _RecordFn,
    ) -> CoordinationUnitResult:
        """阶段 8:深研分支漏掉必需 web_search 时的编排层补做与结论修订。"""

        branch_process_events = list(result.process_events)
        initially_completed_tools = {
            str(event.get("tool") or "").strip()
            for event in result.process_events
            if event.get("kind") == "tool_result"
            and str(event.get("level") or "").lower() not in {"warning", "error"}
        } | set(state.prefetched_tools)
        state.web_contract_satisfied = "web_search" in initially_completed_tools
        if (
            self.name == "deep_research"
            and state.use_web
            and "web_search" in state.expected_tools
            and not state.web_contract_satisfied
        ):
            fallback_args = state.retrieval_policy.web_tool_args(state.task.question)
            fallback_args["max_results"] = _DEEP_RESEARCH_WEB_MAX_RESULTS
            await record(
                make_process_event(
                    "tool_start",
                    "补做必需联网检索",
                    state.task.question,
                    tool="web_search",
                    level="active",
                    metadata={
                        "task_id": state.task_id,
                        "query": state.task.question,
                        "input": dict(fallback_args),
                        "runtime_type": "contract_tool_repair_started",
                    },
                )
            )
            try:
                repair_context = replace(
                    state.task_context,
                    internal_thread_id=self._unit_thread_id(
                        state.task_context,
                        unit_key=state.task_id,
                    ),
                )
                invoker_kwargs = {
                    "context": repair_context,
                    "workflow_name": self.name,
                    "enforce_skill_load": False,
                }
                if self._resolved_definition is not None:
                    invoker_kwargs["resolved_agent_definition"] = (
                        self._resolved_definition
                    )
                fallback_invoker = _legacy.RuntimeToolInvoker(**invoker_kwargs)
                raw_web_result = await asyncio.wait_for(
                    fallback_invoker.invoke("web_search", fallback_args),
                    timeout=state.phase_bounded_timeout(
                        self._runtime_budget.web_search_timeout_seconds
                    ),
                )
                web_payload = parse_tool_json_result(raw_web_result)
                web_error = _tool_result_error(web_payload)
                if web_error:
                    raise RuntimeError(web_error)
                web_metadata = extract_web_search_metadata(raw_web_result)
                web_refs = list(web_metadata.get("urls") or [])
                if not web_refs:
                    raise RuntimeError("联网检索未返回可验证的网页来源")
                await record(
                    make_process_event(
                        "tool_result",
                        "必需联网检索已补齐",
                        "已获得可验证网页来源，正在让子智能体据此修订结论。",
                        tool="web_search",
                        level="success",
                        metadata={
                            "task_id": state.task_id,
                            "query": str(
                                web_payload.get("query") or state.task.question
                            ),
                            "input": dict(fallback_args),
                            "runtime_type": "contract_tool_repair_search_completed",
                            **web_metadata,
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                repair_error = f"必需联网检索补做失败: {exc}"
                state.errors.append(repair_error)
                await record(
                    make_process_event(
                        "tool_result",
                        "必需联网检索补做失败",
                        str(exc),
                        tool="web_search",
                        level="warning",
                        metadata={
                            "task_id": state.task_id,
                            "query": state.task.question,
                            "input": dict(fallback_args),
                            "runtime_type": "contract_tool_repair_failed",
                        },
                    )
                )
            else:
                repair_evidence = _compact_web_repair_evidence(
                    web_payload,
                    raw_web_result,
                )
                try:
                    # This is a continuation stage of the already managed
                    # branch, not another independent subagent.  Execute it
                    # unmanaged with a fresh checkpoint id so it neither
                    # inherits semantic turns nor consumes tree depth/spawn
                    # capacity while the parent waits for it.
                    repair_result = await self._run_unit_unmanaged(
                        role=state.role,
                        question=(
                            "你上一版研究结论没有执行规划要求的 web_search。编排层已经完成一次受限联网检索。"
                            "请仅依据下列真实检索结果和上一版内容修订本分支结论；保留每项事实对应的实际 URL，"
                            "明确证据冲突或不足，不要调用任何工具，也不要生成检索结果中不存在的来源。\n\n"
                            f"原始任务：\n{request.question}\n\n"
                            f"子问题：\n{state.task.question}\n\n"
                            f"上一版内容：\n{result.answer[:10000]}\n\n"
                            f"补充联网证据：\n{repair_evidence}"
                        ),
                        request=request,
                        context=state.task_context,
                        tool_access=False,
                        event_sink=event_sink,
                        progress_title=f"子智能体“{state.task.title}”正在吸收补充证据",
                        # Use a fresh checkpoint/semantic-turn counter.  Reusing
                        # the original branch task id makes a legitimate
                        # evidence-repair pass inherit all prior model turns and
                        # can immediately trip the role's turn-loop guard.
                        task_id=f"{state.task_id}:web-repair",
                        handoff_evidence_refs=web_refs,
                        handoff_evidence_records=state.structured_evidence_records,
                        allowed_tools=set(),
                        timeout_override_seconds=(
                            state.phase_bounded_timeout(
                                self._role_timeout_seconds(
                                    state.role,
                                    state.task_context,
                                )
                            )
                            if state.research_deadline is not None
                            else None
                        ),
                    )
                    branch_process_events.extend(repair_result.process_events)
                    if repair_result.error:
                        raise RuntimeError(repair_result.error)
                    if not repair_result.answer.strip():
                        raise RuntimeError("子智能体未返回修订后的研究结论")
                except Exception as exc:  # noqa: BLE001
                    repair_error = f"联网证据吸收失败: {exc}"
                    state.errors.append(repair_error)
                    await record(
                        make_process_event(
                            "task_progress",
                            "联网证据吸收失败",
                            str(exc),
                            tool="task",
                            level="warning",
                            metadata={
                                "task_id": state.task_id,
                                "runtime_type": "contract_answer_repair_failed",
                            },
                        )
                    )
                else:
                    state.prefetched_tools.add("web_search")
                    state.web_contract_satisfied = True
                    result = replace(
                        repair_result,
                        process_events=tuple(branch_process_events),
                    )
                    await record(
                        make_process_event(
                            "task_progress",
                            "已根据补充联网证据修订结论",
                            "分支已吸收真实网页证据并保留来源 URL。",
                            tool="task",
                            level="success",
                            metadata={
                                "task_id": state.task_id,
                                "runtime_type": "contract_answer_repair_completed",
                            },
                        )
                    )
        return result

    async def _finalize_branch_result(
        self,
        state: _ResearchBranchState,
        result: CoordinationUnitResult,
        *,
        record: _RecordFn,
        event_sink: WorkflowEventSink | None,
    ) -> CoordinationUnitResult:
        """阶段 9:工具契约核验、错误合并、终态事件与交接发布。"""

        all_branch_events = (*state.process_events, *result.process_events)
        invoked_tools = sorted(
            {
                str(event.get("tool") or "").strip()
                for event in all_branch_events
                if event.get("kind") == "tool_start"
                and str(event.get("tool") or "").strip()
            }
        )
        completed_tools = {
            str(event.get("tool") or "").strip()
            for event in all_branch_events
            if event.get("kind") == "tool_result"
            and str(event.get("level") or "").lower() not in {"warning", "error"}
        } | set(state.prefetched_tools)
        # A prefetch failure is recoverable when the isolated specialist later
        # completes the same tool itself. Keeping the stale error would mark
        # valid evidence as degraded and make the writer reject it.
        state.errors.extend(
            error
            for tool_name, error in state.prefetch_errors.items()
            if tool_name not in completed_tools
        )
        completed_tools_for_contract = set(completed_tools)
        if not state.web_contract_satisfied:
            completed_tools_for_contract.discard("web_search")
        combined_error = _join_unique_errors(*state.errors, result.error)
        search_queries: list[str] = []
        for event in all_branch_events:
            if str(event.get("tool") or "").strip() != "web_search":
                continue
            metadata = event.get("metadata") or {}
            tool_input = metadata.get("input") if isinstance(metadata, dict) else None
            raw_query = (
                tool_input.get("query")
                if isinstance(tool_input, dict)
                else metadata.get("query") if isinstance(metadata, dict) else None
            )
            query = str(raw_query or "").strip()
            if query and query not in search_queries:
                search_queries.append(query)
        # ``expected_tools`` is the planner's execution contract, not only a
        # prompt hint.  Treat every declared tool consistently: the branch must
        # complete it successfully before its conclusions can be trusted.  The
        # planner/parser already removes capabilities that are unavailable for
        # the current request (for example KB search without selected KBs).
        contract_tools = set(state.required_tools) | set(state.expected_tools)
        missing_required_tools = sorted(contract_tools - completed_tools_for_contract)
        if missing_required_tools:
            contract_error = (
                "必需工具未完成："
                + "、".join(missing_required_tools)
                + "；该分支相关结论不可采信"
            )
            state.errors.append(contract_error)
            handoff = dict(result.handoff or {})
            handoff["confidence"] = min(
                normalize_confidence(handoff.get("confidence"), default=0.0),
                0.2,
            )
            handoff["missing"] = list(handoff.get("missing") or []) + [contract_error]
            for claim in handoff.get("claims") or []:
                claim["missing"] = list(claim.get("missing") or []) + [contract_error]
                claim["confidence"] = min(
                    normalize_confidence(claim.get("confidence"), default=0.0),
                    0.2,
                )
            result = replace(result, error=contract_error, handoff=handoff)
            combined_error = _join_unique_errors(*state.errors, result.error)
        final_error = (
            combined_error
            or result.error
            or ("子任务未返回内容" if not result.answer.strip() else None)
        )
        task_succeeded = not final_error and bool(result.answer.strip())
        task_failure_detail = final_error or (
            None if result.answer.strip() else "子任务未返回内容"
        )
        task_timed_out = bool(
            task_failure_detail
            and (
                "timed out" in task_failure_detail.lower()
                or "timeout" in task_failure_detail.lower()
                or "超时" in task_failure_detail
            )
        )
        task_runtime_type = (
            "task_completed"
            if task_succeeded
            else "task_timed_out" if task_timed_out else "task_failed"
        )
        await record(
            make_process_event(
                "task_end",
                state.task.title,
                result.answer[:500] if task_succeeded else task_failure_detail,
                tool="task",
                level="success" if task_succeeded else "warning",
                metadata={
                    "task_id": state.task_id,
                    "description": state.task.title,
                    "subagent_type": state.role,
                    "runtime_type": task_runtime_type,
                    "partial_handoff": bool(result.handoff),
                    "scheduler_state": "partial_handoff",
                    "search_queries": search_queries,
                    "tool_calls": invoked_tools,
                },
            )
        )
        if result.handoff:
            await self._emit_event(
                event_sink,
                "coordination.partial_handoff",
                {
                    "workflow": self.name,
                    "role": state.role,
                    "task_id": state.task_id,
                    "handoff": result.handoff,
                    "valid": task_succeeded,
                    "degraded": not task_succeeded,
                    "error": task_failure_detail,
                    "metadata": {
                        "scheduler_state": "partial_handoff",
                        "partial_handoff": True,
                        "task_status": task_runtime_type,
                        "degraded": not task_succeeded,
                        "error": task_failure_detail,
                        "resource": {
                            "kb_capacity": 1,
                            "llm_capacity": self._research_llm_capacity(),
                        },
                    },
                },
            )
        return replace(
            result,
            error=final_error,
            process_events=(*state.process_events, *result.process_events),
            task_key=state.task.key,
            task_title=state.task.title,
        )
