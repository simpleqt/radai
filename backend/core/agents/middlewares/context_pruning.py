"""ContextPruningMiddleware — dynamically manages context using State Elevation and Just-In-Time (JIT) Assembly.

Designed to:
1. Intercept skill files ("read_local_skill_file") and knowledge search ("multi_kb_search").
2. Elevate/isolate their large outputs into Graph State variables (loaded_skills, retrieved_evidence).
3. Trim the verbose ToolMessage records in the permanent conversation history.
4. Perform Just-In-Time (JIT) Assembly right before LLM invocation by injecting state-derived
   rules/knowledge into the temporary SystemMessage payload.
"""

from __future__ import annotations

import logging
import contextvars
import os
from collections.abc import Awaitable, Callable
from typing_extensions import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage, SystemMessage, AIMessage
from langgraph.runtime import Runtime

from backend.core.skills.native_skill_tools import normalize_skill_tool_name

logger = logging.getLogger(__name__)

JIT_MAX_EVIDENCE_BLOCKS = max(1, int(os.getenv("JIT_MAX_EVIDENCE_BLOCKS", "2")))
JIT_MAX_EVIDENCE_CHARS = max(2000, int(os.getenv("JIT_MAX_EVIDENCE_CHARS", "8000")))
JIT_MAX_PROJECT_EVIDENCE_BLOCKS = max(1, int(os.getenv("JIT_MAX_PROJECT_EVIDENCE_BLOCKS", "4")))
JIT_MAX_PROJECT_EVIDENCE_CHARS = max(2000, int(os.getenv("JIT_MAX_PROJECT_EVIDENCE_CHARS", "12000")))
PROJECT_EVIDENCE_TOOLS = {
    "groundwater_project_skill_read",
    "inspect_project_tables",
    "query_project_tables",
    "summarize_project_table",
    "groundwater_prediction",
    "groundwater_quality_check",
}

_current_patched_messages: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "current_patched_messages", default=None
)


