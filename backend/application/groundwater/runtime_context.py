"""Groundwater project context adapter for the agent platform."""

from __future__ import annotations

from typing import Any

from backend.application.groundwater.project_service import GroundwaterProjectService


PROJECT_ID_METADATA_KEY = "groundwater_project_id"


def resolve_groundwater_domain_context(
    *,
    user_id: str,
    request_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Authorize and normalize the project selected for this agent request."""
    project_id = str(request_metadata.get(PROJECT_ID_METADATA_KEY) or "").strip()
    if not project_id:
        raise ValueError(f"缺少 metadata.{PROJECT_ID_METADATA_KEY}")

    context = GroundwaterProjectService().resolve(user_id, project_id)
    return {
        "project_id": context.project_id,
        "project_name": context.project_name,
        "data_version": context.data_version,
        "read_only": context.read_only,
    }
