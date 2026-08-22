"""MCP query execution helpers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from backend.application.agent_runtime.visible_answer import (
    collect_visible_answer_standard,
)
from backend.application.agent_service import AgentService
from backend.application.mcp.config import McpServiceSpec, get_default_mcp_service_spec
from backend.application.tools.runtime_invoker import RuntimeToolInvoker
from backend.core.thread_key import validate_external_thread_id
from backend.domain.agents.request import AgentRequest

MCP_ARGUMENT_BYTES_LIMIT = 128 * 1024
MCP_KNOWLEDGE_BASE_SELECTION_LIMIT = 16


async def collect_mcp_visible_answer(
    query: str,
    *,
    spec: McpServiceSpec | None = None,
    source: str,
    knowledge_bases: str | list[str] | None = None,
    thread_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    spec = spec or get_default_mcp_service_spec()
    selected_names = normalize_mcp_knowledge_base_names(knowledge_bases)
    selected_kb_ids = await resolve_mcp_knowledge_base_ids(
        selected_names,
        spec=spec,
    )
    return await collect_visible_answer_standard(
        question=query,
        agent_code=spec.agent_code,
        user_id=spec.user_id,
        workflow=spec.workflow,
        selected_kb_ids=selected_kb_ids,
        use_search=spec.use_search,
        thinking_enabled=spec.thinking_enabled,
        metadata={
            "source": source,
            "mcp_service": spec.name,
            "selected_knowledge_bases": selected_names,
        },
        thread_id=_resolve_session_id(
            thread_id=thread_id, conversation_id=conversation_id
        ),
    )


def normalize_mcp_knowledge_base_names(
    knowledge_bases: str | list[str] | None,
) -> list[str]:
    """Normalize an optional single KB name or ordered list of KB names."""

    if knowledge_bases is None:
        return []
    values = [knowledge_bases] if isinstance(knowledge_bases, str) else knowledge_bases
    if len(values) > MCP_KNOWLEDGE_BASE_SELECTION_LIMIT:
        raise ValueError(
            f"knowledge_bases 最多选择 {MCP_KNOWLEDGE_BASE_SELECTION_LIMIT} 个知识库"
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = str(value or "").strip()
        if not name:
            raise ValueError("knowledge_bases 不能包含空名称")
        if len(name) > 100:
            raise ValueError("知识库名称不能超过 100 个字符")
        if name not in seen:
            normalized.append(name)
            seen.add(name)
    return normalized


async def resolve_mcp_knowledge_base_ids(
    knowledge_base_names: list[str],
    *,
    spec: McpServiceSpec | None = None,
) -> list[str]:
    """Resolve names only within the MCP service's active agent bindings.

    The normal AgentService knowledge-base policy remains the authorization
    boundary for the resulting IDs. An empty selection deliberately returns an
    empty ID list so the runtime falls back to all accessible bound KBs.
    """

    if not knowledge_base_names:
        return []

    from sqlalchemy import select

    from backend.db.connection import read_only_session_context
    from backend.db.crud.agent_crud import get_runtime_agent_kb_ids
    from backend.db.models.knowledge_base import KnowledgeBase, KnowledgeBaseStatus

    spec = spec or get_default_mcp_service_spec()
    async with read_only_session_context() as session:
        bound_ids = await get_runtime_agent_kb_ids(session, spec.agent_code)
        if not bound_ids:
            raise ValueError(f"MCP 智能体 {spec.agent_code} 未绑定可用知识库")
        result = await session.execute(
            select(KnowledgeBase.kb_id, KnowledgeBase.kb_name).where(
                KnowledgeBase.kb_id.in_(bound_ids),
                KnowledgeBase.status == KnowledgeBaseStatus.ACTIVE,
            )
        )
        rows = result.all()

    ids_by_name: dict[str, list[str]] = {}
    for row in rows:
        ids_by_name.setdefault(str(row.kb_name).strip(), []).append(str(row.kb_id))

    missing = [name for name in knowledge_base_names if name not in ids_by_name]
    if missing:
        raise ValueError(f"知识库未绑定或不可用: {', '.join(missing)}")

    ambiguous = [name for name in knowledge_base_names if len(ids_by_name[name]) != 1]
    if ambiguous:
        raise ValueError(f"知识库名称不唯一，无法选择: {', '.join(ambiguous)}")

    return [ids_by_name[name][0] for name in knowledge_base_names]


def build_mcp_agent_request(
    *,
    question: str,
    user_id: str,
    spec: McpServiceSpec | None = None,
    source: str,
    thread_id: str | None = None,
    conversation_id: str | None = None,
) -> AgentRequest:
    spec = spec or get_default_mcp_service_spec()
    return AgentRequest(
        question=question,
        agent_code=spec.agent_code,
        user_id=user_id,
        requested_workflow=spec.workflow,
        use_search=spec.use_search,
        thinking_enabled=spec.thinking_enabled,
        metadata={"source": source, "mcp_service": spec.name},
        conversation_id=_resolve_session_id(
            thread_id=thread_id, conversation_id=conversation_id
        ),
    )


async def invoke_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    user_id: str,
    role: str = "user",
    spec: McpServiceSpec | None = None,
    workflow: str | None = None,
    selected_skills: list[str] | None = None,
    thread_id: str | None = None,
    conversation_id: str | None = None,
    checkpointer=None,
    allowed_tools: Sequence[str] | None = None,
) -> Any:
    """Invoke one catalogued tool through the normal policy/runtime boundary.

    ``allowed_tools`` is the caller credential's per-key allowlist. It must be
    enforced here as well as at the transport layer, because the built-in
    ``call_tool`` proxy would otherwise let an allowlisted entry point reach any
    other tool in the bound catalog.
    """
    spec = spec or get_default_mcp_service_spec()
    if allowed_tools is not None and tool_name not in set(allowed_tools):
        raise PermissionError(f"API Key 无权调用 MCP 工具: {tool_name}")
    normalized_arguments = dict(arguments or {})
    if len(normalized_arguments) > 64:
        raise ValueError("MCP 工具参数字段过多")
    validate_mcp_arguments(normalized_arguments)
    session_id = _resolve_session_id(
        thread_id=thread_id, conversation_id=conversation_id
    )
    # The MCP surface is intentionally a catalog *and* call contract.  Do not
    # let a caller reach a tool that happens to be assembled by a compatibility
    # path but is absent from the bound policy catalog.
    from backend.application.mcp.tool_catalog import (
        McpToolCatalogQuery,
        build_mcp_tool_catalog,
    )

    catalog = build_mcp_tool_catalog(
        McpToolCatalogQuery(
            agent_code=spec.agent_code,
            workflow=workflow or spec.workflow,
            selected_skills=set(selected_skills or []),
        )
    )
    catalog_names = {str(item.get("name") or "") for item in catalog.tools}
    if tool_name not in catalog_names:
        raise PermissionError(f"MCP tool '{tool_name}' is not in the bound catalog")
    request = AgentRequest(
        question=f"MCP tool call: {tool_name}",
        agent_code=spec.agent_code,
        user_id=user_id,
        requested_workflow=workflow or spec.workflow,
        selected_skills=list(selected_skills) if selected_skills is not None else None,
        use_search=spec.use_search,
        thinking_enabled=spec.thinking_enabled,
        metadata={"source": "mcp.call_tool", "mcp_service": spec.name},
        auth_role=role,
        conversation_id=session_id,
    )
    service = AgentService(checkpointer=checkpointer)
    request = await service.resolve_request_skills(request)
    resolved_workflow, context = await service.resolve_workflow(request)
    invoker = RuntimeToolInvoker(
        context=context,
        # ``_resolve_workflow`` returns an executable workflow object as its
        # first item.  Tool assembly needs the normalized workflow *name* from
        # the runtime context, otherwise the object repr is treated as an
        # unknown capability and the catalogued tool disappears at call time.
        workflow_name=context.workflow_name,
        selected_skills=set(request.selected_skills or []),
        resolved_agent_definition=getattr(
            resolved_workflow,
            "_resolved_definition",
            None,
        ),
    )
    return await invoker.invoke(tool_name, normalized_arguments)


def validate_mcp_arguments(arguments: dict[str, Any]) -> None:
    """Bound the serialized MCP argument envelope before catalog dispatch."""

    try:
        payload_size = len(
            json.dumps(
                arguments, ensure_ascii=False, default=str, separators=(",", ":")
            ).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("MCP 工具参数必须可序列化") from exc
    if payload_size > MCP_ARGUMENT_BYTES_LIMIT:
        raise ValueError("MCP 工具参数不能超过 128 KiB")


def _resolve_session_id(
    *,
    thread_id: str | None,
    conversation_id: str | None,
) -> str | None:
    """Normalize Codex-style thread aliases before they reach checkpoints."""

    if thread_id and conversation_id and thread_id != conversation_id:
        raise ValueError("thread_id 与 conversation_id 必须一致")
    value = (thread_id or conversation_id or "").strip()
    if not value:
        return None
    if len(value) > 100:
        raise ValueError("MCP thread_id 不能超过 100 个字符")
    return validate_external_thread_id(value)
