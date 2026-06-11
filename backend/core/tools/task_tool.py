"""Local task tool for delegated subagent execution."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.config import get_stream_writer
from langgraph.typing import ContextT

from backend.core.agents import RuntimeFeatures, ThreadState, create_agent_graph
from backend.core.models import create_chat_model
from backend.application.agent_executors.stream_utils import (
    extract_chat_model_output_text,
    extract_task_result_metadata,
    stringify_message_content,
    summarize_tool_output,
)
from backend.application.tools.tool_assembly_pipeline import ToolAssemblyPipeline

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalSubagentConfig:
    name: str
    system_prompt: str
    allowed_tools: set[str] | None = None
    disallowed_tools: set[str] | None = None
    max_turns: int = 12
    timeout_seconds: int = 600


_SUBAGENT_CONFIGS: dict[str, LocalSubagentConfig] = {
    "deep-research": LocalSubagentConfig(
        name="deep-research",
        system_prompt="""你是深度研究子智能体，负责处理主控智能体拆解出的单个研究维度。

规则：
- 你的目标是检索证据并输出简明、可信、可汇总的分析结果。
- 优先使用 `multi_kb_search` 做知识库检索；如证据不足，再使用 `web_search` 补充。
- 不要调用 `task`，不要再委派子智能体。
- 不要向用户追问，也不要生成最终总报告工件。
- 如需要领域方法，请先使用技能工具读取相关 skill。

输出格式：
1. 子任务结论
2. 证据摘要（区分 KB / Web）
3. 局限与风险
4. 对主控智能体的建议
5. 参考来源
""",
        allowed_tools={
            "multi_kb_search",
            "web_search",
            "read_local_skill_file",
            "read_skill_reference",
            "list_available_skills",
            "chat_file_search",
        },
        disallowed_tools={"task", "save_deep_research_report"},
        max_turns=12,
        timeout_seconds=600,
    ),
    "general-purpose": LocalSubagentConfig(
        name="general-purpose",
        system_prompt="""你是通用委派子智能体。你需要在隔离上下文里完成主智能体交给你的任务，并返回清晰结论。

规则：
- 直接执行任务，不要再向用户澄清。
- 可以使用当前可用工具完成检索、分析或文件操作。
- 不要调用 `task`，不要嵌套委派。
- 最终返回：完成情况、关键发现、相关文件/结果、风险或限制。
""",
        allowed_tools=None,
        disallowed_tools={"task", "present_files"},
        max_turns=16,
        timeout_seconds=600,
    ),
}


def _trim_text(value: Any, max_chars: int = 500) -> str:
    text = summarize_tool_output(value, max_length=max_chars)
    return text[:max_chars] + "..." if len(text) > max_chars else text


def _extract_chunk_text(chunk: Any) -> str:
    if hasattr(chunk, "content"):
        return stringify_message_content(getattr(chunk, "content"))
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
) -> BaseTool:
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
            result = await tool_item.ainvoke(kwargs)
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
        return result

    args_schema = getattr(tool_item, "args_schema", None)
    kwargs: dict[str, Any] = {
        "name": tool_item.name,
        "description": tool_item.description or tool_item.name,
        "coroutine": _invoke,
    }
    if args_schema is not None:
        kwargs["args_schema"] = args_schema
    return StructuredTool.from_function(**kwargs)


def _build_subagent_tools(
    *,
    subagent_cfg: LocalSubagentConfig,
    model_name: str | None,
    use_search: bool,
    kb_ids: list[str],
    selected_skills: set[str] | None,
    thread_id: str | None,
    writer,
    task_id: str,
    description: str,
    trace_id: str,
) -> list[BaseTool]:
    pipeline = ToolAssemblyPipeline(
        model_name=model_name,
        subagent_enabled=False,
        use_search=use_search,
        kb_ids=kb_ids,
        selected_skills=selected_skills,
        task_type="deep_research" if subagent_cfg.name == "deep-research" else None,
        thread_id=thread_id,
    )
    tools = pipeline.assemble()

    disallowed = set(subagent_cfg.disallowed_tools or set()) | {"task", "present_files"}
    allowed_tools = set(subagent_cfg.allowed_tools) if subagent_cfg.allowed_tools is not None else None
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
            )
        )
    return filtered


async def _run_subagent(
    *,
    subagent_cfg: LocalSubagentConfig,
    description: str,
    prompt: str,
    model_name: str | None,
    max_turns: int,
    thread_id: str | None,
    kb_ids: list[str],
    use_search: bool,
    selected_skills: set[str] | None,
    task_id: str,
    trace_id: str,
) -> str:
    writer = get_stream_writer()
    tools = _build_subagent_tools(
        subagent_cfg=subagent_cfg,
        model_name=model_name,
        use_search=use_search,
        kb_ids=kb_ids,
        selected_skills=selected_skills,
        thread_id=thread_id,
        writer=writer,
        task_id=task_id,
        description=description,
        trace_id=trace_id,
    )
    system_prompt = subagent_cfg.system_prompt
    if selected_skills and "academic-search" in selected_skills:
        system_prompt += """