class ContextPruningMiddleware(AgentMiddleware[AgentState]):
    """Dynamically manages context via State Elevation and JIT Assembly.

    Cleans intermediate heavy ToolMessage states to prevent context bloat,
    elevates skill definitions and retrieval search evidence into ThreadState,
    and dynamically compiles them back into the SystemMessage right before model call.
    """

    def __init__(self, char_threshold: int = 1500, keep_latest_n: int = 1):
        self.char_threshold = char_threshold
        self.keep_latest_n = keep_latest_n
        logger.info(
            f"[ContextPruningMiddleware] Initialized with State Elevation & JIT Assembly: "
            f"char_threshold={char_threshold}, keep_latest_n={keep_latest_n}"
        )

    @staticmethod
    def _trim_retrieved_evidence(evidence: list[str]) -> list[str]:
        """保留最近若干次检索，并限制 JIT 注入总字符数。"""
        if not evidence:
            return []

        trimmed = list(evidence[-JIT_MAX_EVIDENCE_BLOCKS:])
        total = sum(len(item) for item in trimmed)
        if total <= JIT_MAX_EVIDENCE_CHARS:
            return trimmed

        kept: list[str] = []
        used = 0
        for item in reversed(trimmed):
            item_len = len(item)
            if used + item_len <= JIT_MAX_EVIDENCE_CHARS:
                kept.insert(0, item)
                used += item_len
            elif not kept:
                kept.insert(0, item[:JIT_MAX_EVIDENCE_CHARS])
                break
            else:
                break

        if len(kept) < len(trimmed):
            logger.info(
                "[JITAssembly] 检索证据已裁剪: blocks %d→%d, chars %d→%d (limit=%d)",
                len(trimmed),
                len(kept),
                total,
                sum(len(x) for x in kept),
                JIT_MAX_EVIDENCE_CHARS,
            )
        return kept

    @staticmethod
    def _trim_project_evidence(evidence: list[str]) -> list[str]:
        """保留地下水项目工具结果，避免长表被普通裁剪后模型看不到项目事实。"""
        if not evidence:
            return []

        trimmed = list(evidence[-JIT_MAX_PROJECT_EVIDENCE_BLOCKS:])
        total = sum(len(item) for item in trimmed)
        if total <= JIT_MAX_PROJECT_EVIDENCE_CHARS:
            return trimmed

        kept: list[str] = []
        used = 0
        for item in reversed(trimmed):
            item_len = len(item)
            if used + item_len <= JIT_MAX_PROJECT_EVIDENCE_CHARS:
                kept.insert(0, item)
                used += item_len
            elif not kept:
                kept.insert(0, item[:JIT_MAX_PROJECT_EVIDENCE_CHARS])
                break
            else:
                break
        return kept

    def _process_state_elevation(self, state: AgentState) -> None:
        """Scan messages in state, elevate skills/RAG contents to state keys, and prune the messages list."""
        messages = state.get("messages") or []
        if not messages:
            return

        # Ensure our target state keys are initialized in the Graph state dict
        if "loaded_skills" not in state or state["loaded_skills"] is None:
            state["loaded_skills"] = {}
        if "retrieved_evidence" not in state or state["retrieved_evidence"] is None:
            state["retrieved_evidence"] = []
        if "project_evidence" not in state or state["project_evidence"] is None:
            state["project_evidence"] = []

        loaded_skills = state["loaded_skills"]
        retrieved_evidence = state["retrieved_evidence"]
        project_evidence = state["project_evidence"]

        patched_messages = []
        state_updated = False

        # Build ID mapping from AI messages to find matching tool arguments
        ai_tool_calls_map = {}
        for msg in messages:
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                for tc in msg.tool_calls:
                    tc_id = tc.get("id")
                    if tc_id:
                        ai_tool_calls_map[tc_id] = tc

        for msg in messages:
            if isinstance(msg, ToolMessage):
                content_str = str(msg.content or "")
                
                # Check if it has already been pruned / elevated
                is_already_processed = (
                    content_str.startswith("[State Elevation:") or
                    content_str.startswith("[Context Isolation:") or
                    content_str.startswith("[System Notification:")
                )

                if not is_already_processed:
                    # 1. State Elevation for read_local_skill_file
                    if msg.name == "read_local_skill_file" and len(content_str) > 0:
                        if getattr(msg, "status", None) == "error":
                            patched_messages.append(msg)
                            continue
                        matching_tc = ai_tool_calls_map.get(msg.tool_call_id, {})
                        args = matching_tc.get("args", {})
                        if not isinstance(args, dict):
                            args = {"skill_name": args}
                        skill_name = normalize_skill_tool_name(
                            args.get("skill_name") or args.get("skill") or "unknown_skill"
                        )

                        loaded_skills[skill_name] = content_str
                        pruned_content = (
                            f"[State Elevation: Loaded skill content for '{skill_name}' successfully "
                            f"elevated to Graph State and enforced dynamically in the System Prompt.]"
                        )

                        patched_messages.append(
                            ToolMessage(
                                content=pruned_content,
                                tool_call_id=msg.tool_call_id,
                                name=msg.name,
                                status=msg.status,
                            )
                        )
                        state_updated = True
                        logger.info(f"[StateElevation] Elevated skill '{skill_name}' to Graph State (ThreadState)")
                        continue

                    # 2. Context Isolation for multi_kb_search
                    elif msg.name == "multi_kb_search" and len(content_str) > 0:
                        retrieved_evidence.append(content_str)
                        pruned_content = (
                            f"[Context Isolation: Retrieved relevant knowledge from database successfully "
                            f"isolated to retrieved_evidence Graph State to optimize context window.]"
                        )

                        patched_messages.append(
                            ToolMessage(
                                content=pruned_content,
                                tool_call_id=msg.tool_call_id,
                                name=msg.name,
                                status=msg.status,
                            )
                        )
                        state_updated = True
                        logger.info("[ContextIsolation] Isolated database RAG results to retrieved_evidence Graph State")
                        continue

                    # 3. Context Isolation for groundwater project data
                    elif msg.name in PROJECT_EVIDENCE_TOOLS and len(content_str) > self.char_threshold:
                        project_evidence.append(
                            f"<project_tool_result name=\"{msg.name}\">\n{content_str}\n</project_tool_result>"
                        )
                        pruned_content = (
                            f"[Context Isolation: Groundwater project data from tool '{msg.name}' "
                            f"successfully isolated to project_evidence Graph State.]"
                        )
                        patched_messages.append(
                            ToolMessage(
                                content=pruned_content,
                                tool_call_id=msg.tool_call_id,
                                name=msg.name,
                                status=msg.status,
                            )
                        )
                        state_updated = True
                        logger.info("[ContextIsolation] Isolated groundwater project result %s", msg.name)
                        continue

                    # 4. Standard pruning for general massive tool messages
                    elif len(content_str) > self.char_threshold:
                        pruned_content = (
                            f"[System Notification: The raw output of tool '{msg.name or 'unknown'}' "
                            f"({len(content_str)} characters) has been successfully digested and "
                            f"truncated by the context manager to conserve token budget.]"
                        )
                        patched_messages.append(
                            ToolMessage(
                                content=pruned_content,
                                tool_call_id=msg.tool_call_id,
                                name=msg.name,
                                status=msg.status,
                            )
                        )
                        state_updated = True
                        logger.info(f"[ContextPruning] Pruned generic tool result {msg.tool_call_id}")
                        continue

            patched_messages.append(msg)

        if state_updated:
            state["messages"] = patched_messages
            state["loaded_skills"] = loaded_skills
            state["retrieved_evidence"] = self._trim_retrieved_evidence(retrieved_evidence)
            state["project_evidence"] = self._trim_project_evidence(project_evidence)

    def _assemble_jit_context(self, messages: list, state: AgentState) -> list:
        """Compile loaded_skills and retrieved_evidence into a temporary SystemMessage context."""
        if not messages:
            return messages

        loaded_skills = state.get("loaded_skills") or {}
        retrieved_evidence = self._trim_retrieved_evidence(state.get("retrieved_evidence") or [])
        project_evidence = self._trim_project_evidence(state.get("project_evidence") or [])

        if not loaded_skills and not retrieved_evidence and not project_evidence:
            return messages

        # Compile JIT XML Context Block
        jit_blocks = []
        jit_blocks.append("\n\n# ======================================================================")
        jit_blocks.append("# DYNAMIC CONTEXT (JUST-IN-TIME CONTEXT ASSEMBLY)")
        jit_blocks.append("# ======================================================================")

        if loaded_skills:
            jit_blocks.append("\n<loaded_skills_definitions>")
            for name, content in loaded_skills.items():
                jit_blocks.append(f"\n### ACTIVE SKILL RULES: {name.upper()}")
                jit_blocks.append(content)
            jit_blocks.append("\n</loaded_skills_definitions>")

        if project_evidence:
            jit_blocks.append("\n<groundwater_project_evidence>")
            for idx, content in enumerate(project_evidence):
                jit_blocks.append(f"\n### PROJECT TOOL RESULT {idx + 1}:")
                jit_blocks.append(content)
            jit_blocks.append("\n</groundwater_project_evidence>")

        if retrieved_evidence:
            jit_blocks.append("\n<retrieved_evidence_database>")
            for idx, content in enumerate(retrieved_evidence):
                jit_blocks.append(f"\n### KNOWLEDGE EVIDENCE BLOCK {idx + 1}:")
                jit_blocks.append(content)
            jit_blocks.append("\n</retrieved_evidence_database>")

        jit_assembled_content = "\n".join(jit_blocks)

        patched_messages = list(messages)
        # Find the first SystemMessage in history to append JIT content
        system_msg_idx = -1
        for idx, msg in enumerate(patched_messages):
            if isinstance(msg, SystemMessage):
                system_msg_idx = idx
                break

        if system_msg_idx >= 0:
            original_sys_msg = patched_messages[system_msg_idx]
            new_content = str(original_sys_msg.content or "") + jit_assembled_content
            # Override SystemMessage content safely for this run
            patched_messages[system_msg_idx] = SystemMessage(
                content=new_content,
                additional_kwargs=original_sys_msg.additional_kwargs,
                response_metadata=original_sys_msg.response_metadata,
            )
            logger.info(
                f"[JITAssembly] Assembled dynamic context into existing SystemMessage: "
                f"added {len(jit_assembled_content)} characters"
            )
        else:
            # If no SystemMessage exists, prepend a new one
            new_sys_msg = SystemMessage(content=jit_assembled_content)
            patched_messages.insert(0, new_sys_msg)
            logger.info(f"[JITAssembly] Prepended new SystemMessage with dynamic context: {len(jit_assembled_content)} chars")

        return patched_messages

    def _patch_state(self, state: AgentState) -> list:
        self._process_state_elevation(state)
        messages = state.get("messages") or []
        # JIT context is request-local. Persisting the assembled SystemMessage would
        # append the same evidence again on every model round and grow context without bound.
        return self._assemble_jit_context(messages, state)

    @override
    def before_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        patched = self._patch_state(state)
        _current_patched_messages.set(patched)
        return None

    @override
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        patched = self._patch_state(state)
        _current_patched_messages.set(patched)
        return None

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        patched = _current_patched_messages.get()
        if patched is not None:
            # Safe safeguard: if request.messages length matches the state's message length, override it
            request = request.override(messages=patched)
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        patched = _current_patched_messages.get()
        if patched is not None:
            request = request.override(messages=patched)
        return await handler(request)
