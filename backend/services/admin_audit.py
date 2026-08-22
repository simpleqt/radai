"""Shared writer for administrator operation audit records."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.utils.client_ip import UNKNOWN_CLIENT_IP, resolve_client_ip
from backend.db.models import AdminAuditLog


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    value = headers.get(name)
    return str(value) if value is not None else None


def _client_ip(request: Any) -> str | None:
    resolved = resolve_client_ip(request)
    return None if resolved == UNKNOWN_CLIENT_IP else resolved


async def write_admin_audit(
    session: AsyncSession,
    request: Any,
    actor: Any,
    *,
    action: str,
    target_type: str,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    status: str = "success",
) -> None:
    session.add(
        AdminAuditLog(
            actor_user_id=getattr(actor, "user_id", None) or None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            status=status,
            method=str(getattr(request, "method", "")),
            path=str(getattr(getattr(request, "url", None), "path", "")),
            ip_address=_client_ip(request),
            user_agent=_header(getattr(request, "headers", None), "user-agent"),
            detail_json=detail or {},
        )
    )
