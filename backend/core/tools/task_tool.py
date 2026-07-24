"""Local task tool for delegated subagent execution."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from typing import Annotated, Any

from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.config import get_stream_writer
from langgraph.typing import ContextT

from backend.application.agent_executors.stream_utils import (
    extract_chat_model_output_text,
    extract_task_result_metadata,
    stringify_message_content,
    summarize_tool_output,
)
from backend.application.agents.runtime_agent_loader import RuntimeAgentLoader
from backend.application.tools.execution import raise_for_tool_reported_error
from backend.application.tools.tool_assembly_pipeline import ToolAssemblyPipeline
from backend.core.agents import (
    RuntimeFeatures,
    ThreadState,
    create_agent_graph,
)
from backend.core.agents.context.subagent_adapter import (
    build_subagent_result,
    format_subagent_handoff,
    validate_subagent_result_strict,
)
from backend.core.agents.middlewares.runtime_policy import (
    RuntimePolicyGuardrailProvider,
)
from backend.core.agents.middlewares.tool_guardrail import ToolGuardrailMiddleware
from backend.core.agents.runtime_manager import (
    AgentCancellationToken,
    get_agent_runtime_manager,
)
from backend.core.models import create_chat_model
from backend.core.skills.skill_runtime_state import (
    reset_request_skill_state,
    set_request_skill_state,
)
from backend.core.tools.request_limits import get_subagent_execution_budget
from backend.domain.agents.runtime_agent import RuntimeAgentSpec
from backend.domain.tools.runtime_context import ToolRuntimeContext

logger = logging.getLogger(__name__)


_RUNTIME_AGENT_LOADER = RuntimeAgentLoader()


def _trim_text(value: Any, max_chars: int = 500) -> str:
    text = summarize_tool_output(value, max_length=max_chars)
    return text[:max_chars] + "..." if len(text) > max_chars else text


def _extract_chunk_text(chunk: Any) -> str:
    if hasattr(chunk, "content"):
        return stringify_message_content(chunk.content)
    return stringify_message_content(chunk)


def _emit_task_event(writer, payload: dict[str, Any]) -> None:
    try:
        writer(payload)
    except Exception:
        logger.debug("task runtime event emit failed", exc_info=True)


def _wrap_tool(
    tool_item: BaseTool,
    *,
    writer,
    task_id: str,
    subagent_type: str,
    description: str,
    trace_id: str,
    invoked_tools: set[str] | None = None,
) -> BaseTool:
    raw_metadata = getattr(tool_item, "metadata", None)
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}

    async def _invoke(**kwargs: Any):
        _emit_task_event(
            writer,
            {
                "type": "task_tool_start",
                "task_id": task_id,
                "tool": tool_item.name,
                "detail": f"子智能体开始调用工具: {tool_item.name}",
                "description": description,
                "subagent_type": subagent_type,
                "trace_id": trace_id,
            },
        )
        try:
            # The child graph's ToolErrorHandlingMiddleware owns timeout,
            # concurrency, retry, output and cache policy. Calling the service
            # wrapper here as well would apply retries twice.
            result = await tool_item.ainvoke(dict(kwargs))
            raise_for_tool_reported_error(result)
        except Exception as exc:
            _emit_task_event(
                writer,
                {
                    "type": "task_tool_end",
                    "task_id": task_id,
                    "tool": tool_item.name,
                    "detail": f"子智能体工具执行失败: {tool_item.name}",
                    "summary": _trim_text(str(exc), 300),
                    "description": description,
                    "subagent_type": subagent_type,
                    "trace_id": trace_id,
                    "metadata": {"status": "error"},
                },
            )
            raise
        _emit_task_event(
            writer,
            {
                "type": "task_tool_end",
                "task_id": task_id,
                "tool": tool_item.name,
                "detail": f"子智能体工具执行完成: {tool_item.name}",
                "summary": _trim_text(result, 300),
                "description": description,
                "subagent_type": subagent_type,
                "trace_id": trace_id,
            },
        )
        if invoked_tools is not None:
            invoked_tools.add(tool_item.name)
        return result

    args_schema = getattr(tool_item, "args_schema", None)
    kwargs: dict[str, Any] = {
        "name": tool_item.name,
        "description": tool_item.description or tool_item.name,
        "coroutine": _invoke,
    }
    if args_schema is not None:
        kwargs["args_schema"] = args_schema
    if metadata:
        kwargs["metadata"] = metadata
    return StructuredTool.from_function(**kwargs)


def _tool_metadata_by_name(tools: list[BaseTool]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for tool_item in tools:
        raw_metadata = getattr(tool_item, "metadata", None)
        if isinstance(raw_metadata, dict):
            metadata[tool_item.name] = dict(raw_metadata)
    return metadata


def _build_subagent_tools(
    *,
    subagent_cfg: RuntimeAgentSpec,
    model_name: str | None,
    use_search: bool,
    kb_ids: list[str],
    selected_skills: set[str] | None,
    thread_id: str | None,
    domain: str | None,
    workflow_name: str | None,
    tool_policy: str | None,
    tool_runtime_context: ToolRuntimeContext | None,
    writer,
    task_id: str,
    description: str,
    trace_id: str,
    invoked_tools: set[str] | None = None,
) -> list[BaseTool]:
    if not domain or not tool_policy:
        raise RuntimeError("subagent tool assembly requires domain and tool_policy runtime context")
    pipeline = ToolAssemblyPipeline(
        model_name=model_name,
        subagent_enabled=False,
        use_search=use_search and subagent_cfg.can_use_network,
        kb_ids=kb_ids if subagent_cfg.can_read_kb else [],
        selected_skills=selected_skills,
        task_type="deep_research" if subagent_cfg.name == "deep-research" else None,
        thread_id=thread_id,
        domain=domain,
        workflow_name=workflow_name,
        tool_policy=tool_policy,
        tool_runtime_context=tool_runtime_context,
    )
    tools = pipeline.assemble()

    disallowed = set(subagent_cfg.disallowed_tools or set()) | {"task", "present_files"}
    allowed_tools = set(subagent_cfg.allowed_tools) if subagent_cfg.allowed_tools is not None else None
    required_tools = set(subagent_cfg.required_tools or set())
    if allowed_tools is not None:
        allowed_tools |= required_tools
    if (
        allowed_tools is not None
        and subagent_cfg.name == "deep-research"
        and selected_skills
        and "academic-search" in selected_skills
    ):
        allowed_tools = allowed_tools | {"academic_search"}
    filtered: list[BaseTool] = []
    for tool_item in tools:
        if tool_item.name in disallowed:
            continue
        if allowed_tools is not None and tool_item.name not in allowed_tools:
            continue
        filtered.append(
            _wrap_tool(
                tool_item,
                writer=writer,
                task_id=task_id,
                subagent_type=subagent_cfg.name,
                description=description,
                trace_id=trace_id,
                invoked_tools=invoked_tools,
            )
        )
    return filtered


async def _run_subagent(
    *,
    subagent_cfg: RuntimeAgentSpec,
    description: str,
    prompt: str,
    model_name: str | None,
    max_turns: int,
    thread_id: str | None,
    kb_ids: list[str],
    use_search: bool,
    selected_skills: set[str] | None,
    domain: str | None,
    workflow_name: str | None,
    tool_policy: str | None,
    tool_runtime_context: ToolRuntimeContext | None,
    task_id: str,
    trace_id: str,
    runtime_manager_managed: bool = False,
    cancellation_token: AgentCancellationToken | None = None,
    context_policy: Any | None = None,
    checkpointer: Any | None = None,
) -> str:
    writer = get_stream_writer()
    invoked_tools: set[str] = set()
    tools = _build_subagent_tools(
        subagent_cfg=subagent_cfg,
        model_name=model_name,
        use_search=use_search,
        kb_ids=kb_ids,
        selected_skills=selected_skills,
        thread_id=thread_id,
        domain=domain,
        workflow_name=workflow_name,
        tool_policy=tool_policy,
        tool_runtime_context=tool_runtime_context,
        writer=writer,
        task_id=task_id,
        description=description,
        trace_id=trace_id,
        invoked_tools=invoked_tools,
    )
    system_prompt = subagent_cfg.system_prompt
    if selected_skills and "academic-search" in selected_skills:
        system_prompt += """

