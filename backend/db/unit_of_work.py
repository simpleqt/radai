"""Helpers for composing legacy CRUD methods inside an outer transaction."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

OUTER_TRANSACTION_MANAGED = "outer_transaction_managed"


def mark_outer_transaction_managed(session: AsyncSession) -> None:
    """Declare that the session owner, rather than CRUD methods, commits."""
    session.info[OUTER_TRANSACTION_MANAGED] = True


async def flush_or_commit(session: AsyncSession) -> None:
    """Flush an owned unit of work, preserving commits for legacy callers."""
    info = getattr(session, "info", None)
    if isinstance(info, dict) and info.get(OUTER_TRANSACTION_MANAGED) is True:
        await session.flush()
    else:
        await session.commit()
