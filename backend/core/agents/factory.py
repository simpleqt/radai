"""Agent graph factory — pure-argument construction of LangGraph agent graphs.

Uses ``langchain.agents.create_agent`` directly with feature-driven
middleware assembly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware

from backend.core.agents.features import RuntimeFeatures
from backend.core.agents.state import ThreadState

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


def create_agent_graph(
    model: BaseChatModel,
    tools: list[BaseTool] | None = None,
    *,
    system_prompt: str | None = None,
    middleware: list[AgentMiddleware] | None = None,
    features: RuntimeFeatures | None = None,
    extra_middleware: list[AgentMiddleware] | None = None,
    state_schema: type | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    name: str = "default",
) -> CompiledStateGraph:
    """Create a LangGraph agent graph from plain Python arguments.

    Parameters
    ----------
    model:
        Chat model instance.
    tools:
        User-provided tools. Feature-injected tools are appended automatically.
    system_prompt:
        System message. ``None`` uses a minimal default.
    middleware:
        **Full takeover** — if provided, this exact list is used.
        Cannot be combined with *features* or *extra_middleware*.
    features:
        Declarative feature flags. Cannot be combined with *middleware*.
    extra_middleware:
        Additional middlewares inserted into the auto-assembled chain.
    state_schema:
        LangGraph state type. Defaults to ``ThreadState``.
    checkpointer:
        Optional persistence backend.
    name:
        Agent name.
    """
    if middleware is not None and features is not None:
        raise ValueError("Cannot specify both 'middleware' and 'features'.")
    if middleware is not None and extra_middleware:
        raise ValueError("Cannot use 'extra_middleware' with 'middleware' (full takeover).")

    effective_tools: list[BaseTool] = list(tools or [])
    effective_state = state_schema or ThreadState

    if middleware is not None:
        effective_middleware = list(middleware)
    else:
        feat = features or RuntimeFeatures()
        effective_middleware, extra_tools = _assemble_from_features(
            feat,
            name=name,
            extra_middleware=extra_middleware or [],
        )
        existing_names = {t.name for t in effective_tools}
        for t in extra_tools:
            if t.name not in existing_names:
                effective_tools.append(t)
                existing_names.add(t.name)

    if system_prompt:
        logger.info(f"[AgentGraph] 创建图 {name}, system_prompt 长度: {len(system_prompt)}")
    
    return create_agent(
        model=model,
        tools=effective_tools or None,
        middleware=effective_middleware,
        system_prompt=system_prompt,
        state_schema=effective_state,
        checkpointer=checkpointer,
        name=name,
    )


def _assemble_from_features(
    feat: RuntimeFeatures,
    *,
    name: str = "default",
    extra_middleware: list[AgentMiddleware] | None = None,
) -> tuple[list[AgentMiddleware], list[BaseTool]]:
    """Build an ordered middleware chain + extra tools from feature flags.

    Middleware order:
      0.   DanglingToolCallMiddleware (always)
      1.   GuardrailMiddleware (guardrail feature)
      2.   ToolErrorHandlingMiddleware (always)
      3.   SummarizationMiddleware (summarization feature)
      4.   ViewImageMiddleware (vision feature)
      5.   SubagentLimitMiddleware (subagent feature)
      6.   LoopDetectionMiddleware (always)
      7.   Extra middlewares inserted before the end of the chain
    """
    from backend.core.agents.middlewares.dangling_tool_call import DanglingToolCallMiddleware
    from backend.core.agents.middlewares.tool_error_handling import ToolErrorHandlingMiddleware
    from backend.core.agents.middlewares.loop_detection import LoopDetectionMiddleware

    chain: list[AgentMiddleware] = []
    extra_tools: list[BaseTool] = []

    chain.append(DanglingToolCallMiddleware())

    if feat.guardrail is not False:
        if isinstance(feat.guardrail, AgentMiddleware):
            chain.append(feat.guardrail)
        else:
            raise ValueError("guardrail=True requires a custom AgentMiddleware instance")

    chain.append(ToolErrorHandlingMiddleware())

    if feat.summarization is not False:
        if isinstance(feat.summarization, AgentMiddleware):
            chain.append(feat.summarization)
        else:
            raise ValueError("summarization=True requires a custom AgentMiddleware instance")

    if feat.vision is not False:
        if isinstance(feat.vision, AgentMiddleware):
            chain.append(feat.vision)
        else:
            from backend.core.agents.middlewares.view_image import ViewImageMiddleware
            chain.append(ViewImageMiddleware())

    if feat.subagent is not False:
        if isinstance(feat.subagent, AgentMiddleware):
            chain.append(feat.subagent)
        else:
            from backend.core.agents.middlewares.subagent_limit import SubagentLimitMiddleware
            chain.append(SubagentLimitMiddleware())

    chain.append(LoopDetectionMiddleware())

    if feat.clarification is not False:
        logger.info(
            "[AgentGraph] clarification feature is disabled globally; ignoring clarification=%r",
            feat.clarification,
        )

    if extra_middleware:
        _insert_extra(chain, extra_middleware)

    return chain, extra_tools


def _insert_extra(
    chain: list[AgentMiddleware],
    extras: list[AgentMiddleware],
) -> None:
    """Insert extra middlewares into *chain* using @Next/@Prev anchors."""
    anchored: list[tuple[AgentMiddleware, str, type]] = []
    unanchored: list[AgentMiddleware] = []

    for mw in extras:
        next_anchor = getattr(type(mw), "_next_anchor", None)
        prev_anchor = getattr(type(mw), "_prev_anchor", None)
        if next_anchor and prev_anchor:
            raise ValueError(f"{type(mw).__name__} cannot have both @Next and @Prev")
        if next_anchor:
            anchored.append((mw, "next", next_anchor))
        elif prev_anchor:
            anchored.append((mw, "prev", prev_anchor))
        else:
            unanchored.append(mw)

    for mw in unanchored:
        chain.append(mw)

    pending = list(anchored)
    max_rounds = len(pending) + 1
    for _ in range(max_rounds):
        if not pending:
            break
        remaining = []
        for mw, direction, anchor in pending:
            idx = next((i for i, m in enumerate(chain) if isinstance(m, anchor)), None)
            if idx is None:
                remaining.append((mw, direction, anchor))
                continue
            chain.insert(idx + 1 if direction == "next" else idx, mw)
        if len(remaining) == len(pending):
            names = [type(m).__name__ for m, _, _ in remaining]
            raise ValueError(f"Cannot resolve positions for {', '.join(names)}")
        pending = remaining