- 已启用学术搜索：文献/论文/引用类检索优先使用 `academic_search`；`web_search` 仅补充时效资讯。
"""
    effective_model_name = (
        subagent_cfg.model
        if subagent_cfg.model and subagent_cfg.model != "default"
        else model_name
    )
    effective_use_search = use_search and subagent_cfg.can_use_network
    effective_kb_ids = kb_ids if subagent_cfg.can_read_kb else []
    model = create_chat_model(name=effective_model_name, thinking_enabled=False)
    # Delegated agents used to bypass the parent graph's context policy except
    # for one groundwater-only branch.  Give every child the same evidence
    # capture, JIT context assembly and history summarization contract.
    from backend.core.agents.context import ContextPolicy
    from backend.core.agents.middlewares.context_pruning import ContextPruningMiddleware
    from backend.core.agents.middlewares.model_turn_limit import (
        ModelTurnLimitMiddleware,
    )

    context_policy = context_policy or ContextPolicy.from_env(effective_model_name)
    extra_middleware = [
        ContextPruningMiddleware(
            thread_id=f"{thread_id or task_id}:subagent:{task_id}",
            model_name=effective_model_name,
            user_id=(
                str(tool_runtime_context.user_id or "").strip()
                if tool_runtime_context is not None
                else None
            ),
            policy=context_policy,
            checkpointer=checkpointer,
        ),
        ModelTurnLimitMiddleware(max_turns),
    ]
    graph = create_agent_graph(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        features=RuntimeFeatures(
            # ContextPruningMiddleware is the child graph's single compaction
            # owner; do not run a second framework history rewrite.
            summarization=False,
            clarification=False,
            subagent=False,
            vision=False,
            guardrail=ToolGuardrailMiddleware(
                provider=RuntimePolicyGuardrailProvider(
                    allowed_tools=[tool_item.name for tool_item in tools],
                    guardrail_policy=tool_policy or "default",
                    permissions=dict(
                        tool_runtime_context.permissions
                        if tool_runtime_context is not None
                        else {}
                    ),
                ),
                passport=f"subagent:{subagent_cfg.name}:{tool_policy or 'default'}",
                tool_metadata=_tool_metadata_by_name(tools),
            ),
        ),
        extra_middleware=extra_middleware,
        name=f"subagent-{subagent_cfg.name}",
        state_schema=ThreadState,
        checkpointer=checkpointer,
    )

    effective_thread_id = f"{thread_id or 'subagent-exec'}:subagent:{task_id}"
    invoke_config = {
        "configurable": {
            "thread_id": effective_thread_id,
            "kb_ids": effective_kb_ids,
            "selected_skills": sorted(selected_skills) if selected_skills else [],
            "use_search": effective_use_search,
        },
        "metadata": {
            "model_name": effective_model_name,
            "runtime_agent": subagent_cfg.name,
            "runtime_agent_output_schema": subagent_cfg.output_schema,
            "runtime_agent_required_tools": sorted(subagent_cfg.required_tools),
            "trace_id": trace_id,
            "subagent_type": subagent_cfg.name,
        },
        # LangGraph recursion_limit counts internal graph steps (model call +
        # tool call each count as one step), not user-facing conversation turns.
        # A single "turn" with tool use typically consumes 2-3 steps, so using
        # max_turns directly is far too strict for tool-heavy subagents.
        # A turn can consume a model and one tool step.  Keep a small fixed
        # allowance for framework edges, but honour the task contract instead
        # of turning a 12-turn subagent into a 100-step run.
        "recursion_limit": max(8, max_turns * 3 + 2),
    }

    answer_fragments: list[str] = []
    latest_message = ""
    message_index = 0

    async def _consume_events() -> str:
        nonlocal latest_message, message_index
        if cancellation_token is not None:
            cancellation_token.raise_if_cancelled()
        async for event in graph.astream_events(
            {"messages": [{"role": "user", "content": prompt}]},
            config=invoke_config,
            version="v2",
            stream_mode=["messages", "custom", "updates", "tasks"],
        ):
            if cancellation_token is not None:
                cancellation_token.raise_if_cancelled()
            kind = event.get("event")
            data = event.get("data", {}) or {}
            if kind == "on_chat_model_stream":
                chunk_text = _extract_chunk_text(data.get("chunk"))
                if chunk_text:
                    answer_fragments.append(chunk_text)
            elif kind == "on_chat_model_end":
                final_text = extract_chat_model_output_text(data.get("output")).strip()
                candidate = final_text or "".join(answer_fragments).strip()
                if candidate and candidate != latest_message:
                    latest_message = candidate
                    message_index += 1
                    _emit_task_event(
                        writer,
                        {
                            "type": "task_running",
                            "task_id": task_id,
                            "description": description,
                            "subagent_type": subagent_cfg.name,
                            "trace_id": trace_id,
                            "message_index": message_index,
                            "total_messages": message_index,
                            "message_text": _trim_text(candidate, 800),
                        },
                    )
                answer_fragments.clear()
        return latest_message or "".join(answer_fragments).strip()

    async def _consume_with_cancellation() -> str:
        if cancellation_token is None:
            return await _consume_events()
        cancellation_token.raise_if_cancelled()
        owner_task = asyncio.current_task()

        async def cancel_owner_when_signalled() -> None:
            await cancellation_token.wait()
            if owner_task is not None and not owner_task.done():
                owner_task.cancel()

        cancellation_waiter = asyncio.create_task(cancel_owner_when_signalled())
        try:
            return await _consume_events()
        finally:
            if not cancellation_waiter.done():
                cancellation_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await cancellation_waiter

    async def _consume_and_validate_contract() -> str:
        answer = await _consume_with_cancellation()
        missing_required = sorted(set(subagent_cfg.required_tools) - invoked_tools)
        if missing_required:
            raise RuntimeError(
                "subagent required tools were not completed: "
                + ", ".join(missing_required)
            )
        return answer

    # A delegated run gets its own Skill-load state instead of inheriting the
    # parent's ContextVar.  This makes the child prove it loaded the governing
    # instructions before any project business tool is allowed.
    skill_state_tokens = set_request_skill_state(set(selected_skills or set()))
    try:
        execution_budget = get_subagent_execution_budget()
        # AgentRuntimeManager owns the active slot when the task runs inside the
        # platform streaming runtime.  Keep the semaphore for compatibility
        # with direct/legacy callers, but never acquire both controls.
        if runtime_manager_managed or execution_budget is None:
            return await asyncio.wait_for(
                _consume_and_validate_contract(),
                timeout=subagent_cfg.timeout_seconds,
            )
        async with execution_budget.semaphore:
            return await asyncio.wait_for(
                _consume_and_validate_contract(),
                timeout=subagent_cfg.timeout_seconds,
            )
    finally:
        reset_request_skill_state(skill_state_tokens)


@tool("task", parse_docstring=True)
async def task_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    description: str,
    prompt: str,
    subagent_type: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    max_turns: int | None = None,
) -> str:
    """Delegate a task to a local subagent.

    Args:
        description: Short task description shown in runtime events.
        prompt: Detailed delegated prompt.
        subagent_type: Runtime agent type, such as `deep-research` or `general-purpose`.
        max_turns: Optional cap for delegated agent turns.
    """
    configurable = runtime.config.get("configurable", {}) if runtime is not None else {}
    runtime_agent_specs = configurable.get("runtime_agent_specs")
    subagent_cfg = None
    if isinstance(runtime_agent_specs, dict):
        candidate = runtime_agent_specs.get(subagent_type)
        if isinstance(candidate, RuntimeAgentSpec):
            subagent_cfg = candidate
    try:
        subagent_cfg = subagent_cfg or _RUNTIME_AGENT_LOADER.get(subagent_type)
    except ValueError:
        available = ", ".join(_RUNTIME_AGENT_LOADER.available_names())
        return f"Error: Unknown subagent type '{subagent_type}'. Available: {available}"

    writer = get_stream_writer()
    task_id = str(tool_call_id or "").strip()
    metadata = runtime.config.get("metadata", {}) if runtime is not None else {}

    selected_skills_raw = configurable.get("selected_skills")
    selected_skills = None
    if isinstance(selected_skills_raw, list):
        selected_skills = {
            str(skill).strip()
            for skill in selected_skills_raw
            if isinstance(skill, str) and str(skill).strip()
        } or None

    kb_ids_raw = configurable.get("kb_ids")
    kb_ids = [str(item) for item in kb_ids_raw] if isinstance(kb_ids_raw, list) else []
    raw_use_search = configurable.get("use_search", True)
    use_search = raw_use_search if isinstance(raw_use_search, bool) else str(raw_use_search).strip().lower() not in {"0", "false", "no", "off"}
    thread_id = configurable.get("thread_id") if isinstance(configurable.get("thread_id"), str) else None
    model_name = metadata.get("model_name") if isinstance(metadata.get("model_name"), str) else None
    trace_id = str(metadata.get("trace_id") or uuid.uuid4())[:12]
    normalized_description = description.strip() or f"delegated-{subagent_type}"
    runtime_permissions = configurable.get("runtime_permissions")
    if not isinstance(runtime_permissions, dict):
        runtime_permissions = {}
    request_metadata = configurable.get("request_metadata")
    if not isinstance(request_metadata, dict):
        request_metadata = {}
    domain_context = configurable.get("domain_context")
    if not isinstance(domain_context, dict):
        domain_context = {}
    domain = str(configurable.get("domain") or "").strip() or None
    workflow_name = str(configurable.get("workflow_name") or "").strip() or None
    tool_policy = str(configurable.get("tool_policy") or "").strip() or None
    context_policy = configurable.get("context_policy")
    checkpointer = configurable.get("agent_checkpointer")
    if not domain:
        raise RuntimeError(
            "task delegation requires the parent agent's explicit domain"
        )
    if tool_policy in {None, "", "default"}:
        raise RuntimeError(
            "task delegation requires the parent agent's explicit registered "
            f"tool_policy for domain '{domain}'"
        )
    tool_runtime_context = None
    if domain and tool_policy and thread_id:
        tool_runtime_context = ToolRuntimeContext(
            request_id=str(configurable.get("request_id") or ""),
            user_id=str(configurable.get("user_id") or ""),
            agent_code=str(configurable.get("agent_code") or ""),
            domain=domain,
            thread_id=thread_id,
            role=str(runtime_permissions.get("auth_role") or "user").strip().lower() or "user",
            permissions=dict(runtime_permissions),
            time_context=str(configurable.get("time_context") or ""),
            request_metadata=dict(request_metadata),
            domain_context=dict(domain_context),
        )

    try:
        effective_max_turns = max(1, min(int(max_turns or subagent_cfg.max_turns), subagent_cfg.max_turns))
        runtime_manager = get_agent_runtime_manager()

        def emit_started() -> None:
            _emit_task_event(
                writer,
                {
                    "type": "task_started",
                    "task_id": task_id,
                    "description": normalized_description,
                    "subagent_type": subagent_type,
                    "trace_id": trace_id,
                },
            )

        async def run_delegated_agent(
            cancellation_token: AgentCancellationToken | None,
        ) -> str:
            return await _run_subagent(
                subagent_cfg=subagent_cfg,
                description=normalized_description,
                prompt=prompt,
                model_name=model_name,
                max_turns=effective_max_turns,
                thread_id=thread_id,
                kb_ids=kb_ids,
                use_search=use_search,
                selected_skills=selected_skills,
                domain=domain,
                workflow_name=workflow_name,
                tool_policy=tool_policy,
                tool_runtime_context=tool_runtime_context,
                task_id=task_id,
                trace_id=trace_id,
                runtime_manager_managed=runtime_manager is not None,
                cancellation_token=cancellation_token,
                context_policy=context_policy,
                checkpointer=checkpointer,
            )

        async def execute_task_lifecycle() -> str:
            cancellation_token = (
                await runtime_manager.token_for(task_id)
                if runtime_manager is not None
                else None
            )
            result_text = await run_delegated_agent(cancellation_token)
            if cancellation_token is not None:
                # A provider or nested graph may swallow Task.cancel().  The
                # tree token is authoritative, so never publish completion
                # after the manager has accepted an interrupt.
                cancellation_token.raise_if_cancelled()
            result_text = result_text or "任务已完成，但未返回文本结果。"
            handoff = build_subagent_result(
                role=subagent_type,
                objective=normalized_description,
                answer=result_text,
                task_id=task_id,
            )
            # The model/text adapter remains forgiving, but the execution
            # boundary only publishes a complete, non-coercing V2 contract.
            validate_subagent_result_strict(handoff)
            handoff_text = format_subagent_handoff(handoff)
            metadata_payload = {
                "result_preview": _trim_text(result_text, 500),
                "confidence": handoff.get("confidence"),
                "evidence_refs": handoff.get("evidence_refs", []),
                "risks": handoff.get("risks", []),
                **extract_task_result_metadata(result_text),
            }
            _emit_task_event(
                writer,
                {
                    "type": "task_completed",
                    "task_id": task_id,
                    "description": normalized_description,
                    "subagent_type": subagent_type,
                    "trace_id": trace_id,
                    "result": result_text,
                    "result_preview": metadata_payload["result_preview"],
                    **metadata_payload,
                },
            )
            return (
                f"Task Succeeded.\n{handoff_text}\n\n"
                f"Metadata: {json.dumps(metadata_payload, ensure_ascii=False)}"
            )

        if runtime_manager is None:
            emit_started()
            return await execute_task_lifecycle()
        async with runtime_manager.spawn(
            task_id,
            metadata={
                "description": normalized_description,
                "subagent_type": subagent_type,
                "trace_id": trace_id,
            },
        ):
            emit_started()
            return await execute_task_lifecycle()
    except asyncio.CancelledError:
        _emit_task_event(
            writer,
            {
                "type": "task_cancelled",
                "task_id": task_id,
                "description": normalized_description,
                "subagent_type": subagent_type,
                "trace_id": trace_id,
                "error": "Subagent execution cancelled",
            },
        )
        raise
    except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
        error = f"Subagent timed out after {subagent_cfg.timeout_seconds}s"
        _emit_task_event(
            writer,
            {
                "type": "task_timed_out",
                "task_id": task_id,
                "description": normalized_description,
                "subagent_type": subagent_type,
                "trace_id": trace_id,
                "error": error,
            },
        )
        return f"Task timed out. Error: {error}"
    except Exception as exc:
        logger.exception("Local task tool failed: %s", exc)
        error = str(exc)
        _emit_task_event(
            writer,
            {
                "type": "task_failed",
                "task_id": task_id,
                "description": normalized_description,
                "subagent_type": subagent_type,
                "trace_id": trace_id,
                "error": error,
            },
        )
        return f"Task failed. Error: {error}"