- 已启用学术搜索：文献/论文/引用类检索优先使用 `academic_search`；`web_search` 仅补充时效资讯。
"""
    model = create_chat_model(name=model_name, thinking_enabled=False)
    graph = create_agent_graph(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        features=RuntimeFeatures(clarification=False, subagent=False, vision=False),
        name=f"subagent-{subagent_cfg.name}",
        state_schema=ThreadState,
    )

    effective_thread_id = thread_id or f"subagent-exec-{task_id}"
    invoke_config = {
        "configurable": {
            "thread_id": effective_thread_id,
            "kb_ids": kb_ids,
            "selected_skills": sorted(selected_skills) if selected_skills else [],
            "use_search": use_search,
        },
        "metadata": {
            "model_name": model_name,
            "trace_id": trace_id,
            "subagent_type": subagent_cfg.name,
        },
        # LangGraph recursion_limit counts internal graph steps (model call +
        # tool call each count as one step), not user-facing conversation turns.
        # A single "turn" with tool use typically consumes 2-3 steps, so using
        # max_turns directly is far too strict for tool-heavy subagents.
        # Tool-heavy subagents need a looser LangGraph recursion budget than max_turns.
        "recursion_limit": max(100, max_turns * 4),
    }

    answer_fragments: list[str] = []
    latest_message = ""
    message_index = 0

    async def _consume_events() -> str:
        nonlocal latest_message, message_index
        async for event in graph.astream_events(
            {"messages": [{"role": "user", "content": prompt}]},
            config=invoke_config,
            version="v2",
            stream_mode=["messages", "custom", "updates", "tasks"],
        ):
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

    return await asyncio.wait_for(_consume_events(), timeout=subagent_cfg.timeout_seconds)


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
        subagent_type: Subagent type, such as `deep-research` or `general-purpose`.
        max_turns: Optional cap for delegated agent turns.
    """
    subagent_cfg = _SUBAGENT_CONFIGS.get(subagent_type)
    if subagent_cfg is None:
        available = ", ".join(sorted(_SUBAGENT_CONFIGS))
        return f"Error: Unknown subagent type '{subagent_type}'. Available: {available}"

    writer = get_stream_writer()
    configurable = runtime.config.get("configurable", {}) if runtime is not None else {}
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

    _emit_task_event(
        writer,
        {
            "type": "task_started",
            "task_id": tool_call_id,
            "description": normalized_description,
            "subagent_type": subagent_type,
            "trace_id": trace_id,
        },
    )

    try:
        effective_max_turns = max(1, min(int(max_turns or subagent_cfg.max_turns), subagent_cfg.max_turns))
        result_text = await _run_subagent(
            subagent_cfg=subagent_cfg,
            description=normalized_description,
            prompt=prompt,
            model_name=model_name,
            max_turns=effective_max_turns,
            thread_id=thread_id,
            kb_ids=kb_ids,
            use_search=use_search,
            selected_skills=selected_skills,
            task_id=tool_call_id,
            trace_id=trace_id,
        )
        result_text = result_text or "任务已完成，但未返回文本结果。"
        metadata_payload = {
            "result_preview": _trim_text(result_text, 500),
            **extract_task_result_metadata(result_text),
        }
        _emit_task_event(
            writer,
            {
                "type": "task_completed",
                "task_id": tool_call_id,
                "description": normalized_description,
                "subagent_type": subagent_type,
                "trace_id": trace_id,
                "result": result_text,
                "result_preview": metadata_payload["result_preview"],
                **metadata_payload,
            },
        )
        return f"Task Succeeded. Result: {result_text}\n\nMetadata: {json.dumps(metadata_payload, ensure_ascii=False)}"
    except asyncio.TimeoutError:
        error = f"Subagent timed out after {subagent_cfg.timeout_seconds}s"
        _emit_task_event(
            writer,
            {
                "type": "task_timed_out",
                "task_id": tool_call_id,
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
                "task_id": tool_call_id,
                "description": normalized_description,
                "subagent_type": subagent_type,
                "trace_id": trace_id,
                "error": error,
            },
        )
        return f"Task failed. Error: {error}"
