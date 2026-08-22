"""Standardized agent runtime API.

This route exposes the AgentService -> Workflow -> AgentEvent -> SSE boundary.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

# 命名空间兼容:保留拆分前的全部顶层绑定(测试与扩展按 agent_runtime.X 引用)
import asyncio  # noqa: F401
import base64  # noqa: F401
import binascii  # noqa: F401
import json  # noqa: F401
import re  # noqa: F401
import uuid  # noqa: F401
from dataclasses import replace  # noqa: F401
from typing import Any  # noqa: F401
from fastapi import Depends, HTTPException, Query, Request  # noqa: F401
from fastapi.responses import StreamingResponse  # noqa: F401
from pydantic import BaseModel, Field, model_validator  # noqa: F401
from backend.api.dependencies.auth import (  # noqa: F401
    ApiKeyAuth,
    optional_verify_api_key,
    require_admin,
    verify_api_key,
)
from backend.application.agent_trace import (  # noqa: F401
    AgentRunReplay,
    AgentTraceStoreProtocol,
    create_agent_trace_store,
)
from backend.application.agent_trace.models import AgentRunRecord  # noqa: F401
from backend.application.agents.definition import (  # noqa: F401
    AgentDefinitionValidationError,
    AgentDefinitionValidator,
)
from backend.application.agents.profile_loader import AgentProfileLoader  # noqa: F401
from backend.application.event_stream.sse_encoder import SSEEncoder  # noqa: F401
from backend.application.skills.manifest_loader import SkillManifestLoader  # noqa: F401
from backend.application.skills.public_catalog import PublicSkillCatalog  # noqa: F401
from backend.application.tools.registry import ToolRegistry  # noqa: F401
from backend.application.tools.runtime_state import tool_runtime_state  # noqa: F401
from backend.core.thread_key import validate_external_thread_id  # noqa: F401
from backend.domain.agents.request import AgentRequest  # noqa: F401
from backend.domain.agents.workflows import (  # noqa: F401
    SUPPORTED_WORKFLOWS,
    normalize_workflow_name,
)

from backend.api.schemas.agent_runtime import (  # noqa: F401  (兼容 re-export)
    AgentRuntimeQueryRequest,
    ImageAttachment,
)
from backend.api.routers.agent_runtime_catalog import (  # noqa: F401  (兼容 re-export)
    AgentInfoService,
    _ensure_agent_definition,
    _ensure_agent_profile,
    _list_registered_tools,
    _profile_to_dict,
    _profile_to_runtime_dict,
    _resolve_policy_tools,
    _skill_manifest_to_dict,
    _tool_to_dict,
    get_agent_profile,
    get_agent_runtime_info,
    get_tool_runtime_state,
    list_agent_profiles,
    list_policy_tools,
    list_skill_manifests,
    list_tools,
)
from backend.api.routers.agent_runtime_observability import (  # noqa: F401  (兼容 re-export)
    AgentTraceStore,
    _payloads_for_kind,
    _read_trace_records_for_auth,
    _trace_counts,
    _trace_summary,
    cancel_live_agent_run,
    get_agent_run_trace,
    list_admin_agent_runs,
    list_agent_run_compactions,
    list_agent_run_context_packs,
    list_agent_run_events,
    list_agent_run_handoffs,
    list_agent_run_tool_calls,
    list_agent_run_units,
    replay_agent_run_trace,
)
from backend.api.routers.agent_runtime_tasks import (  # noqa: F401  (兼容 re-export)
    AgentService,
    _RECONNECTABLE_BACKGROUND_WORKFLOWS,
    _request_file_ids,
    _uploaded_file_manifest,
    _uses_deep_research_background,
    cancel_agent_runtime_task,
    get_agent_runtime_task_status,
    get_agent_runtime_thread_active_task,
    query_agent_runtime,
    stream_agent_runtime_task_events,
)
from backend.api.routers import agent_runtime_catalog as _catalog
from backend.api.routers import agent_runtime_observability as _observability
from backend.api.routers import agent_runtime_tasks as _tasks

router = APIRouter(prefix="/agent-runtime", tags=["agent-runtime-v2"])
router.include_router(_catalog.router)
router.include_router(_observability.router)
router.include_router(_tasks.router)
logger = logging.getLogger(__name__)
